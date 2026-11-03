"""Forecast-versus-actual matching and monitoring.

Matching never rewrites an issued forecast: each call stores a new evaluation *snapshot* (de-duplicated by the hash of
the actuals it saw). An actual period is ``missing`` (no observation), ``partial`` (not all days observed — not scored),
``preliminary`` (fully observed but younger than the finalization window) or ``final``.

Monitoring raises signals only (deterioration, sustained bias, stale forecast, input-quality change) when minimum sample
sizes are met. It never retrains or replaces a forecast.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecast import metrics as mx
from app.forecast.config import ForecastConfig, ForecastLimits
from app.forecast.prep import RawObs, norm_unit, prepare
from app.forecast.runs import OK_RESULTS, load_obs
from app.models.enums import SimStatus
from app.models.forecast import ForecastAlert, ForecastEvaluation, ForecastRun, ForecastValue
from app.models.inventory import DemandObservation
from app.models.types import utcnow

MIN_EVAL_N = 7  # minimum final matched points before monitoring draws a conclusion
BIAS_REL_THRESHOLD = 0.20
DETERIORATION_FACTOR = 1.5


def _utc_date(now: datetime | None) -> date:
    return (now or utcnow()).date()


def actual_periods(
    session: Session, run: ForecastRun, values: list[ForecastValue], today: date, finalization_days: int
) -> list[dict[str, Any]]:
    """Match forecast periods to current observations (same demand kind and unit as the run's training data)."""
    basis = (run.preparation or {}).get("basis")
    rows = session.scalars(
        select(DemandObservation).where(
            DemandObservation.product_id == run.product_id,
            DemandObservation.facility_id == run.facility_id,
            DemandObservation.period_end > values[0].period_start,
            DemandObservation.period_start < values[-1].period_end,
        )  # fmt: skip
    ).all()
    want = norm_unit(run.unit)
    daily: dict[date, float] = {}
    ids: list[int] = []
    for o in sorted(rows, key=lambda r: (r.period_start, r.id)):
        if (basis and o.kind.value != basis) or norm_unit(o.unit) != want or o.period_end <= o.period_start:
            continue
        n = (o.period_end - o.period_start).days
        ids.append(o.id)
        for k in range(n):
            daily[o.period_start + timedelta(days=k)] = o.quantity / n
    out = []
    for v in values:
        days = [v.period_start + timedelta(days=k) for k in range((v.period_end - v.period_start).days)]
        got = [d for d in days if d in daily]
        if not got:
            st, act = "missing", None
        elif len(got) < len(days):
            st, act = "partial", None
        else:
            act = sum(daily[d] for d in days)
            st = "final" if (today - v.period_end).days >= finalization_days else "preliminary"
        out.append({"step": v.step, "period_start": v.period_start.isoformat(), "period_end": v.period_end.isoformat(), "forecast": v.point, "forecast_raw": v.point_raw, "actual": act, "status": st, "intervals": v.intervals})  # fmt: skip
    return out


def _score(matches: list[dict[str, Any]], status: tuple[str, ...], scale: float | None) -> dict[str, Any]:
    recs = [(0, m["step"], m["actual"], m["forecast"]) for m in matches if m["status"] in status]
    return mx.metric_set(recs, {0: scale})


def _coverage(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    levels = sorted({iv["level"] for m in matches for iv in m["intervals"] if iv.get("lower") is not None})
    out = []
    for lv in levels:
        pts = [
            (m["actual"], next(i for i in m["intervals"] if i["level"] == lv))
            for m in matches
            if m["status"] == "final"
        ]
        pts = [(a, i) for a, i in pts if i.get("lower") is not None]
        hit = sum(1 for a, i in pts if i["lower"] <= a <= i["upper"])
        out.append({"level": lv, "n": len(pts), "empirical_coverage": hit / len(pts) if pts else None, "note": "Observed share of final actuals inside the issued interval; small n is only indicative."})  # fmt: skip
    return out


def evaluate_run(
    session: Session, run: ForecastRun, requested_by: str, finalization_days: int, now: datetime | None = None
) -> tuple[ForecastEvaluation, bool]:
    if run.result_status not in OK_RESULTS or run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise ValueError("This run produced no forecast values to evaluate.")
    values = session.scalars(
        select(ForecastValue).where(ForecastValue.run_id == run.id).order_by(ForecastValue.step)
    ).all()
    today = _utc_date(now)
    matches = actual_periods(session, run, list(values), today, finalization_days)
    snap = hashlib.sha256(
        json.dumps([(m["period_start"], m["actual"], m["status"]) for m in matches], sort_keys=True).encode()
    ).hexdigest()
    prior = session.scalar(
        select(ForecastEvaluation).where(ForecastEvaluation.run_id == run.id, ForecastEvaluation.actuals_hash == snap)
    )
    if prior is not None:
        return prior, False
    scale = (run.validation or {}).get("naive_scale")
    n_final = sum(1 for m in matches if m["status"] == "final")
    n_matched = sum(1 for m in matches if m["status"] in ("final", "preliminary"))
    if n_matched == 0:
        status, metrics = "no_actuals", None
    else:
        status = "evaluated" if n_final >= 1 else "insufficient_evidence"
        metrics = {
            "final": _score(matches, ("final",), scale),
            "preliminary_included": _score(matches, ("final", "preliminary"), scale),
            "interval_coverage": _coverage(matches),
            "bias_convention": "forecast − actual; positive = over-forecast",
            "note": "Preliminary actuals are younger than the finalization window and may still be revised; only 'final' results count for monitoring.",  # fmt: skip
        }
    ev = ForecastEvaluation(run_id=run.id, evaluated_at=now or utcnow(), requested_by=requested_by, actuals_hash=snap, n_matched=n_matched, n_final=n_final, status=status, metrics=metrics, matches=matches)  # fmt: skip
    session.add(ev)
    session.flush()
    return ev, True


# --------------------------------------------------------------------------------------------------- monitoring


def _fp(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _add(
    session: Session,
    found: list[ForecastAlert],
    run: ForecastRun,
    kind: str,
    sev: str,
    msg: str,
    evidence: dict[str, Any],
    key: Any,
) -> None:
    fp = _fp(kind, run.id, key)
    if session.scalar(select(ForecastAlert.id).where(ForecastAlert.fingerprint == fp)) is not None:
        return
    a = ForecastAlert(fingerprint=fp, kind=kind, severity=sev, product_id=run.product_id, facility_id=run.facility_id, run_id=run.id, message=msg[:500], evidence=evidence)  # fmt: skip
    session.add(a)
    found.append(a)


def latest_runs(session: Session, limit: int = 200) -> list[ForecastRun]:
    rows = session.scalars(select(ForecastRun).where(ForecastRun.result_status.in_(OK_RESULTS), ForecastRun.status.in_((SimStatus.COMPLETED, SimStatus.PARTIAL))).order_by(ForecastRun.queued_at.desc(), ForecastRun.id.desc())).all()  # fmt: skip
    seen, out = set(), []
    for r in rows:
        k = (r.product_id, r.facility_id)
        if k not in seen:
            seen.add(k)
            out.append(r)
        if len(out) >= limit:
            break
    return out


def monitor(
    session: Session,
    requested_by: str,
    finalization_days: int,
    now: datetime | None = None,
    limits: ForecastLimits | None = None,
) -> list[ForecastAlert]:
    limits = limits or ForecastLimits()
    today = _utc_date(now)
    found: list[ForecastAlert] = []
    for run in latest_runs(session):
        last_end = session.scalar(
            select(ForecastValue.period_end)
            .where(ForecastValue.run_id == run.id)
            .order_by(ForecastValue.step.desc())
            .limit(1)
        )
        if last_end is not None and today >= last_end:
            _add(session, found, run, "stale_forecast", "WARNING", f"The latest forecast for this series covered periods up to {last_end.isoformat()} and no newer run exists.", {"horizon_end": last_end.isoformat(), "today": today.isoformat()}, last_end.isoformat())  # fmt: skip
        try:
            ev, _ = evaluate_run(session, run, requested_by, finalization_days, now)
        except ValueError:
            continue
        final = [m for m in (ev.matches or []) if m["status"] == "final"]
        if len(final) >= MIN_EVAL_N:
            mt = ev.metrics["final"]
            live = mt["mase"]["value"]
            back = (
                (((run.evaluation or {}).get(run.selected_model or "") or {}).get("overall") or {})
                .get("mase", {})
                .get("value")
            )
            if live is not None and back is not None and back > 0 and live > DETERIORATION_FACTOR * back:
                _add(session, found, run, "deterioration", "WARNING", f"Live MASE {live:.2f} is more than {DETERIORATION_FACTOR}× the backtest MASE {back:.2f} over {len(final)} final periods.", {"live_mase": live, "backtest_mase": back, "n": len(final)}, "mase")  # fmt: skip
            sa = sum(m["actual"] for m in final)
            errs = [m["forecast"] - m["actual"] for m in final]
            if sa > 0:
                rel = sum(errs) / sa
                same = max(sum(1 for e in errs if e > 0), sum(1 for e in errs if e < 0)) / len(errs)
                if abs(rel) >= BIAS_REL_THRESHOLD and same >= 0.8:
                    _add(session, found, run, "sustained_bias", "WARNING", f"Forecasts have been {'above' if rel > 0 else 'below'} actuals by {abs(rel):.0%} in {same:.0%} of {len(final)} final periods.", {"relative_bias": rel, "same_sign_share": same, "n": len(final)}, "bias" + ("+" if rel > 0 else "-"))  # fmt: skip
        # input quality: re-prepare today's data with the run's own configuration
        conf = ForecastConfig.model_validate({**run.config, "origin": None})
        obs: list[RawObs] = load_obs(session, run.product_id, run.facility_id, limits.max_observations)
        cur = prepare(obs, conf, limits)
        was = (run.preparation or {}).get("counts", {})
        n_was, n_now = max(1, was.get("periods", 1)), max(1, cur.counts.get("periods", 1))
        d_missing = cur.counts.get("missing_periods", 0) / n_now - was.get("missing_periods", 0) / n_was
        if not cur.usable and (run.preparation or {}).get("status") == "ok":
            _add(session, found, run, "input_quality_change", "WARNING", f"The series can no longer be prepared for forecasting ({cur.status}).", {"status_now": cur.status, "warnings": cur.warnings[-2:]}, cur.status)  # fmt: skip
        elif d_missing > 0.15:
            _add(session, found, run, "input_quality_change", "WARNING", f"The share of missing periods rose by {d_missing:.0%} since the forecast was issued.", {"missing_share_change": d_missing}, "missing")  # fmt: skip
    return found
