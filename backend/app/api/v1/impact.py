"""Cascading Impact Analysis API: create analyses from simulation runs, retrieve findings, paths, calculations, evidence,
criticality, alternatives and comparisons.

Read-only with respect to registry, relationship, capacity and order data: analyses write only ``impact_*`` tables (and
audit). All results are hypothetical, structural analysis. Data is limited to the caller's organisation, identity comes only from
the authenticated actor and request bodies reject unknown fields (including any client-supplied organisation id).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.impact import compare as comparer
from app.impact import config as cfg
from app.impact import runs, service
from app.models.enums import RegistryType, SimStatus
from app.models.impact import ImpactAlternative, ImpactAnalysis, ImpactCriticality, ImpactEdgeSnapshot, ImpactFinding
from app.models.scenario import Scenario, SimulationRun
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/impact", tags=["cascading impact analysis"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]


class AnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int = Field(ge=1, le=2**31 - 1)
    config: cfg.ImpactConfig | None = None


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Impact analysis write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _limits(request: Request) -> runs.AnalysisLimits:
    return runs.limits_from(request.app.state.settings)


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _analysis(db, analysis_id: int) -> ImpactAnalysis:  # noqa: ANN001
    a = db.get(ImpactAnalysis, analysis_id)
    if a is None:
        raise NotFoundError("Analysis not found.")
    return a


def _ready(a: ImpactAnalysis) -> ImpactAnalysis:
    if a.status in (SimStatus.QUEUED, SimStatus.RUNNING):
        raise DomainValidationError(f"Analysis {a.id} is {a.status.value}; results are available once it finishes.")
    if a.status in (SimStatus.FAILED, SimStatus.CANCELLED):
        raise DomainValidationError(f"Analysis {a.id} is {a.status.value}; no results were stored.")
    return a


def _scenario_name(db, scenario_id: int) -> str | None:  # noqa: ANN001
    sc = db.get(Scenario, scenario_id)
    return sc.name if sc else None


@router.get("/limits", summary="Algorithm version, server limits and analysis semantics")
def limits(request: Request, _a: Viewer) -> dict[str, Any]:
    lim = _limits(request)
    e = lim.engine
    return {
        "algorithm_version": cfg.ALGORITHM_VERSION, "max_depth": e.max_depth, "max_nodes": e.max_nodes, "max_edges": e.max_edges,
        "time_budget_seconds": e.time_budget_seconds, "max_path_steps": e.max_path_steps, "max_findings": e.max_findings,
        "max_active_analyses": lim.max_active, "client_overridable": False,
        "configurable": cfg.ImpactConfig.model_json_schema()["properties"], "defaults": cfg.ImpactConfig().model_dump(mode="json"),
        "scope": cfg.SCOPE_STATEMENT, "reachability_note": cfg.REACHABILITY_NOTE,
        "classifications": {
            "INITIATING": "Named target of the scenario (the initiating disruption).",
            "DIRECT": "Immediately dependent on a disrupted entity or relationship (one hop).",
            "PROPAGATED": "Reached through one or more intermediate entities (two or more hops).",
        },
        "impact_states": {
            "SCENARIO_ASSUMED": "The scenario's own assumption at an initiating entity.",
            "QUANTIFIED": "A share of recorded inbound supply computed from recorded supply shares.",
            "ASSUMPTION_DEPENDENT": "Computed, but depends on a stated assumption (overlapping events, no buffer).",
            "POTENTIAL_EXPOSURE": "A dependency path exists; the data does not support a quantity.",
            "INDETERMINATE": "Several events interact in a way that cannot be combined from the data.",
        },
        "disclaimer": service.DISCLAIMER,
    }  # fmt: skip


# --------------------------------------------------------------------------------------------------------- lifecycle


@router.post(
    "/analyses",
    status_code=202,
    responses={**ERR, 202: {"description": "Queued"}},
    summary="Queue a cascading analysis of a finished simulation run (idempotent with a key)",
)
def create(
    body: AnalysisCreate, tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    lim = _limits(request)
    with db.begin():
        run = db.get(SimulationRun, body.run_id)
        if run is None:
            raise NotFoundError("Simulation run not found.")
        a, created = runs.create_analysis(
            db, run, body.config or cfg.ImpactConfig(), actor.name, lim, idempotency_key=idempotency_key
        )
        if created:
            audit.record(
                db, actor, "impact.analysis.queued", target_type="IMPACT_ANALYSIS", target_id=a.id,
                details={"run_id": run.id, "scenario_id": run.scenario_id, "config_hash": a.config_hash}, request_id=request_id_of(request),
            )  # fmt: skip
        out = service.analysis_out(a, _scenario_name(db, a.scenario_id))
    if created:
        tasks.add_task(runs.execute_analysis, request_session_factory(request), out["id"], lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/analyses", summary="Analyses (newest first)")
def list_analyses(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, run_id: int | None = Query(default=None, ge=1, le=2**31 - 1),
    scenario_id: int | None = Query(default=None, ge=1, le=2**31 - 1), status: SimStatus | None = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(ImpactAnalysis)
    if run_id:
        q = q.where(ImpactAnalysis.run_id == run_id)
    if scenario_id:
        q = q.where(ImpactAnalysis.scenario_id == scenario_id)
    if status:
        q = q.where(ImpactAnalysis.status == status)
    names = dict(db.execute(select(Scenario.id, Scenario.name)).all())
    return _page(
        db,
        q.order_by(ImpactAnalysis.id.desc()),
        limit,
        offset,
        lambda a: service.analysis_out(a, names.get(a.scenario_id)),
    )


@router.get("/analyses/compare", responses=ERR, summary="Compare 2-5 completed or partial analyses")
def compare(db: DbSession, _a: Viewer, ids: Annotated[list[int], Query(min_length=2, max_length=5)]) -> dict[str, Any]:
    if any(i < 1 or i > 2**31 - 1 for i in ids):
        raise DomainValidationError("Analysis ids must be positive integers.")
    return comparer.compare_analyses(db, ids)


@router.get("/analyses/{analysis_id}", responses=ERR, summary="Status, versions, configuration, summary and safe error")
def get_analysis(analysis_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    a = _analysis(db, analysis_id)
    return service.analysis_out(a, _scenario_name(db, a.scenario_id), full=True)


@router.post(
    "/analyses/{analysis_id}/cancel", responses=ERR, summary="Cancel a queued analysis, or ask a running one to stop"
)
def cancel(analysis_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        a = _analysis(db, analysis_id)
        if a.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this analysis.")
        runs.cancel_analysis(db, a)
        audit.record(db, actor, "impact.analysis.cancel", target_type="IMPACT_ANALYSIS", target_id=a.id, details={"status": a.status.value}, request_id=request_id_of(request))  # fmt: skip
        return service.analysis_out(a)


# ----------------------------------------------------------------------------------------------------------- results


@router.get(
    "/analyses/{analysis_id}/summary", responses=ERR, summary="Headline counts, scope, coverage and uncertainty"
)
def summary(analysis_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    return {
        "analysis_id": a.id, "status": a.status.value, "summary": a.summary, "coverage": a.coverage, "uncertainty": a.uncertainty or [],
        "excluded": a.excluded or {}, "warnings": a.warnings or [], "truncated": a.truncated, "truncation_reasons": a.truncation_reasons or [],
        "graph_basis": a.graph_basis, "disclaimer": service.DISCLAIMER,
    }  # fmt: skip


@router.get("/analyses/{analysis_id}/events", responses=ERR, summary="Initiating events and what each one reaches")
def events(analysis_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    return {"analysis_id": a.id, "items": a.events or [], "total": len(a.events or [])}


_SORT = {"depth": (ImpactFinding.min_depth, ImpactFinding.entity_name), "name": (ImpactFinding.entity_name,), "coverage": (ImpactFinding.coverage, ImpactFinding.entity_name), "loss": (ImpactFinding.loss_pct.desc(), ImpactFinding.entity_name)}  # fmt: skip


@router.get("/analyses/{analysis_id}/findings", responses=ERR, summary="Impacted entities (filter, sort, paginate)")
def findings(
    analysis_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, entity_type: RegistryType | None = None,
    classification: Annotated[str | None, Query(pattern="^(INITIATING|DIRECT|PROPAGATED)$")] = None,
    impact_state: Annotated[str | None, Query(pattern="^(SCENARIO_ASSUMED|QUANTIFIED|ASSUMPTION_DEPENDENT|POTENTIAL_EXPOSURE|INDETERMINATE)$")] = None,
    min_depth: Annotated[int | None, Query(ge=0, le=6)] = None, max_depth: Annotated[int | None, Query(ge=0, le=6)] = None,
    event_uid: Annotated[str | None, Query(max_length=64)] = None,
    dimension: Annotated[str | None, Query(pattern="^[a-z_]{3,40}$")] = None,
    dimension_status: Annotated[str | None, Query(pattern="^(QUANTIFIED|LOWER_BOUND|QUALITATIVE|UNAVAILABLE|NOT_APPLICABLE)$")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None, sort: Annotated[str, Query(pattern="^(depth|name|coverage|loss)$")] = "depth",
) -> dict[str, Any]:  # fmt: skip
    a = _ready(_analysis(db, analysis_id))
    if dimension_status and not dimension:
        raise DomainValidationError("dimension_status requires dimension.")
    stmt = select(ImpactFinding).where(ImpactFinding.analysis_id == a.id)
    if entity_type:
        stmt = stmt.where(ImpactFinding.entity_type == entity_type)
    if classification:
        stmt = stmt.where(ImpactFinding.classification == classification)
    if impact_state:
        stmt = stmt.where(ImpactFinding.impact_state == impact_state)
    if min_depth is not None:
        stmt = stmt.where(ImpactFinding.min_depth >= min_depth)
    if max_depth is not None:
        stmt = stmt.where(ImpactFinding.min_depth <= max_depth)
    if q:
        like = f"%{q.strip().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(ImpactFinding.entity_name.ilike(like) | ImpactFinding.entity_code.ilike(like))
    stmt = stmt.order_by(*_SORT[sort], ImpactFinding.id)
    if dimension or event_uid:  # JSON-valued filters are applied on the (bounded) result set
        rows = list(db.scalars(stmt))
        if event_uid:
            rows = [f for f in rows if event_uid in (f.event_uids or [])]
        if dimension:
            rows = [
                f
                for f in rows
                if dimension in (f.dimensions or {})
                and (dimension_status is None or f.dimensions[dimension].get("status") == dimension_status)
            ]
        return {"items": [service.finding_out(f) for f in rows[offset : offset + limit]], "total": len(rows), "limit": limit, "offset": offset, "analysis_status": a.status.value, "complete": a.status is SimStatus.COMPLETED}  # fmt: skip
    out = _page(db, stmt, limit, offset, service.finding_out)
    out.update(analysis_status=a.status.value, complete=a.status is SimStatus.COMPLETED)
    return out


def _finding(db, a: ImpactAnalysis, etype: RegistryType, eid: int) -> ImpactFinding:  # noqa: ANN001
    f = db.scalar(
        select(ImpactFinding).where(
            ImpactFinding.analysis_id == a.id, ImpactFinding.entity_type == etype, ImpactFinding.entity_id == eid
        )
    )
    if f is None:
        raise NotFoundError(
            "This entity was not reached by the analysis within its scope. That does not mean it is safe."
        )
    return f


@router.get(
    "/analyses/{analysis_id}/findings/{entity_type}/{entity_id}",
    responses=ERR,
    summary="One finding with paths, per-event contributions, dimensions, evidence and assumptions",
)
def finding(
    analysis_id: IdPath, entity_type: RegistryType, entity_id: IdPath, db: DbSession, _a: Viewer
) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    out = service.finding_out(_finding(db, a, entity_type, entity_id), detail=True)
    out.update(analysis={"id": a.id, "algorithm_version": a.algorithm_version, "graph_hash": a.graph_hash, "run_id": a.run_id, "config_hash": a.config_hash}, disclaimer=service.DISCLAIMER)  # fmt: skip
    return out


@router.get(
    "/analyses/{analysis_id}/findings/{entity_type}/{entity_id}/paths",
    responses=ERR,
    summary="Stored propagation paths to one entity",
)
def finding_paths(
    analysis_id: IdPath, entity_type: RegistryType, entity_id: IdPath, db: DbSession, _a: Viewer
) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    f = _finding(db, a, entity_type, entity_id)
    return {"entity_type": entity_type.value, "entity_id": entity_id, "path_count": f.path_count, "paths_complete": f.paths_complete, "stored": len(f.paths or []), "paths": f.paths or [], "note": "Paths are bounded: at most the configured number is stored; path_count is the number found within the step cap (a lower bound when paths_complete is false)."}  # fmt: skip


@router.get(
    "/analyses/{analysis_id}/calculations",
    responses=ERR,
    summary="Quantified results with their assumptions and inputs",
)
def calculations(analysis_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    rows = [f for f in db.scalars(select(ImpactFinding).where(ImpactFinding.analysis_id == a.id).order_by(ImpactFinding.min_depth, ImpactFinding.entity_name, ImpactFinding.id))
            if any(d.get("status") in ("QUANTIFIED", "LOWER_BOUND") for k, d in (f.dimensions or {}).items() if k in ("supply_shortfall", "delivery_delay", "capacity_shortfall"))]  # fmt: skip
    items = [
        {
            "entity_type": f.entity_type.value, "entity_id": f.entity_id, "entity_name": f.entity_name, "impact_state": f.impact_state,
            "calculations": {k: v for k, v in (f.dimensions or {}).items() if k in ("supply_shortfall", "delivery_delay", "capacity_shortfall", "disruption_duration")},
            "combined": f.combined, "inputs": [{"ref": e["ref"], "supply_share_pct": e["supply_share_pct"], "lead_time_days": e["lead_time_days"], "capacity_value": e["capacity_value"], "capacity_unit": e["capacity_unit"]} for e in (f.evidence or [])],
            "assumptions": f.assumptions or [], "coverage": f.coverage,
        }
        for f in rows[offset : offset + limit]
    ]  # fmt: skip
    return {"items": items, "total": len(rows), "limit": limit, "offset": offset, "note": "Only dimensions with supporting data are listed. Percentages are shares of recorded inbound supply, not absolute quantities; no financial values exist."}  # fmt: skip


@router.get(
    "/analyses/{analysis_id}/evidence",
    responses=ERR,
    summary="Relationship records the analysis relied on (immutable snapshot)",
)
def evidence(
    analysis_id: IdPath,
    db: DbSession,
    _a: Viewer,
    limit: Lim = 50,
    offset: Off = 0,
    role: Annotated[str | None, Query(pattern="^(PROPAGATION|ALTERNATIVE|CONTEXT)$")] = None,
    edge_ref: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    stmt = select(ImpactEdgeSnapshot).where(ImpactEdgeSnapshot.analysis_id == a.id)
    if role:
        stmt = stmt.where(ImpactEdgeSnapshot.role == role)
    if edge_ref:
        stmt = stmt.where(ImpactEdgeSnapshot.edge_ref == edge_ref)
    out = _page(db, stmt.order_by(ImpactEdgeSnapshot.edge_ref), limit, offset, lambda s: {"edge_ref": s.edge_ref, "edge_type": s.edge_type, "source_type": s.source_type.value, "source_id": s.source_id, "destination_type": s.destination_type.value, "destination_id": s.destination_id, "status": s.status, "role": s.role, "version": s.version, "record": s.record})  # fmt: skip
    out["note"] = "Values as read when the analysis ran. The live relationship may have changed since."
    return out


@router.get(
    "/analyses/{analysis_id}/criticality",
    responses=ERR,
    summary="Structural dependency criticality (candidates, not confirmed bottlenecks)",
)
def criticality(
    analysis_id: IdPath,
    db: DbSession,
    _a: Viewer,
    limit: Lim = 50,
    offset: Off = 0,
    bottleneck_only: bool = False,
    role: Annotated[str | None, Query(pattern="^(INITIATING|INTERMEDIATE)$")] = None,
) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    stmt = select(ImpactCriticality).where(ImpactCriticality.analysis_id == a.id)
    if bottleneck_only:
        stmt = stmt.where(ImpactCriticality.bottleneck_candidate.is_(True))
    if role:
        stmt = stmt.where(ImpactCriticality.role == role)
    out = _page(
        db,
        stmt.order_by(
            ImpactCriticality.would_disconnect.desc(),
            ImpactCriticality.downstream_reachable.desc(),
            ImpactCriticality.id,
        ),
        limit,
        offset,
        service.crit_out,
    )
    out["note"] = (
        cfg.STRUCTURAL_CAVEAT
        + " Computed on the directed affected subgraph only; structural criticality is separate from operational severity and financial impact."
    )
    return out


@router.get(
    "/analyses/{analysis_id}/alternatives",
    responses=ERR,
    summary="Structurally possible alternate paths (analyst review only)",
)
def alternatives(
    analysis_id: IdPath,
    db: DbSession,
    _a: Viewer,
    limit: Lim = 50,
    offset: Off = 0,
    for_type: RegistryType | None = None,
    for_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    path_state: Annotated[str | None, Query(pattern="^(ACTIVE|PROPOSED|UNCERTAIN|STALE)$")] = None,
) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    stmt = select(ImpactAlternative).where(ImpactAlternative.analysis_id == a.id)
    if for_type:
        stmt = stmt.where(ImpactAlternative.for_type == for_type)
    if for_id:
        stmt = stmt.where(ImpactAlternative.for_id == for_id)
    if path_state:
        stmt = stmt.where(ImpactAlternative.path_state == path_state)
    out = _page(
        db,
        stmt.order_by(ImpactAlternative.for_type, ImpactAlternative.for_id, ImpactAlternative.id),
        limit,
        offset,
        service.alt_out,
    )
    out["notice"] = cfg.ALTERNATIVE_CAVEAT
    return out


@router.get("/analyses/{analysis_id}/graph", responses=ERR, summary="Bounded node/edge set for the tier view")
def graph(
    analysis_id: IdPath,
    db: DbSession,
    _a: Viewer,
    max_nodes: Annotated[int, Query(ge=1, le=200)] = 80,
    max_depth: Annotated[int | None, Query(ge=0, le=6)] = None,
    entity_type: RegistryType | None = None,
) -> dict[str, Any]:
    a = _ready(_analysis(db, analysis_id))
    stmt = select(ImpactFinding).where(ImpactFinding.analysis_id == a.id)
    if max_depth is not None:
        stmt = stmt.where(ImpactFinding.min_depth <= max_depth)
    if entity_type:
        stmt = stmt.where((ImpactFinding.entity_type == entity_type) | (ImpactFinding.classification == "INITIATING"))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    fs = list(
        db.scalars(stmt.order_by(ImpactFinding.min_depth, ImpactFinding.entity_name, ImpactFinding.id).limit(max_nodes))
    )
    keys = {(f.entity_type.value, f.entity_id) for f in fs}
    edges = []
    for s in db.scalars(
        select(ImpactEdgeSnapshot)
        .where(ImpactEdgeSnapshot.analysis_id == a.id, ImpactEdgeSnapshot.role == "PROPAGATION")
        .order_by(ImpactEdgeSnapshot.edge_ref)
    ):
        if (s.source_type.value, s.source_id) in keys and (s.destination_type.value, s.destination_id) in keys:
            edges.append({"edge_ref": s.edge_ref, "edge_type": s.edge_type, "source_type": s.source_type.value, "source_id": s.source_id, "destination_type": s.destination_type.value, "destination_id": s.destination_id, "supply_share_pct": (s.record or {}).get("supply_share_pct")})  # fmt: skip
    return {"nodes": [service.finding_out(f) for f in fs], "edges": edges, "total_nodes": total, "shown_nodes": len(fs), "truncated": total > len(fs), "note": "Bounded view: filter by depth or type to see more. The findings table is the complete equivalent."}  # fmt: skip
