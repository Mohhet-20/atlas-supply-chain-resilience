"""Disruption Scenario API: definitions, events, validation, simulation runs, results, alternatives and comparison.

Everything here analyses hypothetical scenarios. Simulations read the network and write only scenario/simulation tables.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from sqlalchemy import func, or_, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.models.enums import RegistryType, ScenarioStatus, ScenarioType, SimStatus
from app.models.scenario import (
    Scenario,
    SimulationAlternative,
    SimulationEdgeImpact,
    SimulationImpact,
    SimulationRun,
)
from app.registry import audit
from app.registry.audit import request_id_of
from app.scenarios import compare as comparer
from app.scenarios import config as cfg
from app.scenarios import runs, service, templates
from app.schemas.common import ErrorResponse
from app.schemas.scenarios import EventBody, EventPatch, ScenarioCreate, ScenarioPatch

router = APIRouter(prefix="/scenarios", tags=["disruption scenarios"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
Expected = Annotated[int | None, Query(ge=1, alias="expected_version")]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Scenario write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _limits(request: Request) -> runs.RunLimits:
    return runs.limits_from(request.app.state.settings)


def _aud(db, actor, request, action, target_type, target_id, details=None) -> None:  # noqa: ANN001
    audit.record(
        db,
        actor,
        action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
        request_id=request_id_of(request),
    )


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _run(db, run_id: int) -> SimulationRun:  # noqa: ANN001
    r = db.get(SimulationRun, run_id)
    if r is None:
        raise NotFoundError("Run not found.")
    return r


def _finished(r: SimulationRun) -> None:
    if r.status in (SimStatus.QUEUED, SimStatus.RUNNING):
        raise DomainValidationError(f"Run {r.id} is {r.status.value}; results are available once it finishes.")


# ------------------------------------------------------------------------------------------------------ reference


@router.get("/templates", summary="Reusable scenario templates (no entities; you must select real targets)")
def list_templates(_a: Viewer) -> dict[str, Any]:
    return {"templates": templates.list_templates(), "disclaimer": templates.DISCLAIMER}


@router.get("/limits", summary="Server-enforced limits and engine versions")
def limits(request: Request, _a: Viewer) -> dict[str, Any]:
    lim = _limits(request)
    return {
        "engine_version": cfg.ENGINE_VERSION, "config_version": cfg.CONFIG_VERSION, "config_hash": cfg.config_hash(),
        "max_events": lim.max_events, "max_targets_per_event": lim.max_targets_per_event, "max_depth": lim.max_depth,
        "max_nodes": lim.max_nodes, "max_edges": lim.max_edges, "time_budget_seconds": lim.time_budget_seconds,
        "max_active_runs": lim.max_active_runs, "client_overridable": False,
        "default_assumptions": cfg.default_assumptions(),
        "kind": "deterministic impact analysis of a stated hypothetical (not forecasting, not event monitoring)",
        "unsupported": {
            "product_supply_interruption": "Products have no capacity or outflow of their own; disrupt the supplier, facility or product link instead.",
            "transport_link_disruption": "ATLAS has no logistics-link entity.",
        },
    }  # fmt: skip


# --------------------------------------------------------------------------------------------------------- runs


@router.get("/runs", summary="Simulation run history across scenarios")
def list_all_runs(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 20,
    offset: Off = 0,
    status: SimStatus | None = None,
    scenario_id: int | None = None,
) -> dict[str, Any]:
    q = select(SimulationRun)
    if status:
        q = q.where(SimulationRun.status == status)
    if scenario_id:
        q = q.where(SimulationRun.scenario_id == scenario_id)
    names = dict(db.execute(select(Scenario.id, Scenario.name)).all())
    return _page(
        db, q.order_by(SimulationRun.id.desc()), limit, offset, lambda r: service.run_out(r, names.get(r.scenario_id))
    )


@router.get("/compare", responses=ERR, summary="Compare 2-5 completed or partial runs from persisted results")
def compare(
    db: DbSession, _a: Viewer, run_ids: Annotated[list[int], Query(min_length=2, max_length=5)]
) -> dict[str, Any]:
    if any(i < 1 or i > 2**31 - 1 for i in run_ids):
        raise DomainValidationError("Run ids must be positive integers.")
    return comparer.compare_runs(db, run_ids)


@router.get("/runs/{run_id}", responses=ERR, summary="Run status, assumptions, limits hit and summary")
def get_run(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _run(db, run_id)
    sc = db.get(Scenario, r.scenario_id)
    return service.run_out(r, sc.name if sc else None, full=True)


@router.post(
    "/runs/{run_id}/cancel",
    responses=ERR,
    summary="Cancel a queued run, or ask a running one to stop at its next checkpoint",
)
def cancel(run_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run(db, run_id)
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        runs.cancel_run(db, r)
        _aud(db, actor, request, "scenario.run.cancel", "SIMULATION_RUN", r.id, {"status": r.status.value})
        return service.run_out(r)


@router.get("/runs/{run_id}/impacts", responses=ERR, summary="Affected entities (paginated, filterable)")
def run_impacts(
    run_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, impact_kind: str | None = None,
    entity_type: RegistryType | None = None, exposure_level: str | None = None, quantification: str | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None, sort: Annotated[str, Query(pattern="^(depth|loss|name)$")] = "depth",
) -> dict[str, Any]:  # fmt: skip
    r = _run(db, run_id)
    _finished(r)
    stmt = select(SimulationImpact).where(SimulationImpact.run_id == run_id)
    if impact_kind:
        stmt = stmt.where(SimulationImpact.impact_kind == impact_kind.upper())
    if entity_type:
        stmt = stmt.where(SimulationImpact.entity_type == entity_type)
    if exposure_level:
        stmt = stmt.where(SimulationImpact.exposure_level == exposure_level.upper())
    if quantification:
        stmt = stmt.where(SimulationImpact.quantification == quantification.upper())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(SimulationImpact.entity_name.ilike(like), SimulationImpact.entity_code.ilike(like)))
    order = {
        "depth": (SimulationImpact.depth, SimulationImpact.id),
        "name": (SimulationImpact.entity_name, SimulationImpact.id),
        "loss": (SimulationImpact.loss_pct.desc().nulls_last(), SimulationImpact.id),
    }[sort]
    out = _page(db, stmt.order_by(*order), limit, offset, service.impact_out)
    out["run_status"] = r.status.value
    out["complete"] = r.status is SimStatus.COMPLETED
    return out


@router.get(
    "/runs/{run_id}/impacts/{entity_type}/{entity_id}",
    responses=ERR,
    summary="Explore how one entity is affected: path, parent relationships, events, rules",
)
def impact_path(
    run_id: IdPath, entity_type: RegistryType, entity_id: IdPath, db: DbSession, _a: Viewer
) -> dict[str, Any]:
    r = _run(db, run_id)
    _finished(r)
    i = db.scalar(
        select(SimulationImpact).where(
            SimulationImpact.run_id == run_id,
            SimulationImpact.entity_type == entity_type,
            SimulationImpact.entity_id == entity_id,
        )
    )
    if i is None:
        raise NotFoundError("This entity is not in the run's affected set.")
    refs = list(dict.fromkeys([s["ref"] for s in (i.primary_path or [])] + list(i.parent_edges or [])))
    edges = (
        {
            e.edge_ref: service.edge_out(e)
            for e in db.scalars(
                select(SimulationEdgeImpact).where(
                    SimulationEdgeImpact.run_id == run_id, SimulationEdgeImpact.edge_ref.in_(refs)
                )
            )
        }
        if refs
        else {}
    )
    events = [e for e in (r.scenario_snapshot or {}).get("events", []) if e["event_uid"] in (i.event_uids or [])]
    return {
        "impact": service.impact_out(i), "path": i.primary_path or [], "parent_relationships": [edges[x] for x in (i.parent_edges or []) if x in edges],
        "path_relationships": [edges[s["ref"]] for s in (i.primary_path or []) if s["ref"] in edges], "events": events,
        "rules_applied": i.rules or [], "other_paths": max(0, i.path_count - 1),
        "note": "The primary path follows the affected inbound relationship with the largest known contribution (ties: lowest reference). " + cfg.EXPLAIN_NOTE,
    }  # fmt: skip


@router.get("/runs/{run_id}/edges", responses=ERR, summary="Affected relationships (paginated)")
def run_edges(
    run_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 100, offset: Off = 0, effect: str | None = None
) -> dict[str, Any]:
    _finished(_run(db, run_id))
    stmt = select(SimulationEdgeImpact).where(SimulationEdgeImpact.run_id == run_id)
    if effect:
        stmt = stmt.where(SimulationEdgeImpact.effect == effect.upper())
    return _page(db, stmt.order_by(SimulationEdgeImpact.id), limit, offset, service.edge_out)


@router.get(
    "/runs/{run_id}/alternatives",
    responses=ERR,
    summary="Known alternative sources for analyst review (no actions are taken)",
)
def run_alternatives(
    run_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, for_type: RegistryType | None = None,
    for_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None, edge_status: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    r = _run(db, run_id)
    _finished(r)
    stmt = select(SimulationAlternative).where(SimulationAlternative.run_id == run_id)
    if for_type:
        stmt = stmt.where(SimulationAlternative.for_type == for_type)
    if for_id:
        stmt = stmt.where(SimulationAlternative.for_id == for_id)
    if edge_status:
        stmt = stmt.where(SimulationAlternative.edge_status == edge_status.upper())
    out = _page(
        db,
        stmt.order_by(SimulationAlternative.for_type, SimulationAlternative.for_id, SimulationAlternative.id),
        limit,
        offset,
        service.alt_out,
    )
    out["notice"] = (
        "Alternatives are graph connections only. Spare capacity, qualification and cost are not assessed. No substitution is performed or recommended."
    )
    return out


# --------------------------------------------------------------------------------------------------- scenarios


@router.post("", status_code=201, responses=ERR, summary="Create a scenario (DRAFT)")
def create_scenario(body: ScenarioCreate, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    lim = _limits(request)
    if body.events and len(body.events) > lim.max_events:
        raise DomainValidationError(f"A scenario may contain at most {lim.max_events} events.")
    with db.begin():
        sc = service.create_scenario(db, actor.name, name=body.name, description=body.description, scenario_type=body.scenario_type, assumptions=body.assumptions, notes=body.notes, template_key=body.template_key, max_scenarios=request.app.state.settings.scenario_max_scenarios)  # fmt: skip
        for ev in body.events or []:
            service.add_event(db, actor.name, sc, ev.values(), None, lim.max_events)
        _aud(
            db,
            actor,
            request,
            "scenario.create",
            "SCENARIO",
            sc.id,
            {"name": sc.name, "events": len(body.events or [])},
        )
        return service.scenario_out(sc, service.events_of(db, sc), 0)


@router.get("", responses=ERR, summary="Scenario library (paginated, filterable)")
def list_scenarios(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, status: ScenarioStatus | None = None,
    scenario_type: ScenarioType | None = None, q: Annotated[str | None, Query(max_length=100)] = None, owner: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    stmt = select(Scenario)
    if status:
        stmt = stmt.where(Scenario.status == status)
    if scenario_type:
        stmt = stmt.where(Scenario.scenario_type == scenario_type)
    if owner:
        stmt = stmt.where(Scenario.owner == owner)
    if q:
        stmt = stmt.where(Scenario.name.ilike(f"%{q.strip()}%"))
    counts = dict(db.execute(select(SimulationRun.scenario_id, func.count()).group_by(SimulationRun.scenario_id)).all())
    return _page(
        db,
        stmt.order_by(Scenario.updated_at.desc(), Scenario.id.desc()),
        limit,
        offset,
        lambda s: service.scenario_out(s, None, counts.get(s.id, 0)),
    )


@router.get("/{scenario_id}", responses=ERR, summary="One scenario with its events")
def get_scenario(scenario_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    sc = service.get_scenario(db, scenario_id)
    n = db.scalar(select(func.count()).select_from(SimulationRun).where(SimulationRun.scenario_id == sc.id)) or 0
    return service.scenario_out(sc, service.events_of(db, sc), n)


@router.patch(
    "/{scenario_id}", responses=ERR, summary="Update a scenario (optimistic version check; bumps the version)"
)
def patch_scenario(
    scenario_id: IdPath, body: ScenarioPatch, db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
        service.update_scenario(db, actor.name, sc, patch, body.expected_version)
        _aud(db, actor, request, "scenario.update", "SCENARIO", sc.id, {"fields": sorted(patch), "version": sc.version})
        return service.scenario_out(sc, service.events_of(db, sc))


@router.post("/{scenario_id}/archive", responses=ERR, summary="Archive a scenario (run history is kept)")
def archive(
    scenario_id: IdPath, db: DbSession, request: Request, actor: Editor, expected_version: Expected = None
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        service.archive(db, actor.name, sc, expected_version)
        _aud(db, actor, request, "scenario.archive", "SCENARIO", sc.id)
        return service.scenario_out(sc)


@router.post("/{scenario_id}/restore", responses=ERR, summary="Restore an archived scenario to DRAFT")
def restore(scenario_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        service.restore(db, actor.name, sc)
        _aud(db, actor, request, "scenario.restore", "SCENARIO", sc.id)
        return service.scenario_out(sc)


@router.get("/{scenario_id}/history", responses=ERR, summary="Definition change history")
def history(scenario_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    from app.models.scenario import ScenarioHistory

    service.get_scenario(db, scenario_id)
    q = select(ScenarioHistory).where(ScenarioHistory.scenario_id == scenario_id).order_by(ScenarioHistory.id.desc())
    return _page(
        db,
        q,
        limit,
        offset,
        lambda h: {"version": h.version, "action": h.action, "actor": h.actor, "at": h.at, "summary": h.summary},
    )


@router.post("/{scenario_id}/events", status_code=201, responses=ERR, summary="Add an event")
def add_event(
    scenario_id: IdPath,
    body: EventBody,
    db: DbSession,
    request: Request,
    actor: Editor,
    expected_version: Expected = None,
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        ev = service.add_event(db, actor.name, sc, body.values(), expected_version, _limits(request).max_events)
        _aud(
            db,
            actor,
            request,
            "scenario.event.add",
            "SCENARIO",
            sc.id,
            {"event_uid": ev.event_uid, "type": ev.event_type.value},
        )
        return {"event": service.event_out(ev), "scenario_version": sc.version, "scenario_status": sc.status.value}


@router.patch("/{scenario_id}/events/{event_id}", responses=ERR, summary="Update an event")
def patch_event(
    scenario_id: IdPath,
    event_id: IdPath,
    body: EventPatch,
    db: DbSession,
    request: Request,
    actor: Editor,
    expected_version: Expected = None,
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        ev = service.get_event(db, sc, event_id)
        service.update_event(db, actor.name, sc, ev, body.values(), expected_version)
        _aud(db, actor, request, "scenario.event.update", "SCENARIO", sc.id, {"event_uid": ev.event_uid})
        return {"event": service.event_out(ev), "scenario_version": sc.version, "scenario_status": sc.status.value}


@router.delete(
    "/{scenario_id}/events/{event_id}", responses=ERR, summary="Remove an event (earlier runs keep their frozen copy)"
)
def delete_event(
    scenario_id: IdPath,
    event_id: IdPath,
    db: DbSession,
    request: Request,
    actor: Editor,
    expected_version: Expected = None,
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        ev = service.get_event(db, sc, event_id)
        uid = ev.event_uid
        service.remove_event(db, actor.name, sc, ev, expected_version)
        _aud(db, actor, request, "scenario.event.remove", "SCENARIO", sc.id, {"event_uid": uid})
        return {"removed": uid, "scenario_version": sc.version, "scenario_status": sc.status.value}


@router.post(
    "/{scenario_id}/validate", responses=ERR, summary="Run the ten validation checks; READY when the scenario can run"
)
def validate(scenario_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        report = service.validate_scenario(db, sc, _limits(request).validation(), mark=True)
        _aud(
            db,
            actor,
            request,
            "scenario.validate",
            "SCENARIO",
            sc.id,
            {"valid": report["valid"], "errors": report["error_count"], "warnings": report["warning_count"]},
        )
        return report


@router.post(
    "/{scenario_id}/runs",
    status_code=202,
    responses={**ERR, 202: {"description": "Queued"}},
    summary="Queue a bounded simulation (idempotent with a key)",
)
def start_run(
    scenario_id: IdPath, tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    lim = _limits(request)
    with db.begin():
        sc = service.get_scenario(db, scenario_id)
        run, created = runs.create_run(db, sc, actor.name, lim, idempotency_key=idempotency_key)
        if created:
            _aud(
                db,
                actor,
                request,
                "scenario.run.queued",
                "SIMULATION_RUN",
                run.id,
                {"scenario_id": sc.id, "scenario_version": sc.version},
            )
        out = service.run_out(run, sc.name)
    if created:
        tasks.add_task(runs.execute_run, request_session_factory(request), out["id"], lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/{scenario_id}/runs", responses=ERR, summary="Run history for one scenario")
def scenario_runs(
    scenario_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, status: SimStatus | None = None
) -> dict[str, Any]:
    sc = service.get_scenario(db, scenario_id)
    q = select(SimulationRun).where(SimulationRun.scenario_id == scenario_id)
    if status:
        q = q.where(SimulationRun.status == status)
    return _page(db, q.order_by(SimulationRun.id.desc()), limit, offset, lambda r: service.run_out(r, sc.name))
