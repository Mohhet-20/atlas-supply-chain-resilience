"""Bounded, idempotent, cancellable stockout analyses (QUEUED -> RUNNING -> COMPLETED | PARTIAL | FAILED | CANCELLED).

An analysis reads inventory, demand, replenishment and policy records (and, for a scenario, a finished impact analysis)
and writes only ``stockout_*`` rows. It never changes inventory, orders, allocations, policies or registry data.
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
from app.inventory import config as cfg
from app.inventory.analysis import InvLimits, run_analysis
from app.models.enums import SimStatus
from app.models.impact import ImpactAnalysis
from app.models.inventory import AnalysisKind, StockoutAnalysis, StockoutFinding
from app.models.types import utcnow
from app.scenarios.engine import RunCancelled

logger = logging.getLogger("atlas.inventory")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
ELIGIBLE = (SimStatus.COMPLETED, SimStatus.PARTIAL)


class Limits:
    def __init__(
        self,
        engine: InvLimits | None = None,
        max_active: int = 3,
        max_horizon_days: int = 365,
        stale_lock_seconds: int = 600,
    ) -> None:
        self.engine = engine or InvLimits()
        self.max_active, self.max_horizon_days, self.stale_lock_seconds = (
            max_active,
            max_horizon_days,
            stale_lock_seconds,
        )


def limits_from(settings: Any) -> Limits:
    return Limits(
        InvLimits(settings.inventory_max_pairs, settings.inventory_time_budget_seconds),
        settings.inventory_max_active_analyses,
        settings.inventory_max_horizon_days,
    )


def create_analysis(
    session: Session, conf: cfg.InventoryConfig, requested_by: str, limits: Limits, *, impact_analysis_id: int | None = None, idempotency_key: str | None = None,
) -> tuple[StockoutAnalysis, bool]:  # fmt: skip
    if idempotency_key:
        prior = session.scalar(select(StockoutAnalysis).where(StockoutAnalysis.idempotency_key == idempotency_key))
        if prior is not None:
            if prior.requested_by != requested_by or prior.impact_analysis_id != impact_analysis_id:
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    if conf.horizon_days > limits.max_horizon_days:
        raise DomainValidationError(f"horizon_days cannot exceed {limits.max_horizon_days} on this server.")
    if conf.scenario_start_offset_days >= conf.horizon_days:
        raise DomainValidationError("scenario_start_offset_days must be inside the horizon.")
    if impact_analysis_id is not None:
        ia = session.get(ImpactAnalysis, impact_analysis_id)
        if ia is None:
            raise NotFoundError("Impact analysis not found.")
        if ia.status not in ELIGIBLE:
            raise DomainValidationError(
                f"Impact analysis {ia.id} is {ia.status.value}. Only COMPLETED or PARTIAL analyses can feed a scenario analysis."
            )
    if conf.forecast_run_id is not None:
        from app.forecast import integration as fint

        fint.validate_selectable(session, conf.forecast_run_id)
    if conf.delivery_assessment_ids:
        from app.leadtime import integration as ltint

        ltint.validate_selectable(session, conf.delivery_assessment_ids)
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(StockoutAnalysis).where(StockoutAnalysis.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = (
        session.scalar(select(func.count()).select_from(StockoutAnalysis).where(StockoutAnalysis.status.in_(ACTIVE)))
        or 0
    )
    if active >= limits.max_active:
        raise ConflictError(
            f"{active} analyses are already queued or running (limit {limits.max_active}). Try again shortly."
        )
    as_of = conf.as_of or now
    if as_of > now + timedelta(minutes=5):
        raise DomainValidationError("as_of cannot be in the future; use the projection horizon to look ahead.")
    conf = conf.model_copy(update={"as_of": as_of})
    a = StockoutAnalysis(
        analysis_uid=f"SA-{uuid.uuid4().hex[:16].upper()}", kind=AnalysisKind.SCENARIO if impact_analysis_id else AnalysisKind.BASELINE, status=SimStatus.QUEUED,
        requested_by=requested_by, idempotency_key=idempotency_key, impact_analysis_id=impact_analysis_id, config=conf.model_dump(mode="json"),
        config_hash=cfg.config_hash(conf), algorithm_version=cfg.ALGORITHM_VERSION, as_of=as_of, queued_at=now, truncated=False, truncation_reasons=[], warnings=[], failures=[],
    )  # fmt: skip
    session.add(a)
    session.flush()
    return a, True


def cancel_analysis(session: Session, a: StockoutAnalysis) -> StockoutAnalysis:
    if a.status is SimStatus.QUEUED:
        a.status, a.finished_at, a.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif a.status is SimStatus.RUNNING:
        a.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {a.status.value} analysis cannot be cancelled.")
    return a


def execute_analysis(factory: sessionmaker[Session], analysis_id: int, limits: Limits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(StockoutAnalysis).where(StockoutAnalysis.id == analysis_id, StockoutAnalysis.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        try:
            _execute(session, analysis_id, limits)
        except RunCancelled:
            session.rollback()
            a = session.get(StockoutAnalysis, analysis_id)
            a.status, a.finished_at, a.error = SimStatus.CANCELLED, utcnow(), "cancelled by request"
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("stockout analysis failed", extra={"analysis": analysis_id})
            a = session.get(StockoutAnalysis, analysis_id)
            a.status, a.finished_at, a.error = SimStatus.FAILED, utcnow(), f"unexpected {type(exc).__name__}"
            session.commit()
    finally:
        session.close()


def _execute(session: Session, analysis_id: int, limits: Limits) -> None:
    t0 = time.monotonic()
    a = session.get(StockoutAnalysis, analysis_id)
    conf = cfg.InventoryConfig.model_validate(a.config)

    def checkpoint() -> None:
        session.execute(
            update(StockoutAnalysis).where(StockoutAnalysis.id == analysis_id).values(heartbeat_at=utcnow())
        )
        session.commit()
        if session.scalar(select(StockoutAnalysis.cancel_requested).where(StockoutAnalysis.id == analysis_id)):
            raise RunCancelled

    res = run_analysis(session, a, conf, limits.engine, scenario_impact_id=a.impact_analysis_id, checkpoint=checkpoint)
    a = session.get(StockoutAnalysis, analysis_id)
    with session.begin_nested():
        for f in res.findings:
            session.add(StockoutFinding(analysis_id=analysis_id, **f))
    reasons = sorted(set(res.truncation_reasons))
    a.input_hash, a.inputs, a.summary, a.coverage = res.input_hash, res.inputs, res.summary, res.coverage
    a.warnings, a.failures = res.warnings, res.failures
    a.truncated, a.truncation_reasons = bool(reasons), reasons
    a.status = SimStatus.PARTIAL if (reasons or res.failures) else SimStatus.COMPLETED
    a.finished_at = utcnow()
    a.duration_ms = int((time.monotonic() - t0) * 1000)
    session.commit()


def analyze_now(factory: sessionmaker[Session], requested_by: str, limits: Limits, conf: cfg.InventoryConfig | None = None, *, impact_analysis_id: int | None = None, idempotency_key: str | None = None) -> StockoutAnalysis:  # fmt: skip
    """Create and execute synchronously (tests and tooling)."""
    s = factory()
    try:
        a, _c = create_analysis(
            s,
            conf or cfg.InventoryConfig(),
            requested_by,
            limits,
            impact_analysis_id=impact_analysis_id,
            idempotency_key=idempotency_key,
        )
        s.commit()
        aid = a.id
    finally:
        s.close()
    execute_analysis(factory, aid, limits)
    s = factory()
    try:
        return s.get(StockoutAnalysis, aid)
    finally:
        s.close()
