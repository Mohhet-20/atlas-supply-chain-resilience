"""Routing runs: validated, idempotent, bounded, cancellable and immutable once finished (QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED).

A run reads lanes, delivery history, risk assessments and (optionally) a scenario, writes only ``routing_runs`` and never changes
the registered network. It stores the network content hash, the scenario definition hash, the request and the algorithm version so
the result can be traced and replayed.
"""

from __future__ import annotations

import logging
import platform
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.transport import RoutingRun, TransportLane
from app.models.types import utcnow
from app.routing import data, overlay
from app.routing import engine as eng
from app.routing.config import ALGORITHM_VERSION, STATEMENTS, Limits, RouteCheck, RouteRequest
from app.routing.network import Network, load_network

logger = logging.getLogger("atlas.routing")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


def _lane_uids(req: RouteRequest | RouteCheck) -> set[str]:
    c = req.constraints
    uids = set(req.lane_closures) | {a.lane_uid for a in req.lane_adjustments} | set(c.excluded_lanes)
    if isinstance(req, RouteCheck):
        uids |= set(req.lane_uids)
    return uids


def validate_refs(session: Session, req: RouteRequest | RouteCheck) -> None:
    """Reject a request that points at things that do not exist, before anything is queued."""
    if isinstance(req, RouteRequest):
        for label, fid in (("origin", req.origin_facility_id), ("destination", req.destination_facility_id)):
            f = session.get(Facility, fid)
            if f is None:
                raise NotFoundError(f"The {label} facility {fid} does not exist.")
            if f.merged_into_id is not None:
                raise ConflictError(
                    f"The {label} facility {fid} was merged into {f.merged_into_id}; use the surviving facility."
                )
    uids = _lane_uids(req)
    if uids:
        known = set(session.scalars(select(TransportLane.lane_uid).where(TransportLane.lane_uid.in_(uids))))
        if uids - known:
            raise DomainValidationError(f"Unknown lanes: {', '.join(sorted(uids - known))}.")
    if req.scenario_id is not None:
        from app.scenarios import service as scen

        sc = scen.get_scenario(session, req.scenario_id)
        if sc.status.value != "READY":
            raise ConflictError(
                f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used for routing."
            )


def create(
    session: Session, req: RouteRequest, requested_by: str, limits: Limits, *, idempotency_key: str | None = None
) -> tuple[RoutingRun, bool]:
    digest = req.digest()
    if idempotency_key:
        prior = session.scalar(select(RoutingRun).where(RoutingRun.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.request_hash) != (requested_by, digest):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    validate_refs(session, req)
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(RoutingRun).where(RoutingRun.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = len(list(session.scalars(select(RoutingRun.id).where(RoutingRun.status.in_(ACTIVE)))))
    if active >= limits.max_active_runs:
        raise ConflictError(
            f"{active} routing runs are already queued or running (limit {limits.max_active_runs}). Try again shortly."
        )
    run = RoutingRun(
        run_uid=f"RR-{uuid.uuid4().hex[:16].upper()}", kind="OPTIMIZE", status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key,
        origin_facility_id=req.origin_facility_id, destination_facility_id=req.destination_facility_id, scenario_id=req.scenario_id,
        request=req.canonical(), request_hash=digest, algorithm_version=ALGORITHM_VERSION, queued_at=now,
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run, True


def cancel(run: RoutingRun) -> RoutingRun:
    from app.core.errors import InvalidTransitionError

    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {run.status.value} routing run cannot be cancelled.")
    return run


def compute(session: Session, req: RouteRequest, limits: Limits, *, stop: Any = None) -> dict[str, Any]:
    """Pure with respect to the database: reads only. Returns the fields to store on the run."""
    now = req.as_of or utcnow()
    base = load_network(session, now, max_lanes=limits.max_lanes, stale_days=limits.stale_days)
    ctx, warns = data.build_context(session, base, use_measured=req.use_measured_reliability, min_n=limits.min_reliability_sample, max_rows=limits.max_delivery_rows, now=now)  # fmt: skip
    disruptions: list[overlay.Disruption] = []
    skipped: list[dict[str, Any]] = []
    scen_info = None
    if req.scenario_id is not None:
        d, skipped, scen_info = overlay.from_scenario(session, req.scenario_id)
        disruptions += d
    disruptions += overlay.from_request(req.lane_closures, req.lane_adjustments)
    baseline = eng.solve(base, req, ctx, limits, stop=stop)
    applied: list[dict[str, Any]] = []
    comparison = None
    result = baseline
    if disruptions:
        net2, applied = overlay.apply(base, disruptions)
        result = eng.solve(net2, req, ctx, limits, stop=stop)
        comparison = eng.compare(baseline, result, net2, ctx, req.constraints)
    prov_mix: dict[str, int] = {}
    for ln in base.lanes.values():
        prov_mix[ln.provenance] = prov_mix.get(ln.provenance, 0) + 1
    warnings = [*warns, *result["warnings"]]
    if skipped:
        warnings.append(
            f"{len(skipped)} scenario event(s) could not be mapped to transport lanes and were not applied (see disruptions.not_applied)."
        )
    if scen_info is not None:
        warnings.append(
            "Scenario event timing windows are not modelled: every applied event is treated as in force for the whole movement."
        )
    if base.issues:
        warnings.append(f"The lane data has {len(base.issues)} data-quality finding(s); see network issues.")
    if prov_mix.get("SYNTHETIC_DEMO"):
        warnings.append(
            "The network includes synthetic demonstration lanes: results verify the software, not real transport options."
        )
    ohash = overlay.overlay_hash(applied, skipped)
    return {
        "result_status": result["status"], "network_hash": base.digest(), "network_summary": base.summary(),
        "routes": result["routes"], "baseline_routes": baseline["routes"] if disruptions else None, "comparison": comparison,
        "rejections": {**result["rejections"], "infeasibility": result.get("infeasibility")},
        "disruptions": {"scenario": scen_info, "applied": applied, "not_applied": skipped, "overlay_hash": ohash, "baseline_unchanged": True} if disruptions or skipped else None,  # fmt: skip
        "summary": {k: result[k] for k in ("status", "objective", "weights", "ranking_currency", "route_count", "unverifiable", "pareto_front", "references", "search", "filters", "statements")} | ({"baseline": {k: baseline[k] for k in ("status", "route_count", "search")}} if disruptions else {}),  # fmt: skip
        "warnings": warnings,
        "provenance": {"lane_provenance": dict(sorted(prov_mix.items())), "as_of": now.isoformat(), "scenario": scen_info, "overlay_hash": ohash, "reliability_sources": sorted({r.source for r in ctx.rel.values()}), "network_issues": base.issues[:50], "excluded_lanes": base.excluded[:50]},  # fmt: skip
        "scenario_hash": None if scen_info is None else scen_info["definition_hash"],
    }  # fmt: skip


def execute(factory: sessionmaker[Session], run_id: int, limits: Limits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(RoutingRun).where(RoutingRun.id == run_id, RoutingRun.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        t0 = time.monotonic()

        def stop() -> None:
            if time.monotonic() - t0 > limits.time_budget_seconds:
                raise _Budget

        try:
            run = session.get(RoutingRun, run_id)
            req = RouteRequest.model_validate(run.request)
            if session.scalar(select(RoutingRun.cancel_requested).where(RoutingRun.id == run_id)):
                raise _Cancelled
            if req.as_of is None:
                req = req.model_copy(update={"as_of": run.started_at})
            res = compute(session, req, limits, stop=stop)
            session.refresh(run)
            if run.cancel_requested:
                raise _Cancelled
            for k in ("network_hash", "network_summary", "routes", "baseline_routes", "comparison", "rejections", "disruptions", "summary", "warnings", "provenance", "scenario_hash"):  # fmt: skip
                setattr(run, k, res[k])
            run.result_status = res["result_status"]
            run.reproducibility = {
                "request_hash": run.request_hash,
                "algorithm_version": ALGORITHM_VERSION,
                "network_hash": res["network_hash"],
                "scenario_hash": res["scenario_hash"],
                "overlay_hash": (res["disruptions"] or {}).get("overlay_hash"),
                "as_of": req.as_of.isoformat() if req.as_of else None,
                "software": {"python": platform.python_version()},
                "note": "Replaying the same request at the same as-of time against an unchanged network reproduces these routes. Lane rows are immutable, so a changed network has a different hash.",  # fmt: skip
            }
            run.duration_ms = int((time.monotonic() - t0) * 1000)
            run.finished_at = utcnow()
            run.status = SimStatus.PARTIAL if (res["summary"]["search"]["truncated"]) else SimStatus.COMPLETED
            session.commit()
        except _Cancelled:
            session.rollback()
            _finish(session, run_id, SimStatus.CANCELLED, "cancelled by request")
        except _Budget:
            session.rollback()
            _finish(session, run_id, SimStatus.FAILED, "compute budget exceeded")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            logger.exception("routing run failed", extra={"run": run_id})
            msg = (
                str(exc)[:200]
                if isinstance(exc, (ConflictError, NotFoundError, DomainValidationError, ValueError))
                else ""
            )
            _finish(session, run_id, SimStatus.FAILED, f"{type(exc).__name__}" + (f": {msg}" if msg else ""))
    finally:
        session.close()


def _finish(session: Session, run_id: int, status: SimStatus, err: str) -> None:
    session.execute(update(RoutingRun).where(RoutingRun.id == run_id, RoutingRun.status == SimStatus.RUNNING).values(status=status, finished_at=utcnow(), error=err[:300]))  # fmt: skip
    session.commit()


def run_sync(session: Session, req: RouteRequest, requested_by: str, limits: Limits | None = None) -> RoutingRun:
    lim = limits or Limits()
    run, _ = create(session, req, requested_by, lim)
    rid = run.id
    session.commit()
    execute(sessionmaker(bind=session.get_bind(), expire_on_commit=False), rid, lim)
    session.expire_all()
    return session.get(RoutingRun, rid)


def check_now(session: Session, body: RouteCheck, requested_by: str, limits: Limits) -> RoutingRun:
    """Validate one proposed route synchronously (cheap and bounded) and keep the outcome as a completed FEASIBILITY run."""
    validate_refs(session, body)
    now = body.as_of or utcnow()
    base = load_network(session, now, max_lanes=limits.max_lanes, stale_days=limits.stale_days)
    ctx, warns = data.build_context(session, base, use_measured=body.use_measured_reliability, min_n=limits.min_reliability_sample, max_rows=limits.max_delivery_rows, now=now)  # fmt: skip
    ctx.transfer_hours = body.transfer_hours
    disruptions: list[overlay.Disruption] = []
    skipped: list[dict[str, Any]] = []
    info = None
    if body.scenario_id is not None:
        disruptions, skipped, info = overlay.from_scenario(session, body.scenario_id)
    disruptions += overlay.from_request(body.lane_closures, body.lane_adjustments)
    net, applied = overlay.apply(base, disruptions) if disruptions else (base, [])
    res = eng.check_route(net, body.lane_uids, body.constraints, ctx)
    rows = {
        r.lane_uid: r for r in session.scalars(select(TransportLane).where(TransportLane.lane_uid.in_(body.lane_uids)))
    }
    first, last = rows.get(body.lane_uids[0]), rows.get(body.lane_uids[-1])
    t = utcnow()
    run = RoutingRun(
        run_uid=f"RR-{uuid.uuid4().hex[:16].upper()}", kind="FEASIBILITY", status=SimStatus.COMPLETED, result_status=res["classification"].lower(),
        requested_by=requested_by, origin_facility_id=first.origin_facility_id if first else 0, destination_facility_id=last.destination_facility_id if last else 0,
        scenario_id=body.scenario_id, scenario_hash=None if info is None else info["definition_hash"], request=body.model_dump(mode="json"),
        request_hash=__import__("hashlib").sha256(body.model_dump_json().encode()).hexdigest(), network_hash=base.digest(), network_summary=base.summary(),
        algorithm_version=ALGORITHM_VERSION, routes=[res], disruptions={"scenario": info, "applied": applied, "not_applied": skipped} if (disruptions or skipped) else None,
        summary={"classification": res["classification"], "statements": [STATEMENTS["decision_support"]]}, warnings=warns,
        provenance={"as_of": now.isoformat(), "lane_provenance": sorted({ln.provenance for ln in base.lanes.values()})},
        reproducibility={"algorithm_version": ALGORITHM_VERSION, "network_hash": base.digest()}, queued_at=t, started_at=t, finished_at=t, duration_ms=0,
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run


def replay(session: Session, run: RoutingRun, limits: Limits) -> dict[str, Any]:
    """Recompute an OPTIMIZE run from its stored request and compare it with what was stored."""
    if run.kind != "OPTIMIZE" or run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError("Only finished optimization runs can be replayed.")
    req = RouteRequest.model_validate(run.request)
    if req.as_of is None:
        req = req.model_copy(update={"as_of": run.started_at})
    res = compute(session, req, limits)
    same_net = res["network_hash"] == run.network_hash
    stored = [r["metrics"]["lane_uids"] for r in (run.routes or [])]
    now = [r["metrics"]["lane_uids"] for r in res["routes"]]
    return {
        "run_uid": run.run_uid,
        "network_unchanged": same_net,
        "routes_identical": stored == now,
        "stored_routes": stored,
        "replayed_routes": now,
        "explanation": "Identical routes are expected whenever the network is unchanged."
        if same_net
        else "The registered lanes have changed since this run, so a different result is expected.",  # fmt: skip
    }


def now_utc() -> datetime:
    return utcnow()


def as_network(session: Session, limits: Limits, as_of: datetime | None = None) -> Network:
    return load_network(session, as_of or utcnow(), max_lanes=limits.max_lanes, stale_days=limits.stale_days)
