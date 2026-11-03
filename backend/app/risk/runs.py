"""Bounded, idempotent assessment runs.

A run assesses a scope (one supplier, a small group, or the active network), writes a new assessment only when the
result changed, and records exactly what happened. A run is COMPLETED only if every in-scope supplier was assessed;
failures, an exceeded time budget or a supplier cap make it PARTIAL and say so.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, EntityMergedError, NotFoundError
from app.models.enums import AssessmentStatus, EntityStatus, EvalStatus, RiskRunScope
from app.models.risk import RiskRun
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.risk import alerts as alert_rules
from app.risk import inputs, service
from app.risk.methodology import RiskConfig, config_hash
from app.risk.scoring import assess
from app.risk.types import ReachFact

logger = logging.getLogger("atlas.risk")
MAX_GROUP = 100
MAX_FAILURES = 50


@dataclass(frozen=True)
class Limits:
    max_suppliers: int = 2_000
    chunk: int = 50
    time_budget_seconds: float = 20.0
    stale_lock_seconds: int = 600


DEFAULT_LIMITS = Limits()


def limits_from(settings: Any) -> Limits:
    return Limits(
        max_suppliers=settings.risk_run_max_suppliers, time_budget_seconds=settings.risk_run_time_budget_seconds
    )


def _scope_key(scope: RiskRunScope, ref: dict[str, Any]) -> str:
    if scope is RiskRunScope.NETWORK:
        return "NETWORK"
    if scope is RiskRunScope.SUPPLIER:
        return f"SUPPLIER:{ref['supplier_id']}"
    return "SUPPLIERS:" + hashlib.sha256(",".join(map(str, ref["supplier_ids"])).encode()).hexdigest()[:24]


def normalize_scope(session: Session, scope: RiskRunScope, ref: dict[str, Any] | None) -> dict[str, Any]:
    ref = dict(ref or {})
    if scope is RiskRunScope.NETWORK:
        return {}
    if scope is RiskRunScope.SUPPLIER:
        try:
            sid = int(ref.get("supplier_id", 0))
        except (TypeError, ValueError):
            raise DomainValidationError("supplier scope needs a supplier_id.") from None
        service.get_supplier(session, sid)  # 404 / 409 merged
        return {"supplier_id": sid}
    raw = ref.get("supplier_ids")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_GROUP:
        raise DomainValidationError(f"suppliers scope needs 1-{MAX_GROUP} supplier_ids.")
    try:
        ids = sorted({int(x) for x in raw})
    except (TypeError, ValueError):
        raise DomainValidationError("supplier_ids must be integers.") from None
    found = {s.id: s for s in session.scalars(select(Supplier).where(Supplier.id.in_(ids)))}
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFoundError(f"{len(missing)} supplier id(s) not found (first: {missing[0]}).")
    merged = [i for i in ids if found[i].merged_into_id is not None]
    if merged:
        raise EntityMergedError(
            f"Supplier {merged[0]} was merged into {found[merged[0]].merged_into_id}; use the surviving record."
        )
    return {"supplier_ids": ids}


def create_run(session: Session, scope: RiskRunScope, ref: dict[str, Any] | None, requested_by: str, *, trigger: str = "API",
               reason: str | None = None, idempotency_key: str | None = None, limits: Limits = DEFAULT_LIMITS) -> tuple[RiskRun, bool]:  # fmt: skip
    """Insert a QUEUED run. Returns (run, created). Same idempotency key + same scope returns the existing run."""
    canon = normalize_scope(session, scope, ref)
    key = _scope_key(scope, canon)
    if idempotency_key:
        prior = session.scalar(select(RiskRun).where(RiskRun.idempotency_key == idempotency_key))
        if prior is not None:
            if prior.scope_key != key or prior.requested_by != requested_by:
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(
        select(RiskRun).where(RiskRun.status.in_([EvalStatus.QUEUED, EvalStatus.RUNNING]), RiskRun.scope_key == key)
    ).all():
        if (old.heartbeat_at or old.queued_at) < cutoff:  # the owning process died: release the lock and say so
            old.status, old.finished_at, old.error = EvalStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    run = RiskRun(
        run_uid=service.new_uid("RR"), scope_type=scope, scope_ref=canon, scope_key=key, status=EvalStatus.QUEUED, trigger=trigger,
        reason=(reason or None) and reason[:200], requested_by=requested_by, idempotency_key=idempotency_key, queued_at=now, examined=0, assessed=0,
        partial=0, insufficient=0, created=0, unchanged=0, failed=0, skipped=0, alerts_created=0, alerts_updated=0, failures=[], truncated=False,
        truncation_reasons=[],
    )  # fmt: skip
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError("An assessment run for this scope is already queued or running.") from None
    return run, True


def _target_ids(session: Session, run: RiskRun, limit: int) -> tuple[list[int], int]:
    """Supplier ids in scope (oldest id first) and how many were left out by the supplier cap."""
    ref = run.scope_ref or {}
    if run.scope_type is RiskRunScope.SUPPLIER:
        return [ref["supplier_id"]], 0
    if run.scope_type is RiskRunScope.SUPPLIERS:
        return list(ref["supplier_ids"]), 0
    ids = list(session.scalars(
        select(Supplier.id)
        .where(Supplier.merged_into_id.is_(None), Supplier.status.in_([EntityStatus.ACTIVE, EntityStatus.ON_HOLD]))
        .order_by(Supplier.id)
    ))  # fmt: skip
    return ids[:limit], max(0, len(ids) - limit)


def execute_run(
    factory: sessionmaker[Session],
    run_id: int,
    cfg: RiskConfig,
    limits: Limits = DEFAULT_LIMITS,
    now: datetime | None = None,
) -> RiskRun:
    session = factory()
    try:
        claimed = session.execute(
            update(RiskRun)
            .where(RiskRun.id == run_id, RiskRun.status == EvalStatus.QUEUED)
            .values(status=EvalStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())
        ).rowcount  # atomic claim: a second executor of the same run does nothing
        session.commit()
        run = session.get(RiskRun, run_id)
        if run is None:
            raise NotFoundError("Run not found.")
        if not claimed:
            return run
        try:
            _execute(session, run, cfg, limits, now)
        except Exception as exc:  # never leave a run RUNNING
            session.rollback()
            logger.exception("risk run failed", extra={"run": run_id})
            run = session.get(RiskRun, run_id)
            run.status, run.finished_at, run.error = EvalStatus.FAILED, utcnow(), f"unexpected {type(exc).__name__}"
            session.commit()
        return run
    finally:
        session.close()


def _execute(session: Session, run: RiskRun, cfg: RiskConfig, limits: Limits, now_override: datetime | None) -> None:
    t0 = time.monotonic()
    started = utcnow()
    now = now_override or started
    run.methodology_version, run.methodology_hash = cfg.version, config_hash(cfg)
    run.graph_snapshot = inputs.graph_snapshot(session)
    ids, left_out = _target_ids(session, run, limits.max_suppliers)
    reasons: list[str] = []
    if left_out:
        reasons.append("supplier_limit")
    run.examined = 0
    session.commit()
    counters = alert_rules.AlertCounters()
    failures: list[dict[str, Any]] = []

    def fail(sid: int, error: str) -> None:
        run.failed += 1
        if len(failures) < MAX_FAILURES:
            failures.append({"supplier_id": sid, "error": error})

    out_of_time = False
    for start in range(0, len(ids), limits.chunk):
        if time.monotonic() - t0 > limits.time_budget_seconds:
            out_of_time = True
            break
        part = ids[start : start + limits.chunk]
        facts, net = inputs.load_facts(session, part, now.date())
        for sid in part:
            if time.monotonic() - t0 > limits.time_budget_seconds:
                out_of_time = True
                break
            run.examined += 1
            if sid not in facts or facts[sid].info.merged_into_id is not None:
                fail(sid, "supplier_missing_or_merged")
                continue
            try:
                reach = None
                if len(ids) <= 200:  # context only; skipped for very large runs to protect the time budget
                    r = inputs.load_reach(
                        session, sid, cfg.traversal.depth, cfg.traversal.max_nodes, cfg.traversal.max_edges
                    )
                    reach = ReachFact(r["depth"], r["nodes_by_type"], r["edges"], r["truncated"], tuple(r["reasons"]))
                result = assess(facts[sid], net, now.date(), now, cfg, reach)
                with session.begin_nested():
                    _row, outcome = service.store(session, run, sid, result, now, cfg, counters)
                run.created += outcome == "CREATED"
                run.unchanged += outcome == "UNCHANGED"
                if result.status == AssessmentStatus.ASSESSED.value:
                    run.assessed += 1
                elif result.status == AssessmentStatus.PARTIAL.value:
                    run.partial += 1
                else:
                    run.insufficient += 1
            except Exception as exc:  # one supplier failing never hides the others
                logger.warning("risk assessment failed", extra={"supplier": sid, "error": type(exc).__name__})
                fail(sid, type(exc).__name__)
        run.heartbeat_at = utcnow()
        run.alerts_created, run.alerts_updated = counters.created, counters.updated
        session.commit()
        if out_of_time:
            break
    if out_of_time:
        reasons.append("time_budget")
    if counters.suppressed:
        reasons.append("alert_limit")
    run.skipped = left_out + (len(ids) - run.examined)
    run.alerts_created, run.alerts_updated = counters.created, counters.updated
    run.failures = failures
    run.truncation_reasons = sorted(set(reasons))
    run.truncated = bool(reasons)
    successes = run.assessed + run.partial + run.insufficient
    if successes == 0 and run.failed > 0:
        run.status = EvalStatus.FAILED
    elif run.failed or run.skipped:
        run.status = EvalStatus.PARTIAL
    else:
        run.status = EvalStatus.COMPLETED
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - t0) * 1000)
    session.commit()


def assess_now(factory: sessionmaker[Session], scope: RiskRunScope, ref: dict[str, Any] | None, requested_by: str, cfg: RiskConfig, *,
               trigger: str = "API", limits: Limits = DEFAULT_LIMITS, now: datetime | None = None, idempotency_key: str | None = None,
               reason: str | None = None) -> RiskRun:  # fmt: skip
    """Create and execute a run synchronously (single-supplier assessment, tests, targeted reassessment)."""
    s = factory()
    try:
        run, created = create_run(
            s, scope, ref, requested_by, trigger=trigger, reason=reason, idempotency_key=idempotency_key, limits=limits
        )
        s.commit()
        run_id = run.id
    finally:
        s.close()
    return execute_run(factory, run_id, cfg, limits, now)
