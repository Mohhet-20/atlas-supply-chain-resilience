"""Resilience runs: validated, idempotent, bounded, cancellable and immutable once finished (QUEUED -> RUNNING -> terminal).

A run reads offers, scenarios and (optionally) inventory and writes only ``resilience_runs``. It stores the exact snapshot it saw, the request and the
algorithm version. Approval is a separate insert-only decision naming the chosen alternative. ATLAS never places an order.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.product import Product
from app.models.resilience import ResilienceDecision, ResilienceRun
from app.models.types import utcnow
from app.resilience import data, engine
from app.resilience.config import ALGORITHM_VERSION, STATEMENTS, Limits, ResilienceRequest
from app.resilience.plan import Budget

logger = logging.getLogger("atlas.resilience")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
SENSITIVE_KEYS = {
    "unit_price",
    "purchase",
    "freight",
    "shortage_penalty",
    "total_known",
    "budget_ceiling",
    "shortage_cost_per_unit",
    "price",
}


class _Cancelled(Exception):
    pass


def redact(obj: Any) -> Any:
    """Remove commercial figures (prices, cost totals, budget) and the COST objective's values for viewers."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        is_cost_row = obj.get("objective") == "COST" or obj.get("id") == "COST"
        for k, v in obj.items():
            if k in SENSITIVE_KEYS or k == "COST":
                out[k] = None
            elif is_cost_row and k in (
                "baseline",
                "candidate",
                "difference",
                "relative_pct",
                "ideal",
                "nadir",
                "best_value",
            ):
                out[k] = None
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def validate_refs(session: Session, req: ResilienceRequest, limits: Limits) -> None:
    if session.get(Product, req.product_id) is None:
        raise NotFoundError(f"Product {req.product_id} does not exist.")
    f = session.get(Facility, req.destination_facility_id)
    if f is None:
        raise NotFoundError(f"Facility {req.destination_facility_id} does not exist.")
    if f.merged_into_id is not None:
        raise ConflictError(f"The facility was merged into {f.merged_into_id}; use the surviving facility.")
    if len(req.stress) > limits.max_scenarios:
        raise DomainValidationError(f"At most {limits.max_scenarios} stress scenarios per run.")
    from app.models.supplier import Supplier
    from app.scenarios import service as scen

    for st in req.stress:
        if st.scenario_id is not None:
            sc = scen.get_scenario(session, st.scenario_id)
            if sc.status.value != "READY":
                raise ConflictError(
                    f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used."
                )
        if st.supplier_id is not None and session.get(Supplier, st.supplier_id) is None:
            raise DomainValidationError(f"Unknown supplier {st.supplier_id} in stress scenario '{st.name}'.")


def create(
    session: Session, req: ResilienceRequest, requested_by: str, limits: Limits, *, idempotency_key: str | None = None
) -> tuple[ResilienceRun, bool]:
    digest = hashlib.sha256(("RESILIENCE" + req.digest()).encode()).hexdigest()
    if idempotency_key:
        prior = session.scalar(select(ResilienceRun).where(ResilienceRun.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.request_hash) != (requested_by, digest):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    validate_refs(session, req, limits)
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(ResilienceRun).where(ResilienceRun.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = len(list(session.scalars(select(ResilienceRun.id).where(ResilienceRun.status.in_(ACTIVE)))))
    if active >= limits.max_active_runs:
        raise ConflictError(
            f"{active} resilience runs are already queued or running (limit {limits.max_active_runs}). Try again shortly."
        )
    run = ResilienceRun(
        run_uid=f"RO-{uuid.uuid4().hex[:16].upper()}", status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key,
        product_id=req.product_id, destination_facility_id=req.destination_facility_id, strategy=req.strategy, scenario_mode=req.scenario_mode,
        request=req.canonical(), request_hash=digest, algorithm_version=ALGORITHM_VERSION, queued_at=now,
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run, True


def cancel(run: ResilienceRun) -> ResilienceRun:
    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {run.status.value} resilience run cannot be cancelled.")
    return run


def compute(
    session: Session,
    req: ResilienceRequest,
    limits: Limits,
    *,
    stop: Any = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reads only. Returns the fields to store. With ``snapshot`` the stored inputs are reused (exact replay)."""
    now = req.as_of or utcnow()
    snap = snapshot if snapshot is not None else data.load(session, req, now, limits)
    res = engine.compute(snap, req, limits, stop=stop)
    res["statements"]["tenancy"] = STATEMENTS["tenancy"]
    return {"snapshot": snap, "snapshot_hash": snap["hash"], "result": res, "as_of": now.isoformat()}


def execute(factory: sessionmaker[Session], run_id: int, limits: Limits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(ResilienceRun).where(ResilienceRun.id == run_id, ResilienceRun.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        t0 = time.perf_counter()

        def stop() -> None:
            if time.perf_counter() - t0 > limits.time_budget_seconds:
                raise Budget
            if int((time.perf_counter() - t0) * 10) % 20 == 0 and session.scalar(
                select(ResilienceRun.cancel_requested).where(ResilienceRun.id == run_id)
            ):
                raise _Cancelled

        try:
            run = session.get(ResilienceRun, run_id)
            req = ResilienceRequest.model_validate(run.request)
            if session.scalar(select(ResilienceRun.cancel_requested).where(ResilienceRun.id == run_id)):
                raise _Cancelled
            if req.as_of is None:
                req = req.model_copy(update={"as_of": run.started_at})
            out = compute(session, req, limits, stop=stop)
            res = out["result"]
            session.refresh(run)
            if run.cancel_requested:
                raise _Cancelled
            run.snapshot, run.snapshot_hash = out["snapshot"], out["snapshot_hash"]
            run.result_status = res["result_status"]
            for k in ("objectives", "constraints", "scenarios", "alternatives", "pareto", "recommendation", "comparison", "sensitivity", "diagnostics", "explanation", "warnings", "solver", "statistics"):  # fmt: skip
                setattr(run, k, res.get(k))
            run.recommendation_hash = res.get("recommendation_hash")
            run.provenance = {"as_of": out["as_of"], "snapshot_hash": out["snapshot_hash"], "scenario_mode": req.scenario_mode, "statements": res["statements"], "inexact": res.get("inexact", False), "baseline_unchanged": True}  # fmt: skip
            run.reproducibility = {"request_hash": run.request_hash, "algorithm_version": ALGORITHM_VERSION, "snapshot_hash": out["snapshot_hash"], "recommendation_hash": run.recommendation_hash, "solver": res["solver"], "deterministic": True, "software": {"python": platform.python_version()}, "note": "The engine is a deterministic function of the stored snapshot and request; replaying them reproduces the result. A re-run against changed records has a different snapshot hash."}  # fmt: skip
            run.duration_ms = int((time.perf_counter() - t0) * 1000)
            run.finished_at = utcnow()
            rs = res["result_status"]
            run.status = (
                SimStatus.FAILED
                if rs == "failed"
                else SimStatus.PARTIAL
                if rs in ("timed_out", "insufficient_data", "invalid_input")
                else SimStatus.COMPLETED
            )
            session.commit()
        except _Cancelled:
            session.rollback()
            _finish(session, run_id, SimStatus.CANCELLED, "cancelled by request")
        except Budget:
            session.rollback()
            _finish(session, run_id, SimStatus.PARTIAL, "time budget exceeded", result_status="timed_out")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            logger.exception("resilience run failed", extra={"run": run_id})
            msg = (
                str(exc)[:200]
                if isinstance(exc, (ConflictError, NotFoundError, DomainValidationError, ValueError))
                else ""
            )
            _finish(
                session,
                run_id,
                SimStatus.FAILED,
                f"{type(exc).__name__}" + (f": {msg}" if msg else ""),
                result_status="failed",
            )
    finally:
        session.close()


def _finish(session: Session, run_id: int, status: SimStatus, err: str, result_status: str | None = None) -> None:
    vals: dict[str, Any] = {"status": status, "finished_at": utcnow(), "error": err[:300]}
    if result_status:
        vals["result_status"] = result_status
    session.execute(
        update(ResilienceRun)
        .where(ResilienceRun.id == run_id, ResilienceRun.status == SimStatus.RUNNING)
        .values(**vals)
    )
    session.commit()


def run_sync(
    session: Session, req: ResilienceRequest, requested_by: str, limits: Limits | None = None
) -> ResilienceRun:
    lim = limits or Limits()
    run, _ = create(session, req, requested_by, lim)
    rid = run.id
    session.commit()
    execute(sessionmaker(bind=session.get_bind(), expire_on_commit=False), rid, lim)
    session.expire_all()
    return session.get(ResilienceRun, rid)


def approval_state(session: Session, run: ResilienceRun) -> dict[str, Any]:
    last = session.scalar(
        select(ResilienceDecision).where(ResilienceDecision.run_id == run.id).order_by(ResilienceDecision.id.desc())
    )
    if last is None:
        return {
            "state": "PROPOSED",
            "decided_by": None,
            "decided_at": None,
            "rationale": None,
            "alternative_id": None,
            "executed": False,
        }
    return {"state": last.decision, "decided_by": last.decided_by, "decided_at": last.decided_at.isoformat(), "rationale": last.rationale, "alternative_id": last.alternative_id, "executed": False}  # fmt: skip


def decide(
    session: Session,
    run: ResilienceRun,
    decision: str,
    rationale: str,
    alternative_id: str | None,
    actor_name: str,
    is_admin: bool,
) -> ResilienceDecision:
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError(f"A {run.status.value} run cannot be approved or rejected.")
    cur = approval_state(session, run)
    if cur["state"] == decision and cur["alternative_id"] == alternative_id:
        raise ConflictError(f"The run is already {decision}.")
    if decision == "APPROVED":
        if run.result_status not in ("completed", "completed_with_warnings") or run.status is not SimStatus.COMPLETED:
            raise ConflictError(f"Only a completed run can be approved; this run is '{run.result_status}'. An infeasible, timed-out or incomplete result is never approved.")  # fmt: skip
        ids = {a["id"] for a in (run.alternatives or [])}
        if alternative_id is None or alternative_id not in ids:
            raise DomainValidationError("Name one of this run's alternatives (alternative_id) to approve.")
        if run.requested_by == actor_name and not is_admin:
            raise ConflictError("The requester cannot approve their own run; another reviewer must decide.")
    elif alternative_id is not None and alternative_id not in {a["id"] for a in (run.alternatives or [])}:
        raise DomainValidationError("Unknown alternative_id for this run.")
    d = ResilienceDecision(run_id=run.id, decision=decision, alternative_id=alternative_id, rationale=rationale, decided_by=actor_name, decided_at=utcnow(), recommendation_hash=run.recommendation_hash or "")  # fmt: skip
    session.add(d)
    session.flush()
    return d


def replay(session: Session, run: ResilienceRun, limits: Limits) -> dict[str, Any]:
    """Recompute from the STORED snapshot (exact) and separately report whether live records have since changed."""
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError("Only finished runs can be replayed.")
    req = ResilienceRequest.model_validate(run.request)
    if req.as_of is None:
        req = req.model_copy(update={"as_of": run.started_at})
    res = compute(session, req, limits, snapshot=json.loads(json.dumps(run.snapshot)))["result"]
    live = compute(session, req, limits)
    return {
        "run_uid": run.run_uid, "reproduced_from_stored_snapshot": res.get("recommendation_hash") == run.recommendation_hash,
        "stored_recommendation_hash": run.recommendation_hash, "replayed_recommendation_hash": res.get("recommendation_hash"),
        "live_inputs_unchanged": live["snapshot_hash"] == run.snapshot_hash, "stored_status": run.result_status, "replayed_status": res["result_status"],
        "explanation": "The stored snapshot reproduces the stored result exactly." if res.get("recommendation_hash") == run.recommendation_hash else "The stored snapshot did not reproduce the stored result; the algorithm version differs from the one recorded.",  # fmt: skip
        "live_note": "Re-loading the registered records as of the run time gives identical inputs." if live["snapshot_hash"] == run.snapshot_hash else "Re-loading the registered records as of the run time gives different inputs; a new run would differ. The stored run is unchanged.",  # fmt: skip
    }  # fmt: skip


def sensitivity_now(run: ResilienceRun, cfg: dict[str, Any], limits: Limits) -> dict[str, Any]:
    """Bounded on-demand sensitivity from the stored snapshot. Nothing is persisted; the run is immutable."""
    req = ResilienceRequest.model_validate({**run.request, "sensitivity": {**cfg, "enabled": True}})
    if req.as_of is None:
        req = req.model_copy(update={"as_of": run.started_at})
    t0 = time.perf_counter()

    def stop() -> None:
        if time.perf_counter() - t0 > limits.time_budget_seconds:
            raise Budget

    try:
        res = engine.compute(json.loads(json.dumps(run.snapshot)), req, limits, stop=stop)
    except Budget:
        return {"result_status": "timed_out", "sensitivity": None, "note": "The time budget ran out; narrow the sweep."}
    return {
        "result_status": res["result_status"],
        "sensitivity": res.get("sensitivity"),
        "note": "Computed now from the stored snapshot; not stored.",
    }
