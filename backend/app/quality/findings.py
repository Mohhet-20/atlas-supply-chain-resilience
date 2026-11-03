"""Finding lifecycle: reconcile evaluation results with persisted findings, and manual review transitions.

Evaluation never deletes a finding. A finding that stops failing is RESOLVED automatically; if the same problem is
seen again by a *later* evaluation it is REOPENED. Accepted exceptions are never reopened or auto-resolved.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor, Role
from app.core.errors import (
    DomainValidationError,
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    VersionConflictError,
)
from app.models.enums import EntityKind, FindingOrigin, ResolutionStatus, Severity
from app.models.ingestion import DataQualityFinding
from app.models.quality import QualityFindingEvent
from app.models.types import utcnow
from app.quality.framework import Check, RuleSpec, Subject, finding_key, safe_value
from app.quality.scoring import Outcome
from app.registry import audit

OPEN_LIKE = (
    ResolutionStatus.OPEN,
    ResolutionStatus.ACKNOWLEDGED,
    ResolutionStatus.IN_REMEDIATION,
    ResolutionStatus.REOPENED,
)
EXCEPTION_LIKE = (ResolutionStatus.ACCEPTED_EXCEPTION, ResolutionStatus.WONT_FIX)
SYSTEM_ACTOR = "system:evaluation"


@dataclass
class Counters:
    created: int = 0
    updated: int = 0
    resolved: int = 0
    reopened: int = 0
    unchanged: int = 0
    checks: int = 0
    reopen_suppressed: int = 0


@dataclass
class SubjectResult:
    outcomes: list[Outcome] = field(default_factory=list)


def add_event(
    session: Session, finding: DataQualityFinding, actor: str, action: str, from_status: str | None, to_status: str | None,
    note: str | None = None, details: dict[str, Any] | None = None, run_id: int | None = None,
) -> None:  # fmt: skip
    session.add(
        QualityFindingEvent(
            finding_id=finding.id, occurred_at=utcnow(), actor=actor, action=action, from_status=from_status,
            to_status=to_status, note=(note or None) and note[:400], details=details or {}, run_id=run_id,
        )
    )  # fmt: skip


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _clean_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: safe_value(k, v) if not isinstance(v, list | dict) else v for k, v in ev.items()}


def load_existing(session: Session, kind: EntityKind, ids: list[int]) -> dict[str, DataQualityFinding]:
    out: dict[str, DataQualityFinding] = {}
    for i in range(0, len(ids), 400):
        part = ids[i : i + 400]
        for f in session.scalars(
            select(DataQualityFinding).where(
                DataQualityFinding.origin == FindingOrigin.EVALUATION,
                DataQualityFinding.entity_type == kind,
                DataQualityFinding.entity_id.in_(part),
            )
        ):
            out[f.finding_key] = f
    return out


def reconcile_subject(
    session: Session,
    subject: Subject,
    results: list[tuple[RuleSpec, list[Check]]],
    existing: dict[str, DataQualityFinding],
    run_id: int,
    run_started: datetime,
    counters: Counters,
) -> list[Outcome]:
    """Apply one subject's check results to the finding table. Caller owns the transaction/savepoint."""
    now = utcnow()
    outcomes: list[Outcome] = []
    failed_keys: set[str] = set()
    evaluated_rules = {r.rule_id for r, _ in results}
    mine = {k: f for k, f in existing.items() if f.entity_type == subject.kind and f.entity_id == subject.id}
    for rule, checks in results:
        for chk in checks:
            counters.checks += 1
            key = finding_key(rule.rule_id, subject.kind, subject.id, chk.field)
            row = mine.get(key)
            excepted = row is not None and row.resolution_status in EXCEPTION_LIKE and not chk.passed
            outcomes.append(Outcome(rule, chk, excepted))
            if chk.passed:
                continue
            failed_keys.add(key)
            sev = chk.severity or rule.severity
            msg = _clip(chk.message or rule.name, 400)
            ev = _clean_evidence(chk.evidence)
            if row is None:
                row = DataQualityFinding(
                    finding_key=key, rule_id=rule.rule_id, severity=sev, batch_id=None, source_id=subject.source_id,
                    entity_type=subject.kind, entity_id=subject.id, external_id=_clip(subject.ref or getattr(subject.obj, "external_id", None) or "", 64) or None,
                    field=chk.field[:80] if chk.field else None, message=msg, suggestion=_clip(rule.remediation, 400),
                    resolution_status=ResolutionStatus.OPEN, origin=FindingOrigin.EVALUATION, rule_version=rule.version,
                    dimension=rule.dimension.value, evidence=ev, requires_review=rule.requires_review,
                    first_detected_at=now, last_evaluated_at=now, run_id=run_id, version=1, created_at=now,
                )  # fmt: skip
                session.add(row)
                session.flush()
                add_event(
                    session,
                    row,
                    SYSTEM_ACTOR,
                    "DETECTED",
                    None,
                    ResolutionStatus.OPEN.value,
                    run_id=run_id,
                    details={"rule": rule.rule_id, "version": rule.version},
                )
                existing[key] = mine[key] = row
                counters.created += 1
                continue
            before = (row.severity, row.message, row.rule_version, row.evidence)
            row.last_evaluated_at, row.run_id = now, run_id
            row.severity, row.message, row.rule_version, row.evidence = sev, msg, rule.version, ev
            row.requires_review = rule.requires_review
            if row.resolution_status is ResolutionStatus.RESOLVED:
                if row.resolved_at is not None and run_started > row.resolved_at:
                    old = row.resolution_status
                    row.resolution_status = ResolutionStatus.REOPENED
                    row.resolved_at = None
                    row.reopened_count += 1
                    row.version += 1
                    session.flush()
                    add_event(
                        session,
                        row,
                        SYSTEM_ACTOR,
                        "REOPENED",
                        old.value,
                        ResolutionStatus.REOPENED.value,
                        note="The problem was detected again by a later evaluation.",
                        run_id=run_id,
                    )
                    counters.reopened += 1
                else:
                    counters.reopen_suppressed += 1
                    counters.unchanged += 1
                continue
            if before != (row.severity, row.message, row.rule_version, row.evidence):
                row.version += 1
                counters.updated += 1
            else:
                counters.unchanged += 1
    # findings whose rule ran but no longer fails (or no longer applies) are resolved automatically
    for key, row in list(mine.items()):
        if key in failed_keys or row.rule_id not in evaluated_rules:
            continue
        if row.resolution_status in OPEN_LIKE:
            old = row.resolution_status
            row.resolution_status = ResolutionStatus.RESOLVED
            row.resolved_at, row.resolved_by, row.last_evaluated_at, row.run_id = now, SYSTEM_ACTOR, now, run_id
            row.resolution_note = "No longer detected by evaluation."
            row.version += 1
            session.flush()
            add_event(
                session,
                row,
                SYSTEM_ACTOR,
                "AUTO_RESOLVED",
                old.value,
                ResolutionStatus.RESOLVED.value,
                note="No longer detected by evaluation.",
                run_id=run_id,
            )
            counters.resolved += 1
    return outcomes


def safe_create_or_reconcile(*args: Any, **kwargs: Any) -> list[Outcome]:  # pragma: no cover - thin wrapper
    return reconcile_subject(*args, **kwargs)


# ------------------------------------------------------------------------------------------- manual review

ACTIONS: dict[str, tuple[set[ResolutionStatus], ResolutionStatus, Role, int]] = {
    # action: (allowed from, to, minimum role, minimum note length)
    "acknowledge": ({ResolutionStatus.OPEN, ResolutionStatus.REOPENED}, ResolutionStatus.ACKNOWLEDGED, Role.EDITOR, 0),
    "start_remediation": ({ResolutionStatus.OPEN, ResolutionStatus.REOPENED, ResolutionStatus.ACKNOWLEDGED}, ResolutionStatus.IN_REMEDIATION, Role.EDITOR, 0),
    "resolve": (set(OPEN_LIKE), ResolutionStatus.RESOLVED, Role.REVIEWER, 3),
    "accept_exception": (set(OPEN_LIKE), ResolutionStatus.ACCEPTED_EXCEPTION, Role.REVIEWER, 10),
    "reopen": ({ResolutionStatus.RESOLVED, ResolutionStatus.ACCEPTED_EXCEPTION, ResolutionStatus.WONT_FIX}, ResolutionStatus.REOPENED, Role.REVIEWER, 3),
}  # fmt: skip


def get_finding(session: Session, finding_id: int) -> DataQualityFinding:
    f = session.get(DataQualityFinding, finding_id)
    if f is None:
        raise NotFoundError("Finding not found.")
    return f


def transition(
    session: Session,
    actor: Actor,
    finding_id: int,
    action: str,
    note: str | None,
    expected_version: int,
    request_id: str | None = None,
) -> DataQualityFinding:
    if action not in ACTIONS:
        raise DomainValidationError("Unknown finding action.")
    allowed, to, role, min_note = ACTIONS[action]
    if actor.role < role:
        raise ForbiddenError(f"'{action}' requires the {role.name.lower()} role.")
    f = get_finding(session, finding_id)
    if f.version != expected_version:
        raise VersionConflictError(
            f"The finding changed (current version {f.version}, you sent {expected_version}). Reload and retry."
        )
    note = (note or "").strip()
    if len(note) < min_note:
        raise DomainValidationError(f"A note of at least {min_note} characters is required for '{action}'.")
    if f.resolution_status not in allowed:
        raise InvalidTransitionError(f"A {f.resolution_status.value} finding cannot be changed with '{action}'.")
    old = f.resolution_status
    f.resolution_status = to
    f.resolution_note = note[:400] or f.resolution_note
    if to in (ResolutionStatus.RESOLVED, ResolutionStatus.ACCEPTED_EXCEPTION):
        f.resolved_at, f.resolved_by = utcnow(), actor.name
    if to is ResolutionStatus.REOPENED:
        f.resolved_at, f.resolved_by = None, None
        f.reopened_count += 1
    f.version += 1
    session.flush()
    add_event(session, f, actor.name, action.upper(), old.value, to.value, note=note)
    audit.record(
        session,
        actor,
        f"quality.finding.{action}",
        target_type="FINDING",
        target_id=f.id,
        details={"from": old.value, "to": to.value, "rule": f.rule_id},
        request_id=request_id,
    )
    return f


def assign(
    session: Session,
    actor: Actor,
    finding_id: int,
    assignee: str | None,
    expected_version: int,
    request_id: str | None = None,
) -> DataQualityFinding:
    if actor.role < Role.REVIEWER:
        raise ForbiddenError("Assigning a finding requires the reviewer role.")
    f = get_finding(session, finding_id)
    if f.version != expected_version:
        raise VersionConflictError(
            f"The finding changed (current version {f.version}, you sent {expected_version}). Reload and retry."
        )
    old = f.assigned_to
    f.assigned_to = assignee
    f.version += 1
    session.flush()
    add_event(
        session,
        f,
        actor.name,
        "ASSIGNED",
        f.resolution_status.value,
        f.resolution_status.value,
        details={"from": old, "to": assignee},
    )
    audit.record(
        session,
        actor,
        "quality.finding.assign",
        target_type="FINDING",
        target_id=f.id,
        details={"assignee": assignee},
        request_id=request_id,
    )
    return f


def events_of(session: Session, finding_id: int) -> list[QualityFindingEvent]:
    return list(
        session.scalars(
            select(QualityFindingEvent)
            .where(QualityFindingEvent.finding_id == finding_id)
            .order_by(QualityFindingEvent.id)
        )
    )


def counts_by(session: Session, column, where=()) -> dict[Any, int]:  # small helper used by analytics
    from sqlalchemy import func

    rows = session.execute(select(column, func.count()).where(*where).group_by(column)).all()
    out: dict[Any, int] = defaultdict(int)
    for k, n in rows:
        out[k] = n
    return dict(out)


__all__ = [
    "Counters",
    "reconcile_subject",
    "transition",
    "assign",
    "get_finding",
    "load_existing",
    "events_of",
    "Severity",
]
