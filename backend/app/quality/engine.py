"""Bounded quality evaluation runs.

A run evaluates a scope (one entity, selected relationships, an import batch, a source, or the network) against the
enabled rules, reconciles findings, writes score snapshots and records exactly what happened. Runs are bounded by a
subject cap and a time budget; hitting either finishes the run PARTIAL with the reason, never COMPLETED.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.graph.rules import EdgeKind
from app.graph.store import link_rec, parse_ref, supply_rec
from app.models.enums import EntityKind, EvalScope, EvalStatus
from app.models.ingestion import IngestionBatch, RecordLineage
from app.models.product import ProductLink
from app.models.quality import QualityEvalRun, QualityRuleSetting, QualityScore
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource
from app.models.types import utcnow
from app.quality import scoring
from app.quality.context import Context, chunks
from app.quality.findings import Counters, load_existing, reconcile_subject
from app.quality.framework import ENTITY_KINDS, RuleSpec, Subject, catalog, catalog_hash
from app.registry.config import QualityConfig, config_hash
from app.registry.entities import MODELS

logger = logging.getLogger("atlas.quality")

_RTYPE = {EntityKind.SUPPLIER: "SUPPLIER", EntityKind.FACILITY: "FACILITY", EntityKind.PRODUCT: "PRODUCT"}
MAX_REFS = 200
MAX_FAILURES = 50


@dataclass(frozen=True)
class Limits:
    max_subjects: int = 5_000
    chunk: int = 200
    time_budget_seconds: float = 20.0
    stale_lock_seconds: int = 600


DEFAULT_LIMITS = Limits()


def limits_from(settings: Any) -> Limits:
    return Limits(
        max_subjects=settings.quality_eval_max_subjects, time_budget_seconds=settings.quality_eval_time_budget_seconds
    )


# -------------------------------------------------------------------------------------------------- rule state


def enabled_map(session: Session) -> dict[str, bool]:
    overrides = {r.rule_id: r.enabled for r in session.scalars(select(QualityRuleSetting))}
    return {rid: overrides.get(rid, spec.default_enabled) for rid, spec in catalog().items()}


def active_rules(session: Session) -> dict[str, RuleSpec]:
    en = enabled_map(session)
    return {rid: spec for rid, spec in catalog().items() if en[rid]}


# ------------------------------------------------------------------------------------------------------ scope


def _scope_key(scope: EvalScope, ref: dict[str, Any]) -> str:
    if scope is EvalScope.NETWORK:
        return "NETWORK"
    if scope is EvalScope.ENTITY:
        return f"ENTITY:{ref['entity_type']}:{ref['entity_id']}"
    if scope is EvalScope.BATCH:
        return f"BATCH:{ref['batch_id']}"
    if scope is EvalScope.SOURCE:
        return f"SOURCE:{ref['source_id']}"
    digest = hashlib.sha256(",".join(sorted(ref["refs"])).encode()).hexdigest()[:24]
    return f"RELATIONSHIPS:{digest}"


def normalize_scope(session: Session, scope: EvalScope, ref: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the scope reference and return its canonical form. Raises 404/422 for bad input."""
    ref = dict(ref or {})
    if scope is EvalScope.NETWORK:
        return {}
    if scope is EvalScope.ENTITY:
        try:
            kind = EntityKind(str(ref.get("entity_type", "")))
            eid = int(ref.get("entity_id", 0))
        except (ValueError, TypeError):
            raise DomainValidationError("entity scope needs entity_type and entity_id.") from None
        if kind in ENTITY_KINDS:
            if session.get(MODELS[_reg(kind)], eid) is None:
                raise NotFoundError("Record not found.")
        elif kind is EntityKind.RELATIONSHIP:
            if session.get(SupplyRelationship, eid) is None:
                raise NotFoundError("Relationship not found.")
        elif session.get(ProductLink, eid) is None:
            raise NotFoundError("Product link not found.")
        return {"entity_type": kind.value, "entity_id": eid}
    if scope is EvalScope.RELATIONSHIPS:
        refs = ref.get("refs")
        if not isinstance(refs, list) or not refs or len(refs) > MAX_REFS:
            raise DomainValidationError(f"relationship scope needs 1-{MAX_REFS} refs such as SUPPLY:3 or PRODUCT:5.")
        clean = []
        for r in refs:
            try:
                kind, rid = parse_ref(str(r))
            except Exception:
                raise DomainValidationError(f"'{str(r)[:30]}' is not a relationship reference.") from None
            if kind is EdgeKind.OWNER:
                raise DomainValidationError("OWNS is derived from the facility owner; evaluate the facility instead.")
            clean.append(f"{kind.value}:{rid}")
        return {"refs": sorted(set(clean))}
    if scope is EvalScope.BATCH:
        bid = int(ref.get("batch_id", 0) or 0)
        if session.get(IngestionBatch, bid) is None:
            raise NotFoundError("Batch not found.")
        return {"batch_id": bid}
    sid = int(ref.get("source_id", 0) or 0)
    if session.get(DataSource, sid) is None:
        raise NotFoundError("Source not found.")
    return {"source_id": sid}


def _reg(kind: EntityKind):
    from app.models.enums import RegistryType

    return RegistryType(kind.value)


def create_run(
    session: Session,
    scope: EvalScope,
    ref: dict[str, Any] | None,
    requested_by: str,
    trigger: str = "API",
    limits: Limits = DEFAULT_LIMITS,
) -> QualityEvalRun:
    """Insert a QUEUED run. Raises ConflictError when the same scope is already queued or running."""
    canon = normalize_scope(session, scope, ref)
    now = utcnow()
    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    stale = session.scalars(
        select(QualityEvalRun).where(
            QualityEvalRun.status.in_([EvalStatus.QUEUED, EvalStatus.RUNNING]),
            QualityEvalRun.scope_key == _scope_key(scope, canon),
        )
    ).all()
    for old in stale:
        beat = old.heartbeat_at or old.queued_at
        if beat < cutoff:  # the process that owned it died: release the lock and say so
            old.status, old.finished_at, old.error = EvalStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    run = QualityEvalRun(
        run_uid=f"QEV-{uuid.uuid4().hex[:16].upper()}", scope_type=scope, scope_ref=canon, scope_key=_scope_key(scope, canon),
        status=EvalStatus.QUEUED, trigger=trigger, requested_by=requested_by, queued_at=now, checks_evaluated=0,
        findings_created=0, findings_updated=0, findings_resolved=0, findings_reopened=0, findings_unchanged=0,
        subjects_failed=0, subjects_skipped=0, failures=[], truncated=False, truncation_reasons=[],
    )  # fmt: skip
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError("An evaluation of this scope is already queued or running.") from None
    return run


# ----------------------------------------------------------------------------------------------- subject loading


def _source_map(session: Session) -> dict[str, int]:
    return {ns: i for i, ns in session.execute(select(DataSource.id, DataSource.namespace))}


def _entity_subject(kind: EntityKind, e: Any) -> Subject:
    return Subject(kind, e.id, e, e.source_id, e.name)


def _edge_subject(kind: EntityKind, rec: Any, ns: dict[str, int]) -> Subject:
    return Subject(
        kind, rec.row_id, rec, ns.get(rec.source_namespace) if rec.source_namespace else None, rec.code, rec.ref
    )


def load_subjects(
    session: Session, scope: EvalScope, ref: dict[str, Any], limit: int
) -> tuple[list[Subject], list[str], dict[str, int], int]:
    """Return (subjects, truncation_reasons, examined_counts, missing_count). Deterministic order: entities then edges."""
    ns = _source_map(session)
    subjects: list[Subject] = []
    missing = 0
    reasons: list[str] = []

    def entities(kind: EntityKind, ids: list[int] | None = None, source_id: int | None = None) -> None:
        model = MODELS[_reg(kind)]
        stmt = select(model).where(model.merged_into_id.is_(None)).order_by(model.id)
        if ids is not None:
            stmt = stmt.where(model.id.in_(ids))
        if source_id is not None:
            stmt = stmt.where(model.source_id == source_id)
        for e in session.scalars(stmt).unique():
            subjects.append(_entity_subject(kind, e))

    def edges(
        supply_ids: list[int] | None = None, link_ids: list[int] | None = None, namespace: str | None = None
    ) -> None:
        s_stmt = select(SupplyRelationship).order_by(SupplyRelationship.id)
        l_stmt = select(ProductLink).order_by(ProductLink.id)
        if supply_ids is not None:
            s_stmt = s_stmt.where(SupplyRelationship.id.in_(supply_ids))
        if link_ids is not None:
            l_stmt = l_stmt.where(ProductLink.id.in_(link_ids))
        if namespace is not None:
            s_stmt = s_stmt.where(SupplyRelationship.source_namespace == namespace)
            l_stmt = l_stmt.where(ProductLink.source_namespace == namespace)
        if supply_ids is None and link_ids is not None:
            s_stmt = s_stmt.where(False)
        if link_ids is None and supply_ids is not None:
            l_stmt = l_stmt.where(False)
        for r in session.scalars(s_stmt).unique():
            subjects.append(_edge_subject(EntityKind.RELATIONSHIP, supply_rec(r), ns))
        for link in session.scalars(l_stmt).unique():
            subjects.append(_edge_subject(EntityKind.PRODUCT_LINK, link_rec(link), ns))

    if scope is EvalScope.NETWORK:
        for kind in ENTITY_KINDS:
            entities(kind)
        edges()
    elif scope is EvalScope.ENTITY:
        kind, eid = EntityKind(ref["entity_type"]), ref["entity_id"]
        if kind in ENTITY_KINDS:
            entities(kind, [eid])
        elif kind is EntityKind.RELATIONSHIP:
            edges(supply_ids=[eid], link_ids=None)
        else:
            edges(supply_ids=None, link_ids=[eid])
        missing = 0 if subjects else 1
    elif scope is EvalScope.RELATIONSHIPS:
        sup = [int(r.split(":")[1]) for r in ref["refs"] if r.startswith("SUPPLY:")]
        lin = [int(r.split(":")[1]) for r in ref["refs"] if r.startswith("PRODUCT:")]
        edges(supply_ids=sup, link_ids=lin)
        missing = len(ref["refs"]) - len(subjects)
    elif scope is EvalScope.BATCH:
        pairs = session.execute(
            select(RecordLineage.entity_type, RecordLineage.entity_id)
            .where(RecordLineage.batch_id == ref["batch_id"])
            .distinct()
        ).all()
        by: dict[EntityKind, list[int]] = defaultdict(list)
        for k, i in pairs:
            by[k].append(i)
        for kind in ENTITY_KINDS:
            if by.get(kind):
                entities(kind, sorted(by[kind]))
        if by.get(EntityKind.RELATIONSHIP) or by.get(EntityKind.PRODUCT_LINK):
            edges(
                supply_ids=sorted(by.get(EntityKind.RELATIONSHIP, [])) or None,
                link_ids=sorted(by.get(EntityKind.PRODUCT_LINK, [])) or None,
            )
        missing = max(0, len(pairs) - len(subjects))
    else:  # SOURCE
        sid = ref["source_id"]
        for kind in ENTITY_KINDS:
            entities(kind, source_id=sid)
        namespace = session.scalar(select(DataSource.namespace).where(DataSource.id == sid))
        edges(namespace=namespace)
    examined: dict[str, int] = defaultdict(int)
    if len(subjects) > limit:
        reasons.append("subject_limit")
        subjects = subjects[:limit]
    for s in subjects:
        examined[s.kind.value] += 1
    return subjects, reasons, dict(examined), missing


# -------------------------------------------------------------------------------------------------- execution


def execute_run(
    factory: sessionmaker[Session], run_id: int, qcfg: QualityConfig, limits: Limits = DEFAULT_LIMITS
) -> QualityEvalRun:
    session = factory()
    try:
        run = session.get(QualityEvalRun, run_id)
        if run is None:
            raise NotFoundError("Run not found.")
        if run.status is not EvalStatus.QUEUED:
            return run
        try:
            _execute(session, run, qcfg, limits)
        except Exception as exc:  # never leave a run RUNNING
            session.rollback()
            logger.exception("quality run failed", extra={"run": run_id})
            run = session.get(QualityEvalRun, run_id)
            run.status, run.finished_at, run.error = EvalStatus.FAILED, utcnow(), f"unexpected {type(exc).__name__}"
            session.commit()
        return run
    finally:
        session.close()


def _execute(session: Session, run: QualityEvalRun, qcfg: QualityConfig, limits: Limits) -> None:
    t0 = time.monotonic()
    run.status, run.started_at, run.heartbeat_at = EvalStatus.RUNNING, utcnow(), utcnow()
    session.commit()
    enabled = enabled_map(session)
    rules = {rid: spec for rid, spec in catalog().items() if enabled[rid]}
    chash = catalog_hash(enabled, config_hash(qcfg), scoring.FORMULA_VERSION)
    run.catalog_hash, run.formula_version = chash, scoring.FORMULA_VERSION
    run.config_version, run.config_hash = qcfg.version, config_hash(qcfg)
    run.rule_versions = {rid: spec.version for rid, spec in rules.items()}
    subjects, reasons, examined, missing = load_subjects(
        session, run.scope_type, run.scope_ref or {}, limits.max_subjects
    )
    run.examined = examined
    run.subjects_skipped = missing
    now = utcnow()
    ctx = Context(session, qcfg, now.date(), now)
    ctx.load(subjects)
    counters = Counters()
    failures: list[dict[str, Any]] = []
    failed = evaluated = 0
    scores: list[float | None] = []
    for part in chunks(subjects, limits.chunk):
        if time.monotonic() - t0 > limits.time_budget_seconds:
            reasons.append("time_budget")
            run.subjects_skipped += len(subjects) - evaluated - failed
            break
        existing: dict[str, Any] = {}
        for kind in {s.kind for s in part}:
            existing.update(load_existing(session, kind, [s.id for s in part if s.kind is kind]))
        for sub in part:
            results: list[tuple[RuleSpec, list]] = []
            try:
                for spec in rules.values():
                    if sub.kind in spec.applies_to:
                        results.append((spec, spec.evaluate(sub, ctx)))
            except Exception as exc:
                failed += 1
                if len(failures) < MAX_FAILURES:
                    failures.append({"subject": f"{sub.kind.value}:{sub.id}", "error": type(exc).__name__})
                continue
            try:
                with session.begin_nested():
                    outcomes = reconcile_subject(session, sub, results, existing, run.id, run.started_at, counters)
                    res = scoring.score_subject(outcomes)
                    session.add(
                        QualityScore(
                            run_id=run.id, subject_type=sub.kind, subject_id=sub.id, source_id=sub.source_id, state=res.state, score=res.score,
                            checks_total=res.checks_total, checks_failed=res.checks_failed, exceptions_excluded=res.exceptions_excluded,
                            applicable_weight=res.applicable_weight, failed_weight=res.failed_weight, breakdown=res.breakdown,
                            catalog_hash=chash, formula_version=scoring.FORMULA_VERSION, evaluated_at=utcnow(),
                        )
                    )  # fmt: skip
                scores.append(res.score)
                evaluated += 1
            except Exception as exc:
                failed += 1
                if len(failures) < MAX_FAILURES:
                    failures.append({"subject": f"{sub.kind.value}:{sub.id}", "error": type(exc).__name__})
        run.heartbeat_at = utcnow()
        run.findings_created, run.findings_updated = counters.created, counters.updated
        session.commit()
    run.checks_evaluated = counters.checks
    run.findings_created, run.findings_updated = counters.created, counters.updated
    run.findings_resolved, run.findings_reopened, run.findings_unchanged = (
        counters.resolved,
        counters.reopened,
        counters.unchanged,
    )
    run.subjects_failed = failed
    run.failures = failures
    run.truncation_reasons = sorted(set(reasons))
    run.truncated = bool(reasons)
    run.aggregate = scoring.aggregate(scores, evaluated)
    if evaluated == 0 and (failed > 0 or not subjects and missing):
        run.status = EvalStatus.FAILED if failed else EvalStatus.COMPLETED
    elif failed or reasons or missing:
        run.status = EvalStatus.PARTIAL
    else:
        run.status = EvalStatus.COMPLETED
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - t0) * 1000)
    session.commit()


def evaluate_now(
    factory: sessionmaker[Session],
    scope: EvalScope,
    ref: dict[str, Any] | None,
    requested_by: str,
    qcfg: QualityConfig,
    trigger: str = "API",
    limits: Limits = DEFAULT_LIMITS,
) -> QualityEvalRun:
    """Create and execute a run synchronously (used by tests, imports and re-evaluation; the API runs it in the background)."""
    s = factory()
    try:
        run = create_run(s, scope, ref, requested_by, trigger, limits)
        s.commit()
        run_id = run.id
    finally:
        s.close()
    return execute_run(factory, run_id, qcfg, limits)


def latest_scores(session: Session, kind: EntityKind, ids: list[int]) -> dict[int, QualityScore]:
    """Most recent score row per subject (by id), chunked."""
    out: dict[int, QualityScore] = {}
    for part in chunks(ids):
        for sc in session.scalars(
            select(QualityScore)
            .where(QualityScore.subject_type == kind, QualityScore.subject_id.in_(part))
            .order_by(QualityScore.id)
        ):
            out[sc.subject_id] = sc
    return out


__all__ = [
    "Limits",
    "create_run",
    "execute_run",
    "evaluate_now",
    "enabled_map",
    "active_rules",
    "load_subjects",
    "update",
]
