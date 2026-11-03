"""Route Optimization & Transportation Intelligence API.

Registers transport lanes between facilities, validates the network, optimizes routes under hard constraints and an objective,
validates a proposed route, compares baseline with a disruption scenario and keeps every run as an immutable, traceable record.
It never books, tenders or contacts a carrier, and it never changes the registered network when a scenario is applied. Data is limited to the caller's organisation;
bodies reject unknown fields (including any organisation id) and within it, routing data is visible to every user
at or above the endpoint role. Per-lane cost figures are reserved for editors and above.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.transport import LANE_STATUSES, MODES, RoutingRun, TransportLane
from app.registry import audit
from app.registry.audit import request_id_of
from app.routing import config as C
from app.routing import lanes as L
from app.routing import runs as R
from app.routing.config import RouteCheck, RouteRequest
from app.routing.network import load_network
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/routing", tags=["route optimization and transportation intelligence"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
UidPath = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
IdemKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
]


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["ACTIVE", "SUSPENDED", "RETIRED"]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Routing write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _limits(request: Request) -> C.Limits:
    s = request.app.state.settings
    return C.Limits(
        max_lanes=s.routing_max_lanes, max_active_runs=s.routing_max_active_runs, time_budget_seconds=s.routing_time_budget_seconds,
        max_expansions=s.routing_max_expansions, min_reliability_sample=s.routing_min_reliability_sample,
        max_delivery_rows=s.routing_max_delivery_rows, stale_days=s.routing_stale_days,
    )  # fmt: skip


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _parse(model, body: dict[str, Any]):  # noqa: ANN001, ANN202
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


def _lane(r: TransportLane, actor: Actor) -> dict[str, Any]:
    out = L.out(r)
    if actor.role < Role.EDITOR:
        out["cost_amount"], out["cost_redacted"] = None, True
    return out


def _run_get(db, uid: str) -> RoutingRun:  # noqa: ANN001
    r = db.scalar(select(RoutingRun).where(RoutingRun.run_uid == uid))
    if r is None:
        raise NotFoundError("Routing run not found.")
    return r


def _run(r: RoutingRun, full: bool = False) -> dict[str, Any]:
    out = {
        "run_uid": r.run_uid, "kind": r.kind, "status": r.status.value, "result_status": r.result_status, "requested_by": r.requested_by,
        "origin_facility_id": r.origin_facility_id, "destination_facility_id": r.destination_facility_id, "scenario_id": r.scenario_id,
        "objective": (r.request or {}).get("objective"), "algorithm_version": r.algorithm_version, "network_hash": r.network_hash,
        "scenario_hash": r.scenario_hash, "queued_at": _iso(r.queued_at), "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
        "duration_ms": r.duration_ms, "error": r.error, "route_count": len(r.routes or []) if r.kind == "OPTIMIZE" else None,
        "best_route": None if not (r.routes and r.kind == "OPTIMIZE") else r.routes[0]["metrics"]["lane_uids"],
    }  # fmt: skip
    if full:
        out.update({k: getattr(r, k) for k in ("request", "request_hash", "network_summary", "routes", "baseline_routes", "comparison", "rejections", "disruptions", "summary", "warnings", "provenance", "reproducibility")})  # fmt: skip
        out["statements"] = [C.STATEMENTS["decision_support"], C.STATEMENTS["cost"], C.STATEMENTS["duration"], C.STATEMENTS["reliability"], C.STATEMENTS["risk"], C.STATEMENTS["tenancy"]]  # fmt: skip
    return out


def _coords(f: Facility) -> dict[str, Any] | None:
    if f.latitude is None or f.longitude is None:
        return None
    if not (-90 <= f.latitude <= 90 and -180 <= f.longitude <= 180) or (f.latitude == 0 and f.longitude == 0):
        return None
    return {"latitude": f.latitude, "longitude": f.longitude, "status": f.location_status.value}


# ------------------------------------------------------------------------------------------------ meta
@router.get("/health", summary="Capabilities, limits and what is not supported")
def health(request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    lim = _limits(request)
    lanes = db.scalar(select(func.count()).select_from(TransportLane)) or 0
    runs = dict(db.execute(select(RoutingRun.status, func.count()).group_by(RoutingRun.status)).all())
    return {
        "algorithm_version": C.ALGORITHM_VERSION, "write_api_enabled": request.app.state.settings.write_api_allowed, "lanes": lanes,
        "runs_by_status": {k.value: v for k, v in runs.items()}, "modes": list(MODES), "lane_statuses": list(LANE_STATUSES),
        "limits": {"max_lanes": lim.max_lanes, "max_active_runs": lim.max_active_runs, "time_budget_seconds": lim.time_budget_seconds, "max_expansions": lim.max_expansions, "max_hops": C.MAX_HOPS, "max_alternatives": 10, "min_reliability_sample": lim.min_reliability_sample, "max_bulk_lanes": request.app.state.settings.routing_max_bulk_lanes},  # fmt: skip
        "algorithms": ["Dijkstra (non-negative additive weights)", "reverse-distance lower bounds for safe pruning", "bounded exhaustive loopless route enumeration", "Yen k-shortest-paths (fallback when the search budget is reached)"],  # fmt: skip
        "not_supported": ["booking, tendering or carrier integration", "schedules, departure windows and time-expanded networks", "multi-commodity flow, vehicle routing and load consolidation", "exchange-rate conversion", "landed cost", "A* (no verified admissible geometric heuristic)", "automatic route execution"],  # fmt: skip
        "statements": C.STATEMENTS,
    }  # fmt: skip


@router.get("/objectives", summary="Objectives, their criteria, directions and weights")
def objectives(_a: Viewer) -> dict[str, Any]:
    return {
        "criteria": [
            {
                "name": k,
                "direction": C.DIRECTION[k],
                "unit": {
                    "cost": "currency of the lanes",
                    "duration": "hours",
                    "reliability": "probability 0-1",
                    "risk": "score 0-1",
                }[k],
            }
            for k in C.CRITERIA
        ],  # fmt: skip
        "objectives": [
            {"name": "LOWEST_COST", "weights": {"cost": 1.0}},
            {"name": "FASTEST", "weights": {"duration": 1.0}},
            {"name": "MOST_RELIABLE", "weights": {"reliability": 1.0}},
            {"name": "LOWEST_RISK", "weights": {"risk": 1.0}},
            {
                "name": "BALANCED",
                "weights": C.BALANCED_WEIGHTS,
                "note": "Risk is excluded to avoid counting delivery uncertainty twice; use CUSTOM to include it.",
            },
            {"name": "CUSTOM", "weights": "caller supplied; non-negative, finite, normalised to sum 1"},
        ],  # fmt: skip
        "normalisation": "Min-max over the feasible, scorable candidate routes: 0 is best and 1 is worst for each criterion. The scale depends on the candidate set, so raw values are always shown beside scores.",  # fmt: skip
        "tie_break": [
            "composite score",
            "total duration",
            "total cost",
            "fewer lanes",
            "lane id sequence (deterministic)",
        ],
    }


@router.get("/constraints", summary="Hard constraints and how each is judged")
def constraints(_a: Viewer) -> dict[str, Any]:
    return {
        "states": {
            "SATISFIED": "verified to hold",
            "VIOLATED": "verified not to hold; the route is rejected",
            "UNVERIFIABLE": "missing data prevents a verdict; the route is never presented as feasible",
        },  # fmt: skip
        "constraints": [
            {
                "name": "max_transit_hours / depart_at + deadline_at",
                "judged_on": "total transit plus transfer hours",
                "missing_data": "UNVERIFIABLE unless known lane times already exceed the limit",
            },  # fmt: skip
            {
                "name": "max_cost + currency",
                "judged_on": "sum of lane charges in that currency",
                "missing_data": "UNVERIFIABLE on missing, mixed or different currency (no FX)",
            },  # fmt: skip
            {
                "name": "min_capacity + capacity_unit",
                "judged_on": "smallest lane capacity in that unit",
                "missing_data": "UNVERIFIABLE",
            },
            {
                "name": "allowed_modes / required_modes / prohibited_modes",
                "judged_on": "lane modes",
                "missing_data": "exact",
            },
            {"name": "max_transfers", "judged_on": "intermediate facilities", "missing_data": "exact"},
            {
                "name": "excluded_lanes / excluded_facilities / required_facilities",
                "judged_on": "route structure",
                "missing_data": "exact",
            },
            {
                "name": "min_reliability",
                "judged_on": "product of lane on-time probabilities",
                "missing_data": "UNVERIFIABLE unless even a perfect unknown lane cannot reach the minimum",
            },  # fmt: skip
            {
                "name": "max_risk",
                "judged_on": "highest supplier-risk score on the route",
                "missing_data": "UNVERIFIABLE while any facility has no score",
            },
            {
                "name": "cargo_tags",
                "judged_on": "lane restriction tags TAG_PROHIBITED",
                "missing_data": "no recorded restriction is not proof none exists",
            },
            {
                "name": "max_data_age_days",
                "judged_on": "lane observation date",
                "missing_data": "UNVERIFIABLE for undated lanes",
            },
            {
                "name": "availability / as_of",
                "judged_on": "lane status and effective window",
                "missing_data": "unavailable lanes are excluded and listed",
            },
        ],  # fmt: skip
    }


# ------------------------------------------------------------------------------------------------ lanes
@router.post(
    "/lanes", status_code=201, responses=ERR, summary="Register a transport lane (figures are immutable once saved)"
)
def create_lane(body: dict[str, Any], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    lane_in = _parse(L.LaneIn, body)
    with db.begin():
        row = L.create(db, lane_in, actor.name)
        audit.record(db, actor, "routing.lane.create", target_type="TRANSPORT_LANE", target_id=row.id, details={"mode": row.mode, "origin": row.origin_facility_id, "destination": row.destination_facility_id}, request_id=request_id_of(request))  # fmt: skip
        return L.out(row)


@router.post("/lanes/bulk", status_code=201, responses=ERR, summary="Register several lanes atomically (all or none)")
def bulk_lanes(body: dict[str, Any], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    items = body.get("lanes") if isinstance(body, dict) and set(body) == {"lanes"} else None
    mx = request.app.state.settings.routing_max_bulk_lanes
    if not isinstance(items, list) or not 1 <= len(items) <= mx:
        raise DomainValidationError(f"Send {{'lanes': [...]}} with 1 to {mx} lanes.")
    parsed = []
    for i, it in enumerate(items):
        try:
            parsed.append(L.LaneIn.model_validate(it))
        except ValidationError as e:
            raise DomainValidationError(
                f"lanes[{i}]: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            ) from e
    with db.begin():
        rows = [L.create(db, p, actor.name) for p in parsed]
        audit.record(
            db,
            actor,
            "routing.lane.bulk_create",
            target_type="TRANSPORT_LANE",
            details={"count": len(rows)},
            request_id=request_id_of(request),
        )
        return {"created": len(rows), "lane_uids": [r.lane_uid for r in rows]}


@router.get("/lanes", summary="Lanes (newest first). Per-lane cost is shown to editors and above only")
def list_lanes(
    db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, origin: Annotated[int | None, Query(ge=1)] = None,
    destination: Annotated[int | None, Query(ge=1)] = None, mode: Annotated[str | None, Query(max_length=20)] = None,
    status: Annotated[str | None, Query(max_length=12)] = None,
) -> dict[str, Any]:  # fmt: skip
    if mode is not None and mode not in MODES:
        raise DomainValidationError(f"Unknown mode. Use: {', '.join(MODES)}.")
    if status is not None and status not in LANE_STATUSES:
        raise DomainValidationError(f"Unknown status. Use: {', '.join(LANE_STATUSES)}.")
    q = select(TransportLane)
    for col, v in ((TransportLane.origin_facility_id, origin), (TransportLane.destination_facility_id, destination), (TransportLane.mode, mode), (TransportLane.status, status)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(TransportLane.id.desc()), limit, offset, lambda r: _lane(r, actor))


@router.get("/lanes/{uid}", responses=ERR, summary="One lane")
def get_lane(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    return _lane(L.get(db, uid), actor)


@router.post("/lanes/{uid}/status", responses=ERR, summary="Suspend, reactivate or retire a lane (retired is final)")
def lane_status(uid: UidPath, body: StatusBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        row = L.get(db, uid)
        frm = row.status
        L.set_status(db, row, body.to)
        audit.record(db, actor, "routing.lane.status", target_type="TRANSPORT_LANE", target_id=row.id, details={"from": frm, "to": body.to}, request_id=request_id_of(request))  # fmt: skip
        return _lane(row, actor)


# ------------------------------------------------------------------------------------------------ network
@router.get("/network", summary="The traversable network with facility coordinates only where valid")
def network(request: Request, db: DbSession, actor: Viewer, as_of: Annotated[str | None, Query(max_length=40)] = None, max_lanes: Annotated[int, Query(ge=1, le=2000)] = 1000) -> dict[str, Any]:  # fmt: skip
    from datetime import datetime

    when = None
    if as_of:
        try:
            when = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError as e:
            raise DomainValidationError("as_of must be an ISO-8601 timestamp.") from e
        if when.tzinfo is None:
            raise DomainValidationError("as_of must include a timezone offset.")
    lim = _limits(request)
    net = load_network(db, when, max_lanes=lim.max_lanes, stale_days=lim.stale_days)
    lanes = sorted(net.lanes.values(), key=lambda x: x.id)
    used = {x for ln in lanes for x in (ln.origin, ln.dest)}
    facs = {f.id: f for f in db.scalars(select(Facility).where(Facility.id.in_(used)))} if used else {}
    redact = actor.role < Role.EDITOR
    return {
        "network_hash": net.digest(),
        "summary": net.summary(),
        "as_of": _iso(net.as_of),
        "truncated": len(lanes) > max_lanes,
        "nodes": [
            {
                "facility_id": f.id,
                "code": f.facility_code,
                "name": f.name,
                "country_or_region": f.country_or_region,
                "coordinates": _coords(f),
            }
            for f in sorted(facs.values(), key=lambda x: x.id)
        ],  # fmt: skip
        "lanes": [
            {
                "lane_uid": ln.uid,
                "origin_facility_id": ln.origin,
                "destination_facility_id": ln.dest,
                "mode": ln.mode,
                "distance_km": ln.distance_km,
                "duration_hours": ln.duration_hours,
                "cost": None if redact else ln.cost,
                "currency": None if redact else ln.currency,
                "capacity": ln.capacity,
                "capacity_unit": ln.capacity_unit,
                "provenance": ln.provenance,
                "flags": list(ln.flags),
            }
            for ln in lanes[:max_lanes]
        ],  # fmt: skip
        "excluded": net.excluded[:200],
        "issues": net.issues[:200],
        "statement": "The geographic view uses a facility's coordinates only when they are present and valid; otherwise only the schematic view is meaningful. Straight lines between facilities are not routes.",  # fmt: skip
    }


@router.get("/network/validation", summary="Data-quality findings about the lane data")
def network_validation(request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    lim = _limits(request)
    net = load_network(db, None, max_lanes=lim.max_lanes, stale_days=lim.stale_days)
    by: dict[str, int] = {}
    for i in net.issues:
        by[i["code"]] = by.get(i["code"], 0) + 1
    return {"network_hash": net.digest(), "lanes": len(net.lanes), "excluded": net.excluded[:500], "excluded_count": len(net.excluded), "by_code": dict(sorted(by.items())), "issues": net.issues[:500]}  # fmt: skip


# ------------------------------------------------------------------------------------------------ optimize / feasibility / runs
@router.post("/optimize", status_code=202, responses=ERR, summary="Queue a route optimization (idempotent with a key)")
def optimize(body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    req = _parse(RouteRequest, body)
    lim = _limits(request)
    with db.begin():
        run, created = R.create(db, req, actor.name, lim, idempotency_key=idempotency_key)
        if created:
            audit.record(db, actor, "routing.run.queued", target_type="ROUTING_RUN", target_id=run.id, details={"objective": req.objective, "origin": req.origin_facility_id, "destination": req.destination_facility_id, "scenario": req.scenario_id}, request_id=request_id_of(request))  # fmt: skip
        out, rid = _run(run), run.id
    if created:
        tasks.add_task(R.execute, request_session_factory(request), rid, lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.post(
    "/feasibility", responses=ERR, summary="Validate a proposed route (ordered lane uids) against the constraints"
)
def feasibility(body: dict[str, Any], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    chk = _parse(RouteCheck, body)
    with db.begin():
        run = R.check_now(db, chk, actor.name, _limits(request))
        audit.record(db, actor, "routing.feasibility", target_type="ROUTING_RUN", target_id=run.id, details={"classification": run.result_status, "lanes": len(chk.lane_uids)}, request_id=request_id_of(request))  # fmt: skip
        return _run(run, full=True)


@router.get("/runs", summary="Routing run history (newest first)")
def list_runs(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, status: SimStatus | None = None, kind: Literal["OPTIMIZE", "FEASIBILITY"] | None = None,
    origin: Annotated[int | None, Query(ge=1)] = None, destination: Annotated[int | None, Query(ge=1)] = None, scenario_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(RoutingRun)
    for col, v in ((RoutingRun.status, status), (RoutingRun.kind, kind), (RoutingRun.origin_facility_id, origin), (RoutingRun.destination_facility_id, destination), (RoutingRun.scenario_id, scenario_id)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(RoutingRun.id.desc()), limit, offset, _run)


@router.get("/runs/{uid}", responses=ERR, summary="One run with routes, rejections, provenance and reproducibility")
def get_run(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _run(_run_get(db, uid), full=True)


@router.post("/runs/{uid}/cancel", responses=ERR, summary="Cancel a queued run, or ask a running one to stop")
def cancel_run(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        R.cancel(r)
        audit.record(db, actor, "routing.run.cancel", target_type="ROUTING_RUN", target_id=r.id, details={"status": r.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _run(r)


def _finished(r: RoutingRun) -> RoutingRun:
    if r.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise DomainValidationError(f"The run is {r.status.value}; this section exists only for finished runs.")
    return r


@router.get("/runs/{uid}/routes", responses=ERR, summary="Ranked alternatives with trade-offs")
def run_routes(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    s = r.summary or {}
    return {"run": _run(r), "routes": r.routes, "references": s.get("references"), "pareto_front": s.get("pareto_front"), "unverifiable": s.get("unverifiable"), "rejections": r.rejections, "warnings": r.warnings}  # fmt: skip


@router.get(
    "/runs/{uid}/routes/{rank}", responses=ERR, summary="One ranked route with constraint results and explanation"
)
def run_route(uid: UidPath, rank: Annotated[int, Path(ge=1, le=10)], db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    for it in r.routes or []:
        if it.get("rank") == rank:
            return {
                "run_uid": r.run_uid,
                "route": it,
                "statements": [
                    C.STATEMENTS["cost"],
                    C.STATEMENTS["duration"],
                    C.STATEMENTS["reliability"],
                    C.STATEMENTS["risk"],
                ],
            }
    raise NotFoundError("No route with that rank in this run.")


@router.get(
    "/runs/{uid}/comparison",
    responses=ERR,
    summary="Baseline versus scenario (only for runs that applied a scenario or disruption)",
)
def run_comparison(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    if r.comparison is None:
        raise DomainValidationError(
            "This run applied no scenario or disruption, so there is nothing to compare with the baseline."
        )
    return {"run_uid": r.run_uid, "scenario_hash": r.scenario_hash, "network_hash": r.network_hash, "disruptions": r.disruptions, "comparison": r.comparison, "baseline_routes": r.baseline_routes, "scenario_routes": r.routes, "statement": C.STATEMENTS["scenario"]}  # fmt: skip


@router.get("/runs/{uid}/cost-breakdown", responses=ERR, summary="Per-lane charges of one route (editors and above)")
def cost_breakdown(
    uid: UidPath, db: DbSession, request: Request, _a: Editor, rank: Annotated[int, Query(ge=1, le=10)] = 1
) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    item = next((it for it in r.routes or [] if it.get("rank") == rank), None)
    if item is None:
        raise NotFoundError("No route with that rank in this run.")
    rows = {
        x.lane_uid: x
        for x in db.scalars(select(TransportLane).where(TransportLane.lane_uid.in_(item["metrics"]["lane_uids"])))
    }
    lines = []
    for u in item["metrics"]["lane_uids"]:
        x = rows.get(u)
        lines.append({"lane_uid": u, "mode": None if x is None else x.mode, "cost": None if x is None else x.cost_amount, "currency": None if x is None else x.currency, "cost_quality": None if x is None else x.cost_quality})  # fmt: skip
    return {"run_uid": r.run_uid, "rank": rank, "lines": lines, "total": item["metrics"]["cost"], "statement": C.STATEMENTS["cost"], "note": "Lane figures are the stored lane rows (immutable). A scenario adjustment, if any, is reflected in the route total but not in these stored lines."}  # fmt: skip


@router.post(
    "/runs/{uid}/replay",
    responses=ERR,
    summary="Recompute a finished run from its stored request and compare with the stored result",
)
def replay_run(uid: UidPath, db: DbSession, request: Request, _a: Editor) -> dict[str, Any]:
    return R.replay(db, _run_get(db, uid), _limits(request))
