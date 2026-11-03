"""Persistent lead-time estimates and late-delivery risk assessments for one open order line (or one supplier lane).

An assessment is a recorded statement made at ``as_of`` using only what was known then. It never changes a delivery, an
order or a plan. A historical late rate is reported as a historical rate, never as the probability for this delivery; a score
is called a probability only when the model's run carries held-out calibration evidence. Everything it used is cited.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.leadtime import analytics, dataset
from app.leadtime import estimators as est
from app.leadtime import features as F
from app.leadtime import semantics as sem
from app.leadtime.config import ALGORITHM_VERSION, STATEMENTS, LeadTimeConfig
from app.leadtime.fitters import apply_platt, dot, sigmoid
from app.leadtime.history import HF, history_at
from app.leadtime.resolve import load_deliveries
from app.models.delivery import DeliveryAssessment, LeadTimeRun
from app.models.enums import SimStatus
from app.models.types import utcnow

BANDS = {"low_below": 0.2, "high_from": 0.5}  # heuristic cut points; not a policy


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_ref: str | None = Field(default=None, min_length=1, max_length=128)
    product_id: int = Field(ge=1, le=2**31 - 1)
    supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)  # lane subject (no delivery_ref)
    destination_facility_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    prediction_point: Literal["ORDER_PLACEMENT", "CONFIRMATION", "DISPATCH"] = "ORDER_PLACEMENT"
    as_of: datetime | None = None
    commitment: Literal["ORIGINAL", "LATEST"] = "ORIGINAL"
    definition: Literal["ORDER_TO_RECEIPT", "DISPATCH_TO_ARRIVAL", "ORDER_TO_DISPATCH"] = "ORDER_TO_RECEIPT"
    start_event: Literal["ORDER", "CONFIRMATION"] = "ORDER"
    unit: Literal["HOURS", "CALENDAR_DAYS"] = "CALENDAR_DAYS"
    leadtime_run_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    risk_run_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    min_group_n: int = Field(default=8, ge=3, le=500)


def _run_ok(session: Session, run_id: int | None, target: str, point: str) -> LeadTimeRun | None:
    if run_id is None:
        return None
    r = session.get(LeadTimeRun, run_id)
    if r is None:
        raise NotFoundError(f"Lead-time run {run_id} not found.")
    if r.target != target:
        raise DomainValidationError(f"Run {run_id} is a {r.target} run; a {target} run is required here.")
    if r.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL) or r.result_status != "completed" or not r.artifact:
        raise DomainValidationError(
            f"Run {run_id} has no usable model (status {r.status.value}, result {r.result_status})."
        )
    if r.artifact.get("point") != point:
        raise DomainValidationError(f"Run {run_id} was trained for prediction point {r.artifact.get('point')}; a model for {point} is required. A model is never reused at a different prediction point.")  # fmt: skip
    return r


def _ts(x: datetime | None) -> str | None:
    return None if x is None else x.isoformat()


def assess(session: Session, req: AssessRequest, requested_by: str, *, idempotency_key: str | None = None, now: datetime | None = None) -> tuple[DeliveryAssessment, bool]:  # fmt: skip
    now = now or utcnow()
    as_of = req.as_of or now
    if as_of.tzinfo is None:
        raise DomainValidationError("as_of must carry a time zone.")
    if as_of > now + timedelta(minutes=5):
        raise DomainValidationError("as_of cannot be in the future.")
    lt_run = _run_ok(session, req.leadtime_run_id, "LEAD_TIME", req.prediction_point)
    rk_run = _run_ok(session, req.risk_run_id, "LATE_RISK", req.prediction_point)
    subject = "DELIVERY" if req.delivery_ref else "LANE"
    if subject == "LANE" and (req.supplier_id is None or req.destination_facility_id is None):
        raise DomainValidationError(
            "A lane estimate needs supplier_id and destination_facility_id; a delivery assessment needs delivery_ref."
        )
    base_cfg = LeadTimeConfig.model_validate(lt_run.config if lt_run else {"definition": req.definition, "start_event": req.start_event, "unit": req.unit, "commitment": req.commitment, "prediction_point": req.prediction_point, "min_group_n": req.min_group_n})  # fmt: skip
    cfg = base_cfg
    if lt_run is None and rk_run is not None:
        cfg = LeadTimeConfig.model_validate({**base_cfg.canonical(), "commitment": rk_run.config.get("commitment", req.commitment), "grace_hours": rk_run.config.get("grace_hours", 0.0)})  # fmt: skip
    deliveries = load_deliveries(session, as_of=as_of)
    target = None
    if subject == "DELIVERY":
        target = next((d for d in deliveries if d.ref == req.delivery_ref and d.product_id == req.product_id), None)
        if target is None:
            raise NotFoundError("No delivery record with this reference and product was known at as_of.")
    sup = target.supplier_id if target else req.supplier_id
    dest = target.destination_id if target else req.destination_facility_id
    history = [d for d in deliveries if target is None or d.key != target.key]
    part = analytics.partition(history, cfg, as_of)
    outcomes = dataset.outcomes_of(part["complete"])
    warnings: list[str] = []
    snap_ids = target.snapshot_ids if target else []
    input_hash = hashlib.sha256(json.dumps([subject, req.delivery_ref, req.product_id, sup, dest, req.prediction_point, as_of.isoformat(), req.commitment, cfg.definition, cfg.start_event, cfg.unit, req.leadtime_run_id, req.risk_run_id, snap_ids, target.event_ids if target else [], len(outcomes)], default=str).encode()).hexdigest()  # fmt: skip
    if idempotency_key:
        prior = session.scalar(select(DeliveryAssessment).where(DeliveryAssessment.idempotency_key == idempotency_key))
        if prior is not None:
            same = (prior.subject, prior.delivery_ref, prior.product_id, prior.prediction_point, prior.leadtime_run_id, prior.risk_run_id) == (subject, req.delivery_ref, req.product_id, req.prediction_point, req.leadtime_run_id, req.risk_run_id)  # fmt: skip
            if (
                prior.requested_by != requested_by
                or not same
                or (req.as_of is not None and prior.input_hash != input_hash)
            ):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False  # a replay returns the stored assessment; an implicit as_of ("now") is not re-evaluated
    prior = session.scalar(select(DeliveryAssessment).where(DeliveryAssessment.input_hash == input_hash))
    if prior is not None and req.as_of is not None:
        return prior, False

    def record(status: str, reason: str | None = None, **kw: Any) -> tuple[DeliveryAssessment, bool]:
        a = DeliveryAssessment(
            assessment_uid=f"DA-{uuid.uuid4().hex[:16].upper()}", subject=subject, delivery_ref=req.delivery_ref, product_id=req.product_id,
            supplier_id=sup, destination_facility_id=dest, prediction_point=req.prediction_point, as_of=as_of, status=status,
            unavailable_reason=reason, commitment=cfg.commitment, unit=cfg.unit, leadtime_run_id=req.leadtime_run_id, risk_run_id=req.risk_run_id,
            input_hash=input_hash, idempotency_key=idempotency_key, requested_by=requested_by, algorithm_version=ALGORITHM_VERSION,
            warnings=warnings, **kw,
        )  # fmt: skip
        session.add(a)
        session.flush()
        return a, True

    if target is not None:
        state, _why = sem.classify(target, as_of, cfg.chronology_tolerance_hours)
        if state not in sem.CENSORED:
            return record("UNAVAILABLE", f"The order is {state} at as_of, not an open or in-transit order; its outcome is already known or it cannot be predicted.", evidence={"state": state, "delivery": target.key})  # fmt: skip
        ref_at = F.reference_time(target, req.prediction_point)
        if ref_at is None:
            return record("UNAVAILABLE", f"The delivery has no {F.REF_FIELD[req.prediction_point]} recorded, which the {req.prediction_point} prediction point requires.", evidence={"delivery": target.key})  # fmt: skip
        if ref_at > as_of:
            return record("UNAVAILABLE", "The prediction point lies after as_of.", evidence={"delivery": target.key})
    else:
        ref_at = as_of
    start = None
    if target is not None:
        start = (
            sem.endpoints(target, cfg.definition, cfg.start_event)[0] if cfg.definition != "PROMISE_DEVIATION" else None
        )
    hf = history_at(outcomes, as_of, sup, (sup, dest))
    # ---------------------------------------------------------------------------------------------- lead-time estimate
    est_block: dict[str, Any] | None = None
    factors: list[dict[str, Any]] = []
    lead_art = lt_run.artifact if lt_run else None
    rows = [
        (o.d.supplier_id, o.d.destination_id, o.d.product_id, o.value, o.ref_at)
        for o in part["with_value"]
        if o.value is not None and o.ref_at
    ]
    if not rows:
        warnings.append(
            "No completed deliveries support this lead-time definition before as_of; no estimate can be made."
        )
    else:
        bmin = (lead_art or {}).get("baseline", {}).get("min_group_n", cfg.min_group_n)
        bhl = (lead_art or {}).get("baseline", {}).get("half_life_days", cfg.recency_half_life_days)
        base = est.LeadBaseline(rows, bmin, bhl, as_of)
        key = lead_art["model"] if lead_art else "hierarchical_median"
        method_kind = "history_lookup"
        if key in est.LEARNED:
            if target is None:
                warnings.append(
                    "A learned model needs the order's own fields; a lane estimate uses the hierarchical history lookup instead."
                )
                key = "hierarchical_median"
            else:
                sp = F.Spec.from_json(lead_art["spec"])
                view = F.view_of(target, req.prediction_point)
                row = F.transform(
                    sp,
                    F.raw_numeric(view, req.prediction_point, hf),
                    F.raw_categorical(view, F.CATEGORICAL[req.prediction_point]),
                )
                val = dot(lead_art["beta"], row)
                clamped = val < 0
                val = max(0.0, val)
                level, n_used, method_kind = "learned model", len(rows), "learned_model"
                factors = [
                    {"feature": c, "contribution": v, "unit": cfg.unit}
                    for c, v in F.contributions(sp, lead_art["beta"], row)[:6]
                ]
                if clamped:
                    warnings.append("The model's raw prediction was negative and was clamped to zero.")
        if key not in est.LEARNED:
            val, level, n_used = base.predict(key, sup, dest, req.product_id)
            factors = [{"feature": "history level", "detail": f"{level} (n={n_used})"}]
            if level == "global":
                warnings.append(
                    "Too little history for this supplier/lane: the estimate is the global median across all suppliers, not a supplier-specific estimate."
                )
        iv = (lead_art or {}).get("interval")
        interval = None
        if iv and iv.get("supported"):
            interval = {"level": iv["level"], "lower": max(0.0, val + iv["lower_offset"]), "upper": max(0.0, val + iv["upper_offset"]), "method": iv["method"], "test_coverage": iv.get("test_coverage"), "n_residuals": iv["n_residuals"], "model_run_id": req.leadtime_run_id}  # fmt: skip
        else:
            interval = {"status": "unsupported", "note": "No validated interval is available for this estimate (no run with a supported interval was selected). No bounds are shown."}  # fmt: skip
        elapsed_val = None
        cond = None
        if start is not None and start <= as_of and cfg.unit != "BUSINESS_DAYS":
            elapsed_val = sem.elapsed(start, as_of, cfg.unit)
            vals = [
                v
                for v in base.values_for(
                    key if key not in est.LEARNED else "hierarchical_median", sup, dest, req.product_id
                )
                if v > elapsed_val
            ]
            if len(vals) >= cfg.min_group_n:
                cond = {"n": len(vals), "median_total": sorted(vals)[len(vals) // 2], "note": "Median of historical lead times that exceeded the time already elapsed (right-censoring aware); not a prediction guarantee."}  # fmt: skip
            elif val is not None and elapsed_val > val:
                warnings.append("The order has already been open longer than the estimate; the estimate does not condition on elapsed time and too few comparable late histories exist.")  # fmt: skip
        est_block = {"value": val, "unit": cfg.unit, "definition": cfg.definition, "start_event": cfg.start_event, "method": key, "method_kind": method_kind, "level": level, "n": n_used, "model_run_id": req.leadtime_run_id, "interval": interval, "elapsed_since_start": elapsed_val, "conditional_on_elapsed": cond, "reference_period": {"first_outcome": _ts(min(r[4] for r in rows)), "last_outcome": _ts(max(r[4] for r in rows))}, "statement": STATEMENTS["evidence"]}  # fmt: skip
    # ---------------------------------------------------------------------------------------------- arrival and delay
    promised = sem.commitment_of(target, cfg.commitment) if target else None
    arrival = None
    if est_block is not None:
        if start is None:
            arrival = {
                "status": "not_calculable",
                "reason": "No start event is recorded for this definition, or the assessment is for a lane.",
            }
        elif cfg.unit == "BUSINESS_DAYS":
            arrival = {"status": "not_calculable", "reason": "Business-day estimates are not converted to a timestamp."}
        else:
            step = timedelta(hours=est_block["value"]) if cfg.unit == "HOURS" else timedelta(days=est_block["value"])
            arr = start + step
            arrival = {
                "status": "estimated",
                "estimated_arrival": _ts(arr),
                "basis": f"{cfg.definition} start + estimate",
            }
            iv = est_block["interval"]
            if iv and "lower" in iv:
                mk = (lambda v: timedelta(hours=v)) if cfg.unit == "HOURS" else (lambda v: timedelta(days=v))
                arrival["window"] = {
                    "earliest": _ts(start + mk(iv["lower"])),
                    "latest": _ts(start + mk(iv["upper"])),
                    "level": iv["level"],
                }
            if promised is not None:
                arrival["promised_at"] = _ts(promised)
                arrival["potential_delay_hours"] = (arr - promised).total_seconds() / 3600
                if "window" in arrival:
                    arrival["potential_delay_hours_at_upper_bound"] = (
                        start + mk(iv["upper"]) - promised
                    ).total_seconds() / 3600
            else:
                arrival["promised_at"] = None
                warnings.append(
                    "No promised date is recorded for the chosen commitment; delay against the promise is unknown (not zero)."
                )
    # ---------------------------------------------------------------------------------------------- late-delivery risk
    risk = _risk(rk_run, target, req, cfg, hf, part, sup, dest, warnings, factors, promised)
    history_block = {
        "completed_before_as_of": len(part["complete"]), "with_lead_time": len(part["with_value"]), "with_commitment": len(part["punct"]),
        "excluded_by_reason": part["excluded"], "supplier_n": hf.sup_n, "lane_n": hf.lane_n, "censored": part["excluded"].get("CENSORED_IN_TRANSIT", 0) + part["excluded"].get("CENSORED_OPEN", 0),  # fmt: skip
        "supplier_historical_late_rate": _rate(part, sup), "note": STATEMENTS["rate"],
    }  # fmt: skip
    evid = {"delivery": target.key if target else None, "snapshot_ids": snap_ids, "event_ids": target.event_ids if target else [], "flags": target.flags if target else [], "provenance": target.provenance if target else None, "leadtime_run": req.leadtime_run_id, "risk_run": req.risk_run_id, "decision": STATEMENTS["decision"]}  # fmt: skip
    status = "ASSESSED" if (est_block is not None or risk is not None) else "UNAVAILABLE"
    reason = (
        None
        if status == "ASSESSED"
        else "Neither a lead-time estimate nor a risk statement could be supported by the history known at as_of."
    )
    return record(status, reason, reference_at=ref_at, promised_at=promised, estimate=est_block, arrival=arrival, risk=risk, history=history_block, factors=factors, evidence=evid)  # fmt: skip


def _rate(part: dict[str, Any], sup: int | None) -> dict[str, Any]:
    xs = [o for o in part["punct"] if o.d.supplier_id == sup]
    k = sum(1 for o in xs if o.late)
    return {"n": len(xs), "late": k, "rate": (k / len(xs)) if xs else None, "ci95": analytics.wilson(k, len(xs))}


def _band(v: float) -> str:
    return "low" if v < BANDS["low_below"] else "high" if v >= BANDS["high_from"] else "medium"


def _risk(
    rk_run, target, req, cfg, hf: HF, part, sup, dest, warnings: list[str], factors: list[dict[str, Any]], promised
) -> dict[str, Any] | None:  # noqa: ANN001
    hist = _rate(part, sup)
    if rk_run is None:
        return {"kind": "historical_rate_only", "score": None, "historical_late_rate": hist, "statement": STATEMENTS["rate"], "note": "No late-risk model was selected; only the supplier's historical late rate is shown. It is not the probability that this delivery will be late."}  # fmt: skip
    art = rk_run.artifact
    ev = (rk_run.evaluation or {}).get("models", {}).get(art["model"], {})
    summary = {"test_pr_auc": ev.get("test", {}).get("pr_auc"), "test_roc_auc": ev.get("test", {}).get("roc_auc"), "test_brier_raw": ev.get("test", {}).get("brier_raw"), "test_n": ev.get("test", {}).get("n"), "test_prevalence": ev.get("test", {}).get("prevalence"), "synthetic_note": STATEMENTS["synthetic"]}  # fmt: skip
    base = {
        "model": art["model"],
        "model_run_id": rk_run.id,
        "evaluation_summary": summary,
        "commitment": art.get("commitment"),
        "historical_late_rate": hist,
    }
    if art["model"] == "base_rate":
        return {**base, "kind": "historical_rate", "score": art["train_prevalence"], "statement": STATEMENTS["rate"]}
    if art["model"] == "supplier_rate":
        t = art["supplier_rate"]
        k, n = t["table"].get(str(sup), [0, 0])
        return {
            **base,
            "kind": "historical_rate",
            "score": (k + t["m"] * t["p0"]) / (n + t["m"]),
            "n": n,
            "statement": STATEMENTS["rate"],
        }
    if target is None:
        warnings.append(
            "A late-risk model needs the order's own fields; a lane assessment shows only the historical rate."
        )
        return {
            "kind": "historical_rate_only",
            "score": None,
            "historical_late_rate": hist,
            "statement": STATEMENTS["rate"],
        }
    sp = F.Spec.from_json(art["spec"])
    view = F.view_of(target, req.prediction_point)
    row = F.transform(
        sp, F.raw_numeric(view, req.prediction_point, hf), F.raw_categorical(view, F.CATEGORICAL[req.prediction_point])
    )
    raw = sigmoid(dot(art["beta"], row))
    kind = art.get("score_kind", "uncalibrated_score")
    score = apply_platt(raw, tuple(art["platt"])) if kind == "calibrated_probability" and art.get("platt") else raw
    factors.extend(
        {"feature": c, "contribution": v, "unit": "log-odds"} for c, v in F.contributions(sp, art["beta"], row)[:6]
    )
    if promised is None:
        warnings.append("No promised date is recorded for the chosen commitment; lateness cannot be defined for this delivery, so the score is shown for information only.")  # fmt: skip
    return {**base, "kind": kind, "score": score, "raw_score": raw, "flag_at_validated_threshold": raw >= art["threshold"], "validated_threshold": art["threshold"], "threshold_basis": "maximises F1 on the validation split", "band": _band(score), "band_basis": "heuristic cut points (below 0.2 low, from 0.5 high); not a policy", "calibration": rk_run.calibration, "statement": STATEMENTS["score"] if kind != "calibrated_probability" else "Calibrated on a held-out chronological split; still an estimate, not a guarantee."}  # fmt: skip


def outcome_of(session: Session, a: DeliveryAssessment, now: datetime | None = None) -> dict[str, Any]:
    """Compare an issued assessment with what eventually happened (as known now). The assessment itself is never edited."""
    if a.subject != "DELIVERY":
        return {"status": "not_applicable", "note": "Lane estimates have no single outcome."}
    ds = load_deliveries(session, as_of=now or utcnow(), refs=[a.delivery_ref], product_id=a.product_id)
    d = next((x for x in ds if x.ref == a.delivery_ref), None)
    if d is None:
        return {"status": "unknown", "note": "The delivery is no longer resolvable."}
    state, _ = sem.classify(d, now or utcnow())
    if state != sem.COMPLETED:
        return {"status": "pending", "state": state, "note": "The outcome is not known yet; nothing is compared."}
    cfg = LeadTimeConfig.model_validate({"definition": (a.estimate or {}).get("definition", "ORDER_TO_RECEIPT"), "start_event": (a.estimate or {}).get("start_event", "ORDER"), "unit": a.unit or "CALENDAR_DAYS", "commitment": a.commitment})  # fmt: skip
    obs, _why = analytics.observe(d, cfg, now or utcnow())
    out: dict[str, Any] = {
        "status": "observed",
        "state": state,
        "actual_arrival": _ts(d.arrived_at),
        "actual_late": obs.late,
    }
    if a.estimate and obs.value is not None:
        e = a.estimate
        out["lead_time"] = {
            "actual": obs.value,
            "estimate": e["value"],
            "error": e["value"] - obs.value,
            "unit": a.unit,
        }
        iv = e.get("interval") or {}
        if "lower" in iv:
            out["lead_time"]["within_interval"] = iv["lower"] <= obs.value <= iv["upper"]
    if a.risk and a.risk.get("score") is not None and obs.late is not None:
        out["risk"] = {"score": a.risk["score"], "kind": a.risk["kind"], "actual_late": obs.late, "flag_correct": (a.risk.get("flag_at_validated_threshold") == obs.late) if "flag_at_validated_threshold" in a.risk else None, "note": "One outcome says little about a probability; calibration is judged over many deliveries on the model run."}  # fmt: skip
    return out
