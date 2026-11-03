"""Sourcing runs: validated, idempotent, bounded, cancellable and immutable once finished (QUEUED -> RUNNING -> terminal).

A run reads offers, relationships, risk assessments, delivery history and (optionally) a scenario; it writes only
``sourcing_runs`` and never changes a supplier, offer or scenario. It stores the exact candidate terms it saw (the snapshot and
its hash), the request, the scenario definition hash and the algorithm version. Approval is a separate insert-only decision.
ATLAS never places an order: an approved plan is an internal record that a human decided to proceed elsewhere.
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
from app.models.inventory import StockoutFinding
from app.models.product import Product
from app.models.sourcing import SourcingDecision, SourcingOffer, SourcingRun
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.sourcing import data, overlay
from app.sourcing import engine as eng
from app.sourcing.config import ALGORITHM_VERSION, STATEMENTS, Limits, SourcingRequest

logger = logging.getLogger("atlas.sourcing")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
SENSITIVE = {"unit_price", "freight_per_unit", "purchase_cost", "freight_cost", "landed_cost_stated", "average_unit_cost", "cost_of_diversification_vs_cheapest", "objective_value", "price", "purchase_cost_total"}  # fmt: skip


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


def redact(obj: Any) -> Any:
    """Remove commercial figures (unit prices, freight and every cost derived from them) from a payload for viewers."""
    if isinstance(obj, dict):
        out = {k: (None if k in SENSITIVE else redact(v)) for k, v in obj.items()}
        if out.get("metric") == "price":
            out["raw"] = None
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def validate_refs(session: Session, req: SourcingRequest) -> None:
    problems = eng.validate_request(req)
    if problems:
        raise DomainValidationError("; ".join(problems))
    p = session.get(Product, req.product_id)
    if p is None:
        raise NotFoundError(f"Product {req.product_id} does not exist.")
    if p.merged_into_id is not None:
        raise ConflictError(f"Product {req.product_id} was merged into {p.merged_into_id}; use the surviving product.")
    f = session.get(Facility, req.destination_facility_id)
    if f is None:
        raise NotFoundError(f"The destination facility {req.destination_facility_id} does not exist.")
    if f.merged_into_id is not None:
        raise ConflictError(f"The destination facility was merged into {f.merged_into_id}; use the surviving facility.")
    if p.unit_of_measure and p.unit_of_measure != req.unit:
        raise DomainValidationError(
            f"The product is measured in '{p.unit_of_measure}' but the requirement is in '{req.unit}'. No unit conversion is applied."
        )
    ids = (
        {a.supplier_id for a in req.adjustments}
        | set(req.constraints.allowed_supplier_ids)
        | set(req.constraints.excluded_supplier_ids)
    )
    if ids:
        known = set(session.scalars(select(Supplier.id).where(Supplier.id.in_(ids))))
        if ids - known:
            raise DomainValidationError(f"Unknown suppliers: {sorted(ids - known)}.")
    if req.stockout_finding_id is not None:
        fd = session.get(StockoutFinding, req.stockout_finding_id)
        if fd is None:
            raise NotFoundError(f"Stockout finding {req.stockout_finding_id} does not exist.")
        if fd.product_id != req.product_id or fd.facility_id != req.destination_facility_id:
            raise DomainValidationError("The referenced stockout finding is for a different product or facility.")
        if fd.unit and fd.unit != req.unit:
            raise DomainValidationError(f"The stockout finding is in '{fd.unit}', the requirement in '{req.unit}'.")
    if req.scenario_id is not None:
        from app.scenarios import service as scen

        sc = scen.get_scenario(session, req.scenario_id)
        if sc.status.value != "READY":
            raise ConflictError(
                f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used for sourcing."
            )


def create(session: Session, req: SourcingRequest, requested_by: str, limits: Limits, *, kind: str = "OPTIMIZE", idempotency_key: str | None = None) -> tuple[SourcingRun, bool]:  # fmt: skip
    digest = hashlib.sha256((kind + req.digest()).encode()).hexdigest()
    if idempotency_key:
        prior = session.scalar(select(SourcingRun).where(SourcingRun.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.request_hash) != (requested_by, digest):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    validate_refs(session, req)
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(SourcingRun).where(SourcingRun.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = len(list(session.scalars(select(SourcingRun.id).where(SourcingRun.status.in_(ACTIVE)))))
    if active >= limits.max_active_runs:
        raise ConflictError(
            f"{active} sourcing runs are already queued or running (limit {limits.max_active_runs}). Try again shortly."
        )
    run = SourcingRun(
        run_uid=f"SR-{uuid.uuid4().hex[:16].upper()}", kind=kind, status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key,
        product_id=req.product_id, destination_facility_id=req.destination_facility_id, scenario_id=req.scenario_id, request=req.canonical(),
        request_hash=digest, algorithm_version=ALGORITHM_VERSION, queued_at=now,
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run, True


def cancel(run: SourcingRun) -> SourcingRun:
    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {run.status.value} sourcing run cannot be cancelled.")
    return run


def compute(
    session: Session, req: SourcingRequest, limits: Limits, *, kind: str = "OPTIMIZE", stop: Any = None
) -> dict[str, Any]:
    """Reads only. Returns the fields to store on the run."""
    now = req.as_of or utcnow()
    cands, ctx, warns, truncated = data.load_candidates(session, req, limits, now)
    terms, snap_hash = data.snapshot_of(cands)
    disrupted = None
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    info = None
    if req.scenario_id is not None or req.adjustments:
        dc = cands
        if req.scenario_id is not None:
            dc, a1, skipped, info = overlay.from_scenario(session, req.scenario_id, dc, req.destination_facility_id)
            applied += a1
        if req.adjustments:
            dc, a2 = overlay.from_adjustments(dc, req.adjustments)
            applied += a2
        disrupted = dc
    base = eng.plan(cands, ctx, req, limits, stop=stop)
    scen = eng.plan(disrupted, ctx, req, limits, stop=stop) if disrupted is not None else None
    primary = scen or base
    cmp_out: Any = None
    allocations: Any = primary["allocations"]
    if kind == "COMPARE":
        cmp_out = eng.strategy_comparison(cands, ctx, req, limits, disrupted, stop)
        allocations = cmp_out["strategies"]
    elif scen is not None:
        cmp_out = eng.compare_results(base, scen, applied, skipped)
    if truncated:
        warns.append(
            "The candidate list was truncated, so the result covers only part of the registered relationships."
        )
    if scen is not None and info is not None:
        warns.append(
            "Scenario event timing windows are not modelled: every applied event is treated as in force for the whole requirement period."
        )
    if skipped:
        warns.append(
            f"{len(skipped)} scenario event(s) could not be mapped to supplier offers and were not applied (see disruptions.not_applied)."
        )
    if any(c["terms"]["provenance"] == "SYNTHETIC_DEMO" for c in primary["candidates"] if c["terms"]["provenance"]):
        warns.append(
            "Some offers are synthetic demonstration data: results verify the software, not real sourcing options."
        )
    if primary["ranking_incomplete"]:
        warns.append(
            "The suitability ranking is incomplete: at least one eligible candidate lacks a weighted metric (it scored 0 for it)."
        )
    warns += primary["ranking_warnings"]
    if primary["price_basis"] == "PURCHASE_ONLY" and req.objective == "LOWEST_COST":
        warns.append("Cost covers purchase price only; freight and other landed-cost components are not included.")
    ohash = overlay.overlay_hash(applied, skipped)
    prov_mix: dict[str, int] = {}
    for c in cands:
        if c.provenance:
            prov_mix[c.provenance] = prov_mix.get(c.provenance, 0) + 1
    summary = {
        "result_status": primary["result_status"], "objective": req.objective, "strategy": req.strategy, "fulfillment": req.fulfillment,
        "demand": req.quantity, "unit": req.unit, "currency": req.currency, "price_basis": primary["price_basis"], "ranking_incomplete": primary["ranking_incomplete"],
        "solver": primary["solver"], "bounds": primary["bounds"], "counts": primary["counts"], "hard_constraint_checks": primary["hard_constraint_checks"],
        "requirement": {"source": req.requirement_source, "stockout_finding_id": req.stockout_finding_id, "period_start": str(req.period_start) if req.period_start else None, "period_end": str(req.period_end) if req.period_end else None, "note": req.note},  # fmt: skip
        "weights": req.weights.normalised(),
        "statements": [STATEMENTS[k] for k in ("decision_support", "score", "cost", "missing", "concentration", "resilience", "tenancy")],
        "baseline": None if scen is None else {"result_status": base["result_status"], "metrics": base["metrics"]},
        "hypothetical": disrupted is not None,
    }  # fmt: skip
    return {
        "result_status": primary["result_status"], "snapshot": terms, "snapshot_hash": snap_hash, "candidates": primary["candidates"], "allocations": allocations,
        "baseline_allocations": base["allocations"] if scen is not None else None, "comparison": cmp_out, "infeasibility": primary["infeasibility"],
        "metrics": primary["metrics"], "summary": summary, "warnings": warns,
        "disruptions": {"scenario": info, "applied": applied, "not_applied": skipped, "overlay_hash": ohash, "baseline_unchanged": True} if (disrupted is not None) else None,  # fmt: skip
        "provenance": {"as_of": now.isoformat(), "offer_provenance": dict(sorted(prov_mix.items())), "scenario": info, "overlay_hash": ohash, "snapshot_hash": snap_hash, "truncated_candidates": truncated},  # fmt: skip
        "scenario_hash": None if info is None else info["definition_hash"],
        "inexact": primary["solver"]["optimality"] == "NOT_PROVEN" or truncated,
    }  # fmt: skip


def execute(factory: sessionmaker[Session], run_id: int, limits: Limits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(SourcingRun).where(SourcingRun.id == run_id, SourcingRun.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        t0 = time.monotonic()

        def stop() -> None:
            if time.monotonic() - t0 > limits.time_budget_seconds:
                raise _Budget

        try:
            run = session.get(SourcingRun, run_id)
            req = SourcingRequest.model_validate(run.request)
            if session.scalar(select(SourcingRun.cancel_requested).where(SourcingRun.id == run_id)):
                raise _Cancelled
            if req.as_of is None:
                req = req.model_copy(update={"as_of": run.started_at})
            res = compute(session, req, limits, kind=run.kind, stop=stop)
            session.refresh(run)
            if run.cancel_requested:
                raise _Cancelled
            for k in ("snapshot", "snapshot_hash", "candidates", "allocations", "baseline_allocations", "comparison", "infeasibility", "metrics", "summary", "warnings", "disruptions", "provenance", "scenario_hash"):  # fmt: skip
                setattr(run, k, res[k])
            run.result_status = res["result_status"]
            run.reproducibility = {
                "request_hash": run.request_hash, "algorithm_version": ALGORITHM_VERSION, "snapshot_hash": res["snapshot_hash"], "scenario_hash": res["scenario_hash"],
                "overlay_hash": (res["disruptions"] or {}).get("overlay_hash"), "as_of": req.as_of.isoformat() if req.as_of else None, "software": {"python": platform.python_version()},
                "note": "Replaying the same request at the same as-of time against unchanged offers reproduces this result. Offer rows are immutable, so changed terms have a different snapshot hash.",
            }  # fmt: skip
            run.duration_ms = int((time.monotonic() - t0) * 1000)
            run.finished_at = utcnow()
            run.status = SimStatus.PARTIAL if res["inexact"] else SimStatus.COMPLETED
            session.commit()
        except _Cancelled:
            session.rollback()
            _finish(session, run_id, SimStatus.CANCELLED, "cancelled by request")
        except _Budget:
            session.rollback()
            _finish(session, run_id, SimStatus.FAILED, "compute budget exceeded")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            logger.exception("sourcing run failed", extra={"run": run_id})
            msg = (
                str(exc)[:200]
                if isinstance(exc, (ConflictError, NotFoundError, DomainValidationError, ValueError))
                else ""
            )
            _finish(session, run_id, SimStatus.FAILED, f"{type(exc).__name__}" + (f": {msg}" if msg else ""))
    finally:
        session.close()


def _finish(session: Session, run_id: int, status: SimStatus, err: str) -> None:
    session.execute(update(SourcingRun).where(SourcingRun.id == run_id, SourcingRun.status == SimStatus.RUNNING).values(status=status, finished_at=utcnow(), error=err[:300]))  # fmt: skip
    session.commit()


def run_sync(
    session: Session, req: SourcingRequest, requested_by: str, limits: Limits | None = None, kind: str = "OPTIMIZE"
) -> SourcingRun:
    lim = limits or Limits()
    run, _ = create(session, req, requested_by, lim, kind=kind)
    rid = run.id
    session.commit()
    execute(sessionmaker(bind=session.get_bind(), expire_on_commit=False), rid, lim)
    session.expire_all()
    return session.get(SourcingRun, rid)


def allocation_hash(run: SourcingRun) -> str:
    return hashlib.sha256(json.dumps(run.allocations, sort_keys=True, default=str).encode()).hexdigest()


def approval_state(session: Session, run: SourcingRun) -> dict[str, Any]:
    last = session.scalar(
        select(SourcingDecision).where(SourcingDecision.run_id == run.id).order_by(SourcingDecision.id.desc())
    )
    if last is None:
        return {"state": "PROPOSED", "decided_by": None, "decided_at": None, "rationale": None, "executed": False}
    return {
        "state": last.decision,
        "decided_by": last.decided_by,
        "decided_at": last.decided_at.isoformat(),
        "rationale": last.rationale,
        "executed": False,
    }


def decide(
    session: Session, run: SourcingRun, decision: str, rationale: str, actor_name: str, is_admin: bool
) -> SourcingDecision:
    if run.kind != "OPTIMIZE":
        raise ConflictError(
            "Only single-objective optimisation runs can be approved or rejected, not strategy comparisons."
        )
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError(f"A {run.status.value} run cannot be approved or rejected.")
    cur = approval_state(session, run)["state"]
    if cur == decision:
        raise ConflictError(f"The run is already {decision}.")
    if decision == "APPROVED":
        if run.result_status != "feasible":
            raise ConflictError(
                f"Only a feasible plan can be approved; this run is '{run.result_status}'. A partial or infeasible plan is never approved as a sourcing plan."
            )
        if (run.summary or {}).get("hypothetical"):
            raise ConflictError(
                "A run that applied a scenario or what-if adjustment is hypothetical and cannot be approved as a sourcing plan."
            )
        if run.status is SimStatus.PARTIAL:
            raise ConflictError(
                "This run's solution is not proven optimal or its candidate list was truncated; re-run with narrower filters before approving."
            )
        if run.requested_by == actor_name and not is_admin:
            raise ConflictError("The requester cannot approve their own run; another reviewer must decide.")
        used = [a["offer_uid"] for a in (run.allocations or [])]
        stale = [
            o.offer_uid
            for o in session.scalars(select(SourcingOffer).where(SourcingOffer.offer_uid.in_(used)))
            if o.status != "ACTIVE"
        ]
        if stale:
            raise ConflictError(
                f"Offer(s) {', '.join(sorted(stale))} are no longer active; re-run the optimisation before approving."
            )
    d = SourcingDecision(
        run_id=run.id,
        decision=decision,
        rationale=rationale,
        decided_by=actor_name,
        decided_at=utcnow(),
        allocation_hash=allocation_hash(run),
    )
    session.add(d)
    session.flush()
    return d


def replay(session: Session, run: SourcingRun, limits: Limits) -> dict[str, Any]:
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ConflictError("Only finished runs can be replayed.")
    req = SourcingRequest.model_validate(run.request)
    if req.as_of is None:
        req = req.model_copy(update={"as_of": run.started_at})
    res = compute(session, req, limits, kind=run.kind)
    same = res["snapshot_hash"] == run.snapshot_hash
    pick = (lambda a: a) if run.kind == "COMPARE" else (lambda a: {r["offer_uid"]: r["quantity"] for r in a or []})
    return {
        "run_uid": run.run_uid,
        "snapshot_unchanged": same,
        "allocations_identical": pick(res["allocations"]) == pick(run.allocations)
        if run.kind == "OPTIMIZE"
        else json.dumps(res["allocations"], sort_keys=True, default=str)
        == json.dumps(run.allocations, sort_keys=True, default=str),  # fmt: skip
        "stored_status": run.result_status,
        "replayed_status": res["result_status"],
        "explanation": "Identical allocations are expected whenever the offers and evidence are unchanged."
        if same
        else "The registered offers or evidence changed since this run, so a different result is expected. The stored run is unchanged.",  # fmt: skip
    }
