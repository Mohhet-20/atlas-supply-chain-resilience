"""Bounded, idempotent, cancellable simulation runs.

A run freezes the scenario definition, runs the pure engine over a read-only view of the network, and stores the results.
It never writes to registry, relationship, capacity or order data. A run is COMPLETED only if nothing was truncated or
failed; otherwise it is PARTIAL and says why. Status transitions: QUEUED -> RUNNING -> COMPLETED | PARTIAL | FAILED |
CANCELLED (and QUEUED -> CANCELLED).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.models.enums import RegistryType, ScenarioStatus, SimStatus
from app.models.facility import Facility
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.scenario import Scenario, SimulationAlternative, SimulationEdgeImpact, SimulationImpact, SimulationRun
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.scenarios import config as cfg
from app.scenarios import service
from app.scenarios.engine import EngineLimits, RunCancelled, run_engine
from app.scenarios.validation import Limits as ValidationLimits

logger = logging.getLogger("atlas.scenarios")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)


@dataclass(frozen=True)
class RunLimits:
    max_depth: int = 4
    max_nodes: int = 500
    max_edges: int = 1_000
    time_budget_seconds: float = 20.0
    max_active_runs: int = 3
    stale_lock_seconds: int = 600
    max_events: int = 25
    max_targets_per_event: int = 50

    def engine(self) -> EngineLimits:
        return EngineLimits(self.max_nodes, self.max_edges, self.max_depth, self.time_budget_seconds)

    def validation(self) -> ValidationLimits:
        return ValidationLimits(self.max_events, self.max_targets_per_event, self.max_depth)


def limits_from(settings: Any) -> RunLimits:
    return RunLimits(
        max_depth=settings.scenario_max_depth, max_nodes=settings.scenario_max_nodes, max_edges=settings.scenario_max_edges,
        time_budget_seconds=settings.scenario_time_budget_seconds, max_active_runs=settings.scenario_max_active_runs,
        max_events=settings.scenario_max_events, max_targets_per_event=settings.scenario_max_targets_per_event,
    )  # fmt: skip


def graph_snapshot(session: Session) -> dict[str, Any]:
    """Counts and newest change of what the engine reads. A reference for comparison, not an immutable copy."""

    def stat(model):  # noqa: ANN001
        n, m = session.execute(select(func.count(), func.max(model.updated_at))).one()
        return {"count": n, "latest_change": m.isoformat() if m else None}

    return {
        "suppliers": stat(Supplier), "facilities": stat(Facility), "products": stat(Product),
        "supply_relationships": stat(SupplyRelationship), "product_links": stat(ProductLink),
        "limitation": "References only; ATLAS does not keep immutable network snapshots. Use graph_hash to detect changed inputs.",
    }  # fmt: skip


def create_run(
    session: Session, scenario: Scenario, requested_by: str, limits: RunLimits, *, idempotency_key: str | None = None
) -> tuple[SimulationRun, bool]:
    """Insert a QUEUED run after re-validating. Returns (run, created); a repeated idempotency key returns the first run."""
    if idempotency_key:
        prior = session.scalar(select(SimulationRun).where(SimulationRun.idempotency_key == idempotency_key))
        if prior is not None:
            if prior.scenario_id != scenario.id or prior.requested_by != requested_by:
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    if scenario.status is ScenarioStatus.ARCHIVED:
        raise InvalidTransitionError("An archived scenario cannot be run. Restore it first.")
    report = service.validate_scenario(session, scenario, limits.validation(), mark=True)
    if not report["can_run"]:
        raise DomainValidationError(
            f"The scenario is not valid ({report['error_count']} error(s)). Validate it and fix the errors before running."
        )
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(
        select(SimulationRun).where(
            SimulationRun.status.in_(ACTIVE),
            SimulationRun.heartbeat_at.is_not(None) | (SimulationRun.queued_at < cutoff),
        )
    ):
        if (old.heartbeat_at or old.queued_at) < cutoff:  # the owning process died: release the lock and say so
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = (
        session.scalar(select(func.count()).select_from(SimulationRun).where(SimulationRun.status.in_(ACTIVE))) or 0
    )
    if active >= limits.max_active_runs:
        raise ConflictError(
            f"{active} simulations are already queued or running (limit {limits.max_active_runs}). Try again shortly."
        )
    defn = service.definition_of(session, scenario)
    run = SimulationRun(
        run_uid=service.new_uid("SR"), scenario_id=scenario.id, scenario_version=scenario.version, definition_hash=service.definition_hash(defn),
        scenario_snapshot=defn, status=SimStatus.QUEUED, requested_by=requested_by, idempotency_key=idempotency_key, queued_at=now,
        engine_version=cfg.ENGINE_VERSION, config_hash=cfg.config_hash(), assumptions=defn["scenario"]["assumptions"],
        entities_examined=0, entities_affected=0, direct_count=0, indirect_count=0, truncated=False, truncation_reasons=[],
        warnings=[], missing_data=[], failures=[],
    )  # fmt: skip
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError("A simulation for this scenario is already queued or running.") from None
    return run, True


def cancel_run(session: Session, run: SimulationRun) -> SimulationRun:
    if run.status is SimStatus.QUEUED:
        run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif run.status is SimStatus.RUNNING:
        run.cancel_requested = True  # the engine stops at its next checkpoint
    else:
        raise InvalidTransitionError(f"A {run.status.value} run cannot be cancelled.")
    return run


def execute_run(factory: sessionmaker[Session], run_id: int, limits: RunLimits) -> None:
    session = factory()
    try:
        claimed = session.execute(
            update(SimulationRun).where(SimulationRun.id == run_id, SimulationRun.status == SimStatus.QUEUED)
            .values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())
        ).rowcount  # fmt: skip
        session.commit()
        if not claimed:  # already running, finished or cancelled: a duplicate executor does nothing
            return
        try:
            _execute(session, run_id, limits)
        except RunCancelled:
            session.rollback()
            run = session.get(SimulationRun, run_id)
            run.status, run.finished_at, run.error = SimStatus.CANCELLED, utcnow(), "cancelled by request"
            session.commit()
        except Exception as exc:  # never leave a run RUNNING
            session.rollback()
            logger.exception("simulation run failed", extra={"run": run_id})
            run = session.get(SimulationRun, run_id)
            run.status, run.finished_at, run.error = SimStatus.FAILED, utcnow(), f"unexpected {type(exc).__name__}"
            session.commit()
    finally:
        session.close()


def _execute(session: Session, run_id: int, limits: RunLimits) -> None:
    t0 = time.monotonic()
    run = session.get(SimulationRun, run_id)
    run.graph_snapshot = graph_snapshot(session)
    session.commit()

    def checkpoint() -> None:
        session.execute(update(SimulationRun).where(SimulationRun.id == run_id).values(heartbeat_at=utcnow()))
        session.commit()
        if session.scalar(select(SimulationRun.cancel_requested).where(SimulationRun.id == run_id)):
            raise RunCancelled

    result = run_engine(
        session, session.get(SimulationRun, run_id).scenario_snapshot, limits.engine(), checkpoint=checkpoint
    )
    run = session.get(SimulationRun, run_id)
    with session.begin_nested():
        for i in result.impacts:
            session.add(SimulationImpact(run_id=run_id, entity_type=RegistryType(i["entity_type"]), **{k: v for k, v in i.items() if k != "entity_type" and k != "impact_kind" and k != "quantification"}, impact_kind=i["impact_kind"], quantification=i["quantification"]))  # fmt: skip
        for e in result.edge_impacts:
            session.add(SimulationEdgeImpact(run_id=run_id, **e))
        for a in result.alternatives:
            session.add(SimulationAlternative(run_id=run_id, **a))
    reasons = list(result.truncation_reasons)
    duration = time.monotonic() - t0
    if duration > limits.time_budget_seconds and "time_budget" not in reasons:
        reasons.append("time_budget")
    run.graph_hash = result.input_hash
    run.entities_examined = result.examined
    run.entities_affected = result.summary["entities_affected"]
    run.direct_count = result.summary["direct"]
    run.indirect_count = result.summary["indirect"]
    run.summary = result.summary
    run.warnings = result.warnings
    run.missing_data = result.missing_data
    run.failures = result.failures
    run.truncated = bool(reasons)
    run.truncation_reasons = sorted(set(reasons))
    run.status = SimStatus.PARTIAL if (reasons or result.failures) else SimStatus.COMPLETED
    run.finished_at = utcnow()
    run.duration_ms = int(duration * 1000)
    session.commit()


def run_now(
    factory: sessionmaker[Session],
    scenario_id: int,
    requested_by: str,
    limits: RunLimits,
    *,
    idempotency_key: str | None = None,
) -> SimulationRun:
    """Create and execute synchronously (tests and tooling)."""
    s = factory()
    try:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise NotFoundError("Scenario not found.")
        run, _created = create_run(s, sc, requested_by, limits, idempotency_key=idempotency_key)
        s.commit()
        run_id = run.id
    finally:
        s.close()
    execute_run(factory, run_id, limits)
    s = factory()
    try:
        return s.get(SimulationRun, run_id)
    finally:
        s.close()
