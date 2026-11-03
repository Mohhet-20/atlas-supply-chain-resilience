"""Bounded, idempotent, cancellable forecast runs (QUEUED -> RUNNING -> COMPLETED | PARTIAL | FAILED | CANCELLED).

A run reads ``demand_observations`` for one product-facility series and writes only ``forecast_*`` rows (and audit). It
never changes demand, inventory, orders or any registry data. A terminal run is immutable.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.forecast.config import ALGORITHM_VERSION, PERIOD_DAYS, ForecastConfig, ForecastLimits
from app.forecast.pipeline import run_pipeline
from app.forecast.prep import RawObs
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.forecast import ForecastRun, ForecastValue
from app.models.inventory import DemandObservation
from app.models.product import Product
from app.models.types import utcnow
from app.scenarios.engine import RunCancelled

logger = logging.getLogger("atlas.forecast")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
OK_RESULTS = ("completed", "completed_with_fallback")


class BudgetExceeded(Exception):
    """The run used more than its compute budget."""


class RunLimits:
    def __init__(self, limits: ForecastLimits | None = None, max_active: int = 4, time_budget_seconds: float = 60.0, finalization_days: int = 7) -> None:  # fmt: skip
        self.fc = limits or ForecastLimits()
        self.max_active, self.time_budget_seconds, self.finalization_days = (
            max_active,
            time_budget_seconds,
            finalization_days,
        )


def limits_from(settings: Any) -> RunLimits:
    return RunLimits(
        ForecastLimits(
            max_horizon_daily=settings.forecast_max_horizon_daily, max_horizon_weekly=settings.forecast_max_horizon_weekly,
            max_history_periods=settings.forecast_max_history_periods, max_folds=settings.forecast_max_folds,
            max_candidates=settings.forecast_max_candidates, max_concurrent_runs=settings.forecast_max_active_runs,
        ),
        settings.forecast_max_active_runs, settings.forecast_time_budget_seconds, settings.forecast_finalization_days,
    )  # fmt: skip


def create_run(session: Session, product_id: int, facility_id: int, conf: ForecastConfig, requested_by: str, limits: RunLimits, *, idempotency_key: str | None = None) -> tuple[ForecastRun, bool]:  # fmt: skip
    chash = conf.digest()
    if idempotency_key:
        prior = session.scalar(select(ForecastRun).where(ForecastRun.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.product_id, prior.facility_id, prior.config_hash) != (requested_by, product_id, facility_id, chash):  # fmt: skip
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    if session.get(Product, product_id) is None:
        raise NotFoundError("Product not found.")
    if session.get(Facility, facility_id) is None:
        raise NotFoundError("Facility not found.")
    try:
        conf.check_limits(limits.fc)
    except ValueError as e:
        raise DomainValidationError(str(e)) from e
    n_obs = session.scalar(select(func.count()).select_from(DemandObservation).where(DemandObservation.product_id == product_id, DemandObservation.facility_id == facility_id)) or 0  # fmt: skip
    if n_obs == 0:
        raise DomainValidationError(
            "No demand observations exist for this product at this facility; there is nothing to forecast."
        )
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.fc.stale_lock_seconds)
    for old in session.scalars(select(ForecastRun).where(ForecastRun.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = session.scalar(select(func.count()).select_from(ForecastRun).where(ForecastRun.status.in_(ACTIVE))) or 0
    if active >= limits.max_active:
        raise ConflictError(
            f"{active} forecast runs are already queued or running (limit {limits.max_active}). Try again shortly."
        )
    run = ForecastRun(
        run_uid=f"FC-{uuid.uuid4().hex[:16].upper()}", status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key,
        product_id=product_id, facility_id=facility_id, frequency=conf.frequency, horizon=conf.horizon, unit=conf.unit,
        config=conf.canonical(), config_hash=chash, algorithm_version=ALGORITHM_VERSION, queued_at=now, warnings=[], failures=[],
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run, True


def cancel_run(session: Session, run: ForecastRun) -> ForecastRun:
    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {run.status.value} run cannot be cancelled.")
    return run


def load_obs(session: Session, product_id: int, facility_id: int, limit: int) -> list[RawObs]:
    rows = session.scalars(
        select(DemandObservation)
        .where(DemandObservation.product_id == product_id, DemandObservation.facility_id == facility_id)
        .order_by(DemandObservation.period_start, DemandObservation.id)
        .limit(limit + 1)  # fmt: skip
    ).all()
    return [RawObs(r.id, r.kind.value, r.period_start, r.period_end, r.quantity, r.unit, r.row_hash) for r in rows]


def execute_run(factory: sessionmaker[Session], run_id: int, limits: RunLimits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(ForecastRun).where(ForecastRun.id == run_id, ForecastRun.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        try:
            _execute(session, run_id, limits)
        except RunCancelled:
            session.rollback()
            _finish(session, run_id, SimStatus.CANCELLED, "cancelled by request")
        except BudgetExceeded:
            session.rollback()
            _finish(session, run_id, SimStatus.FAILED, "compute budget exceeded")
        except Exception as exc:
            session.rollback()
            logger.exception("forecast run failed", extra={"run": run_id})
            _finish(session, run_id, SimStatus.FAILED, f"unexpected {type(exc).__name__}")
    finally:
        session.close()


def _finish(session: Session, run_id: int, status: SimStatus, err: str) -> None:
    session.execute(update(ForecastRun).where(ForecastRun.id == run_id, ForecastRun.status == SimStatus.RUNNING).values(status=status, finished_at=utcnow(), error=err))  # fmt: skip
    session.commit()


def _execute(session: Session, run_id: int, limits: RunLimits) -> None:
    t0 = time.monotonic()
    run = session.get(ForecastRun, run_id)
    conf = ForecastConfig.model_validate(run.config)

    def check() -> None:
        session.execute(update(ForecastRun).where(ForecastRun.id == run_id).values(heartbeat_at=utcnow()))
        session.commit()
        if session.scalar(select(ForecastRun.cancel_requested).where(ForecastRun.id == run_id)):
            raise RunCancelled
        if time.monotonic() - t0 > limits.time_budget_seconds:
            raise BudgetExceeded

    obs = load_obs(session, run.product_id, run.facility_id, limits.fc.max_observations)
    res = run_pipeline(obs, conf, limits.fc, check)
    run = session.get(ForecastRun, run_id)
    prep = res.get("preparation", {})
    run.result_status = res["status"]
    run.unit = prep.get("unit") or run.unit
    run.data_snapshot_hash = prep.get("snapshot_hash") or None
    run.preparation, run.diagnostics, run.patterns = prep, res.get("diagnostics"), res.get("patterns")
    run.candidates, run.validation, run.evaluation, run.selection = res.get("candidates"), res.get("validation"), res.get("evaluation"), res.get("selection")  # fmt: skip
    run.interval_calibration, run.fallback, run.clamping = res.get("interval_calibration"), res.get("fallback"), res.get("nonnegative_clamping")  # fmt: skip
    run.reproducibility, run.timings, run.warnings, run.failures = res.get("reproducibility"), res.get("timings"), res.get("warnings", []), []  # fmt: skip
    if res["status"] in OK_RESULTS:
        m = res["model"]
        run.selected_model, run.model_version, run.model_params = m["key"], m["version"], m["params"]
        run.origin = date.fromisoformat(res["origin"])
        run.training_interval = res["training_interval"]
        step = PERIOD_DAYS[conf.frequency]
        with session.begin_nested():
            for v in res["values"]:
                ps = run.origin + timedelta(days=step * (v["h"] - 1))
                session.add(ForecastValue(run_id=run_id, step=v["h"], period_start=ps, period_end=ps + timedelta(days=step), point=v["point"], point_raw=v["point_raw"], intervals=v["intervals"]))  # fmt: skip
        run.status = SimStatus.PARTIAL if res["status"] == "completed_with_fallback" else SimStatus.COMPLETED
    else:
        run.status = SimStatus.COMPLETED  # the run did its job: it declined to forecast, and says why
        run.warnings = [*run.warnings, f"No forecast was produced: {res.get('decline_reason', res['status'])}"]
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - t0) * 1000)
    session.commit()


def forecast_now(factory: sessionmaker[Session], product_id: int, facility_id: int, requested_by: str, limits: RunLimits, conf: ForecastConfig | None = None, *, idempotency_key: str | None = None) -> ForecastRun:  # fmt: skip
    """Create and execute synchronously (tests and tooling)."""
    s = factory()
    try:
        run, _c = create_run(
            s, product_id, facility_id, conf or ForecastConfig(), requested_by, limits, idempotency_key=idempotency_key
        )
        s.commit()
        rid = run.id
    finally:
        s.close()
    execute_run(factory, rid, limits)
    s = factory()
    try:
        return s.get(ForecastRun, rid)
    finally:
        s.close()
