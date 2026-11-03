"""Bridge from a forecast run to the inventory projection.

The inventory engine consumes a *per-day demand series* taken from a chosen forecast run. This module validates unit,
frequency and coverage and builds that series; it never extends a forecast past its horizon and never presents a
historical average as a forecast (a failed validation returns a labelled reason and the caller falls back to the
observed rate, clearly marked as such).

Day alignment: projection step ``d`` (closing of day ``d``) consumes the demand dated ``as_of_date + d − 1``.
Weekly forecasts are spread evenly across their seven days (an assumption that is recorded).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, NotFoundError
from app.forecast.prep import norm_unit
from app.forecast.runs import OK_RESULTS
from app.models.forecast import ForecastRun, ForecastValue

FORECAST_LABEL = "Forecast demand — a statistical estimate (MODEL_ESTIMATE), not an observation"
OBSERVED_LABEL = "Observed average rate over the demand window — a historical average, not a forecast"


def load_context(session: Session, run_id: int) -> dict[str, Any]:
    run = session.get(ForecastRun, run_id)
    if run is None:
        raise NotFoundError("Forecast run not found.")
    vals = session.scalars(
        select(ForecastValue).where(ForecastValue.run_id == run_id).order_by(ForecastValue.step)
    ).all()
    return {
        "run": run,
        "values": [(v.period_start, v.period_end, v.point, v.step) for v in vals],
        "pair": (run.product_id, run.facility_id),
    }


def validate_selectable(session: Session, run_id: int) -> dict[str, Any]:
    ctx = load_context(session, run_id)
    run = ctx["run"]
    if run.result_status not in OK_RESULTS or not ctx["values"]:
        raise DomainValidationError(f"Forecast run {run_id} produced no forecast values (status {run.result_status or run.status.value}); it cannot drive an inventory projection.")  # fmt: skip
    return ctx


def source_block(run: ForecastRun, **extra: Any) -> dict[str, Any]:
    fb = run.fallback or {}
    return {
        "source": "forecast", "label": FORECAST_LABEL, "forecast_run_id": run.id, "run_uid": run.run_uid, "model": run.selected_model,
        "model_version": run.model_version, "origin": run.origin.isoformat() if run.origin else None, "frequency": run.frequency,
        "unit": run.unit, "result_status": run.result_status, "validated": bool(run.result_status == "completed" and not fb),
        "fallback": run.fallback, "data_snapshot_hash": run.data_snapshot_hash, "config_hash": run.config_hash,
        "issued_at": run.finished_at.isoformat() if run.finished_at else None, **extra,
    }  # fmt: skip


def demand_series(
    ctx: dict[str, Any], *, unit: str | None, as_of_date: date, horizon_days: int, factor: float | None
) -> dict[str, Any]:
    run: ForecastRun = ctx["run"]
    if run.result_status not in OK_RESULTS or not ctx["values"]:
        return {
            "status": "no_values",
            "reason": f"Forecast run {run.id} produced no values ({run.result_status}).",
            "series": None,
        }
    mult, conversion = 1.0, None
    if norm_unit(run.unit) != norm_unit(unit):
        if factor is None or not unit or not run.unit:
            return {"status": "unit_mismatch", "series": None, "reason": f"The forecast is in '{run.unit}' but the inventory is in '{unit}'; no conversion was supplied, so none is applied."}  # fmt: skip
        mult = factor
        conversion = {
            "from": run.unit,
            "to": unit,
            "factor": factor,
            "basis": "analyst-supplied factor (recorded assumption)",
        }
    per_day: dict[date, float] = {}
    for ps, pe, point, _step in ctx["values"]:
        n = (pe - ps).days
        for k in range(n):
            per_day[ps + timedelta(days=k)] = point * mult / n
    series: list[float] = []
    for d in range(1, horizon_days + 1):
        day = as_of_date + timedelta(days=d - 1)
        if day not in per_day:
            break
        series.append(per_day[day])
    if not series:
        first = min(per_day)
        last = max(per_day)
        why = "begins after the inventory as-of date" if first > as_of_date else "ended before the inventory as-of date"
        return {"status": "not_covered", "series": None, "reason": f"The forecast covers {first.isoformat()}–{last.isoformat()} and {why}; there is no forecast demand for the projection start."}  # fmt: skip
    warns = []
    if len(series) < horizon_days:
        warns.append(
            f"The forecast covers {len(series)} of the {horizon_days} projection days; the projection stops there and says nothing beyond it."
        )
    if run.origin and (as_of_date - run.origin).days > 7:
        warns.append(f"The forecast was issued {(as_of_date - run.origin).days} days before the inventory as-of date.")
    if run.frequency == "weekly":
        warns.append("Weekly forecast values are spread evenly across their seven days.")
    return {"status": "usable", "series": series, "covered_days": len(series), "conversion": conversion, "warnings": warns, "reason": None, "mean_daily": sum(series) / len(series)}  # fmt: skip
