"""Bounded, idempotent, cancellable cascading impact analyses.

An analysis derives from one finished simulation run. Creating it never changes the run, the scenario or any registry /
relationship data; it writes only ``impact_*`` tables. A rerun creates a new analysis; completed ones are never
overwritten. Status transitions mirror simulation runs: QUEUED -> RUNNING -> COMPLETED | PARTIAL | FAILED | CANCELLED.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.impact import config as cfg
from app.impact.engine import ImpactLimits, run_analysis
from app.models.enums import SimStatus
from app.models.impact import ImpactAlternative, ImpactAnalysis, ImpactCriticality, ImpactEdgeSnapshot, ImpactFinding
from app.models.scenario import SimulationRun
from app.models.types import utcnow
from app.scenarios.engine import RunCancelled

logger = logging.getLogger("atlas.impact")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)
ELIGIBLE = (SimStatus.COMPLETED, SimStatus.PARTIAL)


@dataclass(frozen=True)
class AnalysisLimits:
    engine: ImpactLimits = ImpactLimits()
    max_active: int = 3
    stale_lock_seconds: int = 600


def limits_from(settings: Any) -> AnalysisLimits:
    return AnalysisLimits(
        engine=ImpactLimits(
            max_depth=settings.scenario_max_depth, max_nodes=settings.scenario_max_nodes, max_edges=settings.scenario_max_edges,
            time_budget_seconds=settings.impact_time_budget_seconds, max_path_steps=settings.impact_max_path_steps,
            max_findings=settings.impact_max_findings,
        ),
        max_active=settings.impact_max_active_analyses,
    )  # fmt: skip


def new_uid() -> str:
    return f"IA-{uuid.uuid4().hex[:16].upper()}"


def create_analysis(
    session: Session,
    run: SimulationRun,
    conf: cfg.ImpactConfig,
    requested_by: str,
    limits: AnalysisLimits,
    *,
    idempotency_key: str | None = None,
) -> tuple[ImpactAnalysis, bool]:
    """Insert a QUEUED analysis. Returns (analysis, created); a repeated idempotency key returns the first one."""
    if idempotency_key:
        prior = session.scalar(select(ImpactAnalysis).where(ImpactAnalysis.idempotency_key == idempotency_key))
        if prior is not None:
            if prior.run_id != run.id or prior.requested_by != requested_by:
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    if run.status not in ELIGIBLE:
        raise DomainValidationError(
            f"Simulation run {run.id} is {run.status.value}. Only COMPLETED or PARTIAL runs can feed a cascading analysis."
        )
    if not run.scenario_snapshot or not run.scenario_snapshot.get("events"):
        raise DomainValidationError("The simulation run has no stored scenario definition to analyse.")
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(ImpactAnalysis).where(ImpactAnalysis.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:  # the owning process died: release the lock and say so
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = (
        session.scalar(select(func.count()).select_from(ImpactAnalysis).where(ImpactAnalysis.status.in_(ACTIVE))) or 0
    )
    if active >= limits.max_active:
        raise ConflictError(
            f"{active} analyses are already queued or running (limit {limits.max_active}). Try again shortly."
        )
    if conf.reference_date is None:
        conf = conf.model_copy(update={"reference_date": now.astimezone(timezone.utc).date()})
    a = ImpactAnalysis(
        analysis_uid=new_uid(), run_id=run.id, scenario_id=run.scenario_id, scenario_version=run.scenario_version,
        status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key, config=conf.model_dump(mode="json"),
        config_hash=cfg.config_hash(conf), algorithm_version=cfg.ALGORITHM_VERSION, source_engine_version=run.engine_version,
        source_run_graph_hash=run.graph_hash, queued_at=now, truncated=False, truncation_reasons=[], warnings=[], failures=[],
    )  # fmt: skip
    session.add(a)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError("An analysis of this simulation run is already queued or running.") from None
    return a, True


def cancel_analysis(session: Session, a: ImpactAnalysis) -> ImpactAnalysis:
    if a.status is SimStatus.QUEUED:
        a.status, a.finished_at, a.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif a.status is SimStatus.RUNNING:
        a.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {a.status.value} analysis cannot be cancelled.")
    return a


def execute_analysis(factory: sessionmaker[Session], analysis_id: int, limits: AnalysisLimits) -> None:
    session = factory()
    try:
        claimed = session.execute(
            update(ImpactAnalysis).where(ImpactAnalysis.id == analysis_id, ImpactAnalysis.status == SimStatus.QUEUED)
            .values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())
        ).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        try:
            _execute(session, analysis_id, limits)
        except RunCancelled:
            session.rollback()
            a = session.get(ImpactAnalysis, analysis_id)
            a.status, a.finished_at, a.error = SimStatus.CANCELLED, utcnow(), "cancelled by request"
            session.commit()
        except Exception as exc:  # never leave an analysis RUNNING; the message is safe (type only)
            session.rollback()
            logger.exception("impact analysis failed", extra={"analysis": analysis_id})
            a = session.get(ImpactAnalysis, analysis_id)
            a.status, a.finished_at, a.error = SimStatus.FAILED, utcnow(), f"unexpected {type(exc).__name__}"
            session.commit()
    finally:
        session.close()


def _execute(session: Session, analysis_id: int, limits: AnalysisLimits) -> None:
    t0 = time.monotonic()
    a = session.get(ImpactAnalysis, analysis_id)
    run = session.get(SimulationRun, a.run_id)
    conf = cfg.ImpactConfig.model_validate(a.config)

    def checkpoint() -> None:
        session.execute(update(ImpactAnalysis).where(ImpactAnalysis.id == analysis_id).values(heartbeat_at=utcnow()))
        session.commit()
        if session.scalar(select(ImpactAnalysis.cancel_requested).where(ImpactAnalysis.id == analysis_id)):
            raise RunCancelled

    res = run_analysis(session, run, conf, limits.engine, checkpoint=checkpoint)
    a = session.get(ImpactAnalysis, analysis_id)
    with session.begin_nested():
        for f in res.findings:
            session.add(ImpactFinding(analysis_id=analysis_id, **f))
        for c in res.criticality:
            session.add(ImpactCriticality(analysis_id=analysis_id, **c))
        for alt in res.alternatives:
            session.add(ImpactAlternative(analysis_id=analysis_id, **alt))
        for s in res.edge_snapshots:
            session.add(ImpactEdgeSnapshot(analysis_id=analysis_id, **s))
    duration = time.monotonic() - t0
    reasons = list(res.truncation_reasons)
    if duration > limits.engine.time_budget_seconds * 1.5 and "time_budget" not in reasons:
        reasons.append("time_budget")
    a.graph_hash, a.graph_basis = res.graph_hash, res.graph_basis
    a.summary, a.coverage, a.uncertainty, a.events = res.summary, res.coverage, res.uncertainty, res.events
    a.warnings, a.failures, a.excluded = res.warnings, res.failures, res.excluded
    a.truncated = bool(reasons)
    a.truncation_reasons = sorted(set(reasons))
    a.status = SimStatus.PARTIAL if (reasons or res.failures) else SimStatus.COMPLETED
    a.finished_at = utcnow()
    a.duration_ms = int(duration * 1000)
    session.commit()


def analyze_now(
    factory: sessionmaker[Session], run_id: int, requested_by: str, limits: AnalysisLimits,
    conf: cfg.ImpactConfig | None = None, *, idempotency_key: str | None = None,
) -> ImpactAnalysis:  # fmt: skip
    """Create and execute synchronously (tests and tooling)."""
    s = factory()
    try:
        run = s.get(SimulationRun, run_id)
        if run is None:
            raise NotFoundError("Simulation run not found.")
        a, _c = create_analysis(
            s, run, conf or cfg.ImpactConfig(), requested_by, limits, idempotency_key=idempotency_key
        )
        s.commit()
        aid = a.id
    finally:
        s.close()
    execute_analysis(factory, aid, limits)
    s = factory()
    try:
        return s.get(ImpactAnalysis, aid)
    finally:
        s.close()
