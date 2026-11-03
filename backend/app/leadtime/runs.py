"""Bounded, idempotent, cancellable lead-time model runs (QUEUED -> RUNNING -> COMPLETED | PARTIAL | FAILED | CANCELLED).

A run reads delivery snapshots/events and writes only ``leadtime_runs`` (and audit). It never changes deliveries, inventory,
orders or registry data. A terminal run is immutable and is the model version that assessments cite.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.forecast.runs import BudgetExceeded
from app.leadtime.config import ALGORITHM_VERSION, LeadTimeConfig, LeadTimeLimits
from app.leadtime.pipeline import run_pipeline
from app.leadtime.resolve import load_deliveries
from app.models.delivery import DeliverySnapshot, LeadTimeRun
from app.models.enums import SimStatus
from app.models.types import utcnow
from app.scenarios.engine import RunCancelled

logger = logging.getLogger("atlas.leadtime")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
STALE_LOCK_SECONDS = 600


def limits_from(settings: Any) -> LeadTimeLimits:
    return LeadTimeLimits(
        max_records=settings.leadtime_max_records_per_run, max_active_runs=settings.leadtime_max_active_runs,
        time_budget_seconds=settings.leadtime_time_budget_seconds,
    )  # fmt: skip


def create_run(session: Session, conf: LeadTimeConfig, requested_by: str, limits: LeadTimeLimits, *, idempotency_key: str | None = None) -> tuple[LeadTimeRun, bool]:  # fmt: skip
    chash = conf.digest()
    if idempotency_key:
        prior = session.scalar(select(LeadTimeRun).where(LeadTimeRun.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.config_hash) != (requested_by, chash):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    q = select(func.count()).select_from(DeliverySnapshot)
    for col, val in ((DeliverySnapshot.supplier_id, conf.supplier_id), (DeliverySnapshot.destination_facility_id, conf.destination_facility_id), (DeliverySnapshot.product_id, conf.product_id)):  # fmt: skip
        if val is not None:
            q = q.where(col == val)
    n = session.scalar(q) or 0
    if n == 0:
        raise DomainValidationError("No delivery records match this scope; there is nothing to train or evaluate on.")
    if n > limits.max_records * 5:
        raise DomainValidationError(
            f"The scope holds {n} snapshots, above the supported bound. Narrow it by supplier, destination or product."
        )
    now = utcnow()
    cutoff = now - timedelta(seconds=STALE_LOCK_SECONDS)
    for old in session.scalars(select(LeadTimeRun).where(LeadTimeRun.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = session.scalar(select(func.count()).select_from(LeadTimeRun).where(LeadTimeRun.status.in_(ACTIVE))) or 0
    if active >= limits.max_active_runs:
        raise ConflictError(
            f"{active} lead-time runs are already queued or running (limit {limits.max_active_runs}). Try again shortly."
        )
    run = LeadTimeRun(
        run_uid=f"LT-{uuid.uuid4().hex[:16].upper()}", status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key,
        target=conf.target, config=conf.canonical(), config_hash=chash, algorithm_version=ALGORITHM_VERSION, as_of=now, queued_at=now,
        warnings=[], failures=[],
    )  # fmt: skip
    session.add(run)
    session.flush()
    return run, True


def cancel_run(session: Session, run: LeadTimeRun) -> LeadTimeRun:
    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {run.status.value} run cannot be cancelled.")
    return run


def execute_run(factory: sessionmaker[Session], run_id: int, limits: LeadTimeLimits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(LeadTimeRun).where(LeadTimeRun.id == run_id, LeadTimeRun.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
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
            logger.exception("lead-time run failed", extra={"run": run_id})
            _finish(session, run_id, SimStatus.FAILED, f"unexpected {type(exc).__name__}")
    finally:
        session.close()


def _finish(session: Session, run_id: int, status: SimStatus, err: str) -> None:
    session.execute(update(LeadTimeRun).where(LeadTimeRun.id == run_id, LeadTimeRun.status == SimStatus.RUNNING).values(status=status, finished_at=utcnow(), error=err))  # fmt: skip
    session.commit()


def _execute(session: Session, run_id: int, limits: LeadTimeLimits) -> None:
    t0 = time.monotonic()
    run = session.get(LeadTimeRun, run_id)
    conf = LeadTimeConfig.model_validate(run.config)
    as_of = run.as_of

    def check() -> None:
        session.execute(update(LeadTimeRun).where(LeadTimeRun.id == run_id).values(heartbeat_at=utcnow()))
        session.commit()
        if session.scalar(select(LeadTimeRun.cancel_requested).where(LeadTimeRun.id == run_id)):
            raise RunCancelled
        if time.monotonic() - t0 > limits.time_budget_seconds:
            raise BudgetExceeded

    deliveries = load_deliveries(session, as_of=as_of, supplier_id=conf.supplier_id, destination_id=conf.destination_facility_id, product_id=conf.product_id, limit=limits.max_records)  # fmt: skip
    if len(deliveries) > limits.max_records:
        run = session.get(LeadTimeRun, run_id)
        run.status, run.finished_at, run.error = SimStatus.FAILED, utcnow(), f"scope has more than {limits.max_records} order lines; narrow it by supplier, destination or product"  # fmt: skip
        session.commit()
        return
    check()
    res = run_pipeline(deliveries, conf, limits, as_of, check)
    run = session.get(LeadTimeRun, run_id)
    prep = res.get("preparation", {})
    run.result_status = res["status"]
    run.data_snapshot_hash = prep.get("data_hash")
    run.preparation, run.splits, run.candidates = prep, res.get("splits"), res.get("candidates")
    run.evaluation, run.selection, run.calibration, run.intervals = res.get("evaluation"), res.get("selection"), res.get("calibration"), res.get("intervals")  # fmt: skip
    run.explanation, run.leakage_controls, run.reproducibility, run.timings = res.get("explanation"), res.get("leakage_controls"), res.get("reproducibility"), res.get("timings")  # fmt: skip
    run.warnings = res.get("warnings", [])
    failed = (res.get("candidates") or {}).get("failed") or {}
    run.failures = [{"model": k, "reason": v} for k, v in failed.items()]
    if res["status"] == "completed":
        run.selected_model = res.get("selected_model")
        art = dict(res.get("artifact") or {})
        if res.get("score_kind"):
            art["score_kind"] = res["score_kind"]
        run.artifact = art
        run.status = SimStatus.PARTIAL if failed else SimStatus.COMPLETED
    else:
        run.status = SimStatus.COMPLETED  # the run did its job: it declined, and says why
        run.warnings = [*run.warnings, f"No model was produced: {res.get('decline_reason', res['status'])}"]
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - t0) * 1000)
    session.commit()


def train_now(factory: sessionmaker[Session], conf: LeadTimeConfig, requested_by: str, limits: LeadTimeLimits, *, idempotency_key: str | None = None) -> LeadTimeRun:  # fmt: skip
    """Create and execute synchronously (tests and tooling)."""
    s = factory()
    try:
        run, _c = create_run(s, conf, requested_by, limits, idempotency_key=idempotency_key)
        s.commit()
        rid = run.id
    finally:
        s.close()
    execute_run(factory, rid, limits)
    s = factory()
    try:
        r = s.get(LeadTimeRun, rid)
        s.expunge(r)
        return r
    finally:
        s.close()


def get_run(session: Session, run_id: int) -> LeadTimeRun:
    r = session.get(LeadTimeRun, run_id)
    if r is None:
        raise NotFoundError("Lead-time run not found.")
    return r
