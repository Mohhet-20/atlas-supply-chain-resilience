"""Replenishment runs: validated, idempotent, bounded, cancellable and immutable once finished (QUEUED -> RUNNING -> terminal).

A run reads inventory, demand, forecast, lead-time, offer, cost and scenario records and writes only ``replenishment_runs``. It stores the
exact inputs it saw (the snapshot and its hash), the request, the scenario definition hash and the algorithm version. Approval is a separate
insert-only decision. ATLAS never places an order: an approved recommendation is an internal record that a human decided to proceed elsewhere.
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
from app.invopt import data, engine, overlay
from app.invopt.config import ALGORITHM_VERSION, STATEMENTS, Limits, ReplenishRequest, stable_hash
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.invopt import ReplenishmentDecision, ReplenishmentRun
from app.models.product import Product
from app.models.types import utcnow

logger = logging.getLogger("atlas.invopt")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
SENSITIVE = {"unit_cost", "purchase_spend", "order_purchase_spend", "holding_cost_per_unit_day", "ordering_cost", "shortage_cost_per_unit", "holding", "ordering", "shortage", "known_subtotal", "unit_price", "storage_capacity_cost"}  # fmt: skip
COST_BLOCKS = ("costs",)


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


def redact(obj: Any, _top: bool = True) -> Any:
    """Remove commercial figures (unit cost, order cost, holding and shortage cost, spend and every total derived from them) for viewers."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SENSITIVE:
                out[k] = None
            elif k == "values" and isinstance(v, dict) and "unit_cost" in v:
                out[k] = {kk: None for kk in v}
            else:
                out[k] = redact(v, False)
        if "redacted" not in out and _top:
            out["_redacted_for_role"] = True
        return out
    if isinstance(obj, list):
        return [redact(x, False) for x in obj]
    return obj


def validate_refs(session: Session, req: ReplenishRequest) -> None:
    p = session.get(Product, req.product_id)
    if p is None:
        raise NotFoundError(f"Product {req.product_id} does not exist.")
    if p.merged_into_id is not None:
        raise ConflictError(f"Product {req.product_id} was merged into {p.merged_into_id}; use the surviving product.")
    f = session.get(Facility, req.facility_id)
    if f is None:
        raise NotFoundError(f"Facility {req.facility_id} does not exist.")
    if f.merged_into_id is not None:
        raise ConflictError(f"The facility was merged into {f.merged_into_id}; use the surviving facility.")
    if req.forecast_run_id is not None:
        from app.forecast import integration as fint

        fint.validate_selectable(session, req.forecast_run_id)
    if req.scenario_id is not None:
        from app.scenarios import service as scen

        sc = scen.get_scenario(session, req.scenario_id)
        if sc.status.value != "READY":
            raise ConflictError(f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used.")
    if req.supplier_id is not None:
        from app.models.supplier import Supplier

        if session.get(Supplier, req.supplier_id) is None:
            raise DomainValidationError(f"Unknown supplier {req.supplier_id}.")


def create(session: Session, req: ReplenishRequest, requested_by: str, limits: Limits, *, kind: str = "OPTIMIZE", idempotency_key: str | None = None) -> tuple[ReplenishmentRun, bool]:  # fmt: skip
    if kind == "SIMULATE" and req.proposed is None:
        raise DomainValidationError("A simulation run needs a proposed policy to evaluate.")
    if kind == "OPTIMIZE" and req.proposed is not None:
        raise DomainValidationError("A proposed policy is evaluated by a simulation run, not an optimisation run.")
    digest = hashlib.sha256((kind + req.digest()).encode()).hexdigest()
    if idempotency_key:
        prior = session.scalar(select(ReplenishmentRun).where(ReplenishmentRun.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.request_hash) != (requested_by, digest):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    validate_refs(session, req)
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(ReplenishmentRun).where(ReplenishmentRun.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = len(list(session.scalars(select(ReplenishmentRun.id).where(ReplenishmentRun.status.in_(ACTIVE)))))
    if active >= limits.max_active_runs:
        raise ConflictError(
            f"{active} replenishment runs are already queued or running (limit {limits.max_active_runs}). Try again shortly."
        )
    run = ReplenishmentRun(
        run_uid=f"RR-{uuid.uuid4().hex[:16].upper()}", kind=kind, status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key,
        product_id=req.product_id, facility_id=req.facility_id, scenario_id=req.scenario_id, forecast_run_id=req.forecast_run_id, request=req.canonical(),
        request_hash=digest, algorithm_version=ALGORITHM_VERSION, queued_at=now,
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run, True


def cancel(run: ReplenishmentRun) -> ReplenishmentRun:
    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {run.status.value} replenishment run cannot be cancelled.")
    return run


def compute(
    session: Session, req: ReplenishRequest, limits: Limits, *, stop: Any = None, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Reads only. Returns the fields to store on the run. With ``snapshot`` the stored inputs are reused (exact replay)."""
    now = req.as_of or utcnow()
    snap = snapshot
    if snap is None:
        snap = data.load(session, req, now, limits)
        scenario = None
        if req.scenario_id is not None:
            sups = {
                x for x in (snap["lead_time"].get("supplier_id"), (snap.get("offer") or {}).get("supplier_id")) if x
            }
            sups |= {r["supplier_id"] for r in snap["receipts"]["counted"] if r.get("supplier_id")}
            scenario = overlay.from_scenario(session, req.scenario_id, req.facility_id, sups)
            if not sups:
                scenario["not_applied"].append({"source": "scenario", "event_type": "ALL", "reason": "No supplier is known to serve this product at this facility (no offer, supplier-specific lead time or dated receipt), so supplier events cannot be mapped."})  # fmt: skip
        snap["scenario"] = scenario
        snap["hash"] = stable_hash({k: v for k, v in snap.items() if k not in ("data_quality", "hash")})
    res = engine.compute(snap, req, limits, stop=stop)
    sc = snap.get("scenario")
    hypothetical = bool(sc and sc.get("what_ifs")) or not req.what_if.is_neutral() or req.proposed is not None
    unmapped = (sc or {}).get("not_applied", [])
    if unmapped:
        res["warnings"].append({"code": "SCENARIO_EVENTS_NOT_APPLIED", "severity": "WARNING", "message": f"{len(unmapped)} scenario event(s) could not be mapped to inventory inputs and were not applied (see provenance.scenario.not_applied)."})  # fmt: skip
    if sc and sc.get("what_ifs"):
        res["warnings"].append({"code": "SCENARIO_WINDOWS_APPROXIMATED", "severity": "INFO", "message": "Overlapping scenario events are combined into one blocked window; event timing detail is not modelled."})  # fmt: skip
    prov = {"as_of": now.isoformat(), "snapshot_hash": snap["hash"], "scenario": sc, "hypothetical": hypothetical, "lead_time_source": snap["lead_time"].get("source"), "lead_time_provenance": snap["lead_time"].get("provenance"), "demand_source": res["demand"]["source"] if res.get("demand") else None, "forecast": (res.get("demand") or {}).get("forecast"), "cost_provenance": snap["costs"]["provenance"], "offer": None if not snap.get("offer") else {k: snap["offer"].get(k) for k in ("offer_uid", "supplier_id", "eligible", "provenance")}, "baseline_unchanged": True}  # fmt: skip
    rec_hash = stable_hash(
        {
            "recommendation": res.get("recommendation"),
            "safety_stock": (res.get("safety_stock") or {}).get("value"),
            "status": res["result_status"],
        }
    )
    res["statements"]["tenancy"] = STATEMENTS["tenancy"]
    return {
        "result_status": res["result_status"], "snapshot": snap, "snapshot_hash": snap["hash"], "position": res["position"], "demand": res["demand"],
        "lead_time": res["lead_time"], "safety_stock": res["safety_stock"], "recommendation": res["recommendation"], "projection": res["projection"],
        "evaluation": res["evaluation"], "scenario_results": res["scenario_results"], "mitigations": res["mitigations"], "infeasibility": res["infeasibility"],
        "costs": res["costs"], "explanation": {"steps": res["explanation"], "sensitivity": res.get("sensitivity"), "order_quantity": res.get("order_quantity"), "constraints": res.get("constraints"), "statements": res["statements"]},  # fmt: skip
        "warnings": {"warnings": res["warnings"], "data_quality": res["data_quality"]}, "provenance": prov, "recommendation_hash": rec_hash,
        "scenario_hash": None if not sc else sc["scenario"]["definition_hash"], "hypothetical": hypothetical,
    }  # fmt: skip


def execute(factory: sessionmaker[Session], run_id: int, limits: Limits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(ReplenishmentRun).where(ReplenishmentRun.id == run_id, ReplenishmentRun.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        t0 = time.monotonic()

        def stop() -> None:
            if time.monotonic() - t0 > limits.time_budget_seconds:
                raise _Budget
            if int((time.monotonic() - t0) * 10) % 20 == 0 and session.scalar(
                select(ReplenishmentRun.cancel_requested).where(ReplenishmentRun.id == run_id)
            ):
                raise _Cancelled

        try:
            run = session.get(ReplenishmentRun, run_id)
            req = ReplenishRequest.model_validate(run.request)
            if session.scalar(select(ReplenishmentRun.cancel_requested).where(ReplenishmentRun.id == run_id)):
                raise _Cancelled
            if req.as_of is None:
                req = req.model_copy(update={"as_of": run.started_at})
            res = compute(session, req, limits, stop=stop)
            session.refresh(run)
            if run.cancel_requested:
                raise _Cancelled
            for k in ("snapshot", "snapshot_hash", "position", "demand", "lead_time", "safety_stock", "recommendation", "projection", "evaluation", "scenario_results", "mitigations", "infeasibility", "costs", "explanation", "warnings", "provenance", "recommendation_hash"):  # fmt: skip
                setattr(run, k, res[k])
            run.scenario_hash = res["scenario_hash"]
            run.result_status = res["result_status"]
            run.reproducibility = {
                "request_hash": run.request_hash, "algorithm_version": ALGORITHM_VERSION, "snapshot_hash": res["snapshot_hash"], "scenario_hash": res["scenario_hash"],
                "recommendation_hash": res["recommendation_hash"], "seed": (res["evaluation"] or {}).get("seed"), "as_of": req.as_of.isoformat() if req.as_of else None,
                "software": {"python": platform.python_version()},
                "note": "Replaying the stored snapshot with the stored request and seed reproduces the recommendation. A re-run against changed records has a different snapshot hash.",
            }  # fmt: skip
            run.duration_ms = int((time.monotonic() - t0) * 1000)
            run.finished_at = utcnow()
            run.status = (
                SimStatus.PARTIAL if res["result_status"] in ("incomplete", "not_solved") else SimStatus.COMPLETED
            )
            session.commit()
        except _Cancelled:
            session.rollback()
            _finish(session, run_id, SimStatus.CANCELLED, "cancelled by request")
        except _Budget:
            session.rollback()
            _finish(session, run_id, SimStatus.FAILED, "compute budget exceeded")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            logger.exception("replenishment run failed", extra={"run": run_id})
            msg = (
                str(exc)[:200]
                if isinstance(exc, (ConflictError, NotFoundError, DomainValidationError, ValueError))
                else ""
            )
            _finish(session, run_id, SimStatus.FAILED, f"{type(exc).__name__}" + (f": {msg}" if msg else ""))
    finally:
        session.close()


def _finish(session: Session, run_id: int, status: SimStatus, err: str) -> None:
    session.execute(update(ReplenishmentRun).where(ReplenishmentRun.id == run_id, ReplenishmentRun.status == SimStatus.RUNNING).values(status=status, finished_at=utcnow(), error=err[:300]))  # fmt: skip
    session.commit()


def run_sync(
    session: Session, req: ReplenishRequest, requested_by: str, limits: Limits | None = None, kind: str = "OPTIMIZE"
) -> ReplenishmentRun:
    lim = limits or Limits()
    run, _ = create(session, req, requested_by, lim, kind=kind)
    rid = run.id
    session.commit()
    execute(sessionmaker(bind=session.get_bind(), expire_on_commit=False), rid, lim)
    session.expire_all()
    return session.get(ReplenishmentRun, rid)


def approval_state(session: Session, run: ReplenishmentRun) -> dict[str, Any]:
    last = session.scalar(
        select(ReplenishmentDecision)
        .where(ReplenishmentDecision.run_id == run.id)
        .order_by(ReplenishmentDecision.id.desc())
    )
    if last is None:
        return {"state": "PROPOSED", "decided_by": None, "decided_at": None, "rationale": None, "executed": False}
    return {"state": last.decision, "decided_by": last.decided_by, "decided_at": last.decided_at.isoformat(), "rationale": last.rationale, "executed": False}  # fmt: skip


def decide(
    session: Session, run: ReplenishmentRun, decision: str, rationale: str, actor_name: str, is_admin: bool
) -> ReplenishmentDecision:
    if run.kind != "OPTIMIZE":
        raise ConflictError(
            "Only optimisation runs can be approved or rejected, not simulations of an analyst's own policy."
        )
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError(f"A {run.status.value} run cannot be approved or rejected.")
    cur = approval_state(session, run)["state"]
    if cur == decision:
        raise ConflictError(f"The run is already {decision}.")
    if decision == "APPROVED":
        if run.result_status not in ("recommended", "no_order_needed"):
            raise ConflictError(f"Only a recommended plan can be approved; this run is '{run.result_status}'. An incomplete, infeasible or unsolved result is never approved.")  # fmt: skip
        if (run.provenance or {}).get("hypothetical"):
            raise ConflictError(
                "A run that applied a scenario or what-if is hypothetical and cannot be approved as a replenishment plan."
            )
        if run.status is SimStatus.PARTIAL:
            raise ConflictError("This run is partial; re-run with complete inputs before approving.")
        if run.requested_by == actor_name and not is_admin:
            raise ConflictError("The requester cannot approve their own run; another reviewer must decide.")
    d = ReplenishmentDecision(run_id=run.id, decision=decision, rationale=rationale, decided_by=actor_name, decided_at=utcnow(), recommendation_hash=run.recommendation_hash or "")  # fmt: skip
    session.add(d)
    session.flush()
    return d


def replay(session: Session, run: ReplenishmentRun, limits: Limits) -> dict[str, Any]:
    """Recompute from the STORED snapshot (exact) and, separately, report whether live records have since changed."""
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError("Only finished runs can be replayed.")
    req = ReplenishRequest.model_validate(run.request)
    if req.as_of is None:
        req = req.model_copy(update={"as_of": run.started_at})
    res = compute(session, req, limits, snapshot=json.loads(json.dumps(run.snapshot)))
    live = compute(session, req, limits)
    return {
        "run_uid": run.run_uid,
        "reproduced_from_stored_snapshot": res["recommendation_hash"] == run.recommendation_hash,
        "stored_recommendation_hash": run.recommendation_hash,
        "replayed_recommendation_hash": res["recommendation_hash"],
        "live_inputs_unchanged": live["snapshot_hash"] == run.snapshot_hash,
        "stored_status": run.result_status,
        "replayed_status": res["result_status"],
        "explanation": "The stored snapshot reproduces the stored recommendation exactly."
        if res["recommendation_hash"] == run.recommendation_hash
        else "The stored snapshot did not reproduce the stored recommendation; the algorithm version differs from the one recorded.",  # fmt: skip
        "live_note": "Re-loading the registered records as of the run time gives identical inputs."
        if live["snapshot_hash"] == run.snapshot_hash
        else "Re-loading the registered records as of the run time gives different inputs (records were corrected, retired or back-dated since); a new run would differ. The stored run is unchanged.",  # fmt: skip
    }
