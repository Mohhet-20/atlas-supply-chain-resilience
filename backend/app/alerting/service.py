"""Alert lifecycle: rules, event ingestion, evaluation, de-duplication, prioritisation, transitions, assignment, suppression, escalation.

Everything here works on rows in the caller's transaction. Nothing runs on a hidden schedule: evaluation and escalation happen when an authorised
caller (or the optional timer) invokes them, and every deadline lives in the database so a restart loses nothing. Execution is at-least-once with
idempotent effects (unique keys), never claimed exactly-once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerting import detectors as D
from app.alerting import notify as N
from app.alerting.config import (
    DETECTORS,
    ENTITY_TYPES,
    EVENT_SCHEMA_VERSION,
    EVENT_TYPES,
    LIMITS,
    SENSITIVE,
    SEV_RANK,
    SUPPORTED_SCHEMA_VERSIONS,
    TRANSITIONS,
    PolicyBody,
    RuleBody,
    validate_params,
)
from app.core.errors import (
    ConflictError,
    DomainValidationError,
    InvalidTransitionError,
    NotFoundError,
    VersionConflictError,
)
from app.db.tenancy import note_probe
from app.models.alerting import (
    ACTIVE_STATES,
    Alert,
    AlertEscalation,
    AlertEscalationPolicy,
    AlertEvent,
    AlertLink,
    AlertNote,
    AlertOccurrence,
    AlertRule,
    AlertRuleRun,
    AlertRuleVersion,
    AlertSuppression,
    AlertTransition,
    InboxNotification,
    NotificationDelivery,
)

log = logging.getLogger("atlas.alerting")
AUTO_RESOLVABLE = ("supplier_risk_score", "stockout_status", "source_stale", "source_ingest_failures")
ROLE_NAMES = ("viewer", "editor", "reviewer", "admin")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(d: datetime | None) -> datetime | None:
    return None if d is None else (d if d.tzinfo else d.replace(tzinfo=timezone.utc))


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def redact(obj: Any, depth: int = 0) -> Any:
    """Drop credential-looking keys and bound strings, lists and depth before anything is stored in evidence or logs."""
    if depth > 4:
        return "[truncated]"
    if isinstance(obj, dict):
        return {
            str(k)[:64]: ("[redacted]" if any(s in str(k).lower() for s in SENSITIVE) else redact(v, depth + 1))
            for k, v in list(obj.items())[:50]
        }
    if isinstance(obj, list | tuple):
        return [redact(v, depth + 1) for v in list(obj)[:50]]
    if isinstance(obj, str):
        return obj[:500]
    return obj


@dataclass
class Ctx:
    """What the service needs from the running application (kept out of the service so tests can supply fakes)."""

    directory: dict[str, int]  # actor name -> Role value
    provider: N.Provider
    webhooks: dict[str, str] = field(default_factory=dict)
    allowed_hosts: set[str] = field(default_factory=set)
    max_attempts: int = 4
    retry_base: int = 60
    timeout: float = 5.0
    retention_days: int = 90
    org_id: int = 1


# ---------- rules ----------


def rule_definition(rule: AlertRule, policy_uid: str | None) -> dict[str, Any]:
    return {
        "name": rule.name, "description": rule.description, "detector": rule.detector, "params": rule.params, "severity": rule.severity, "enabled": rule.enabled,
        "dedup_window_minutes": rule.dedup_window_minutes, "cooldown_minutes": rule.cooldown_minutes, "auto_resolve": rule.auto_resolve, "notify": rule.notify,
        "default_assignee": rule.default_assignee, "escalation_policy": policy_uid,
        "effective_from": aware(rule.effective_from).isoformat() if rule.effective_from else None,
        "effective_to": aware(rule.effective_to).isoformat() if rule.effective_to else None,
    }  # fmt: skip


def _valid_target(ctx: Ctx, name: str, *, assignee: bool) -> bool:
    if name.startswith("team:"):
        return name[5:] in ROLE_NAMES and name[5:] != "viewer"
    return name in ctx.directory and (not assignee or ctx.directory[name] >= 2)


def _parse_dt(v: str | None, field_name: str) -> datetime | None:
    if v is None:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise DomainValidationError(f"{field_name}: not an ISO 8601 timestamp") from None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def check_rule(
    db: Session, body: RuleBody, ctx: Ctx, *, rule: AlertRule | None = None
) -> tuple[dict[str, Any], AlertEscalationPolicy | None, datetime | None, datetime | None]:
    try:
        params = validate_params(body.detector, body.params)
    except ValueError as e:
        raise DomainValidationError(str(e)) from None
    errs: list[str] = []
    if body.auto_resolve and body.detector not in AUTO_RESOLVABLE:
        errs.append(
            f"auto_resolve: not supported for {body.detector}, because the absence of a match does not show that the condition cleared"
        )
    if body.default_assignee and not _valid_target(ctx, body.default_assignee, assignee=True):
        errs.append(
            "default_assignee: must be a configured user with the editor role or higher, or team:editor|reviewer|admin"
        )
    for r in body.notify.recipients:
        if not _valid_target(ctx, r, assignee=False):
            errs.append(f"notify.recipients: '{r}' is not a configured user or team")
    if "webhook" in body.notify.channels and (not body.notify.webhook or body.notify.webhook not in ctx.webhooks):
        errs.append(
            "notify.webhook: the webhook channel is unavailable or the named destination is not configured (ATLAS_ALERTING_WEBHOOKS and the host allow-list)"
        )
    pol = None
    if body.escalation_policy:
        pol = db.scalar(select(AlertEscalationPolicy).where(AlertEscalationPolicy.policy_uid == body.escalation_policy))
        if pol is None or not pol.enabled:
            errs.append("escalation_policy: no such enabled policy")
    f, t = _parse_dt(body.effective_from, "effective_from"), _parse_dt(body.effective_to, "effective_to")
    if f and t and t <= f:
        errs.append("effective_to: must be after effective_from")
    dup = db.scalar(select(AlertRule.id).where(AlertRule.name == body.name, AlertRule.id != (rule.id if rule else -1)))
    if dup:
        raise ConflictError(f"A rule named '{body.name}' already exists.")
    if errs:
        raise DomainValidationError("; ".join(errs))
    return params, pol, f, t


def _apply(
    rule: AlertRule,
    body: RuleBody,
    params: dict[str, Any],
    pol: AlertEscalationPolicy | None,
    f: datetime | None,
    t: datetime | None,
) -> None:
    rule.name, rule.description, rule.detector, rule.params, rule.severity, rule.enabled = (
        body.name,
        body.description,
        body.detector,
        params,
        body.severity,
        body.enabled,
    )
    rule.dedup_window_minutes, rule.cooldown_minutes, rule.auto_resolve = (
        body.dedup_window_minutes,
        body.cooldown_minutes,
        body.auto_resolve,
    )
    rule.notify, rule.default_assignee, rule.escalation_policy_id, rule.effective_from, rule.effective_to = (
        body.notify.model_dump(),
        body.default_assignee,
        pol.id if pol else None,
        f,
        t,
    )


def _snapshot(db: Session, rule: AlertRule, author: str) -> None:
    pol = db.get(AlertEscalationPolicy, rule.escalation_policy_id) if rule.escalation_policy_id else None
    db.add(
        AlertRuleVersion(
            rule_id=rule.id,
            version=rule.version,
            definition=rule_definition(rule, pol.policy_uid if pol else None),
            author=author,
        )
    )


def create_rule(db: Session, actor: str, body: RuleBody, ctx: Ctx) -> AlertRule:
    params, pol, f, t = check_rule(db, body, ctx)
    rule = AlertRule(rule_uid=uid("AR"), created_by=actor, version=1, description="", notify={})
    _apply(rule, body, params, pol, f, t)
    db.add(rule)
    db.flush()
    _snapshot(db, rule, actor)
    return rule


def update_rule(db: Session, actor: str, rule: AlertRule, body: RuleBody, expected_version: int, ctx: Ctx) -> AlertRule:
    if rule.version != expected_version:
        raise VersionConflictError(f"The rule changed (now version {rule.version}); reload before editing.")
    params, pol, f, t = check_rule(db, body, ctx, rule=rule)
    _apply(rule, body, params, pol, f, t)
    rule.version += 1
    db.flush()
    _snapshot(db, rule, actor)
    return rule


def _effective(rule: AlertRule, now: datetime) -> bool:
    f, t = aware(rule.effective_from), aware(rule.effective_to)
    return rule.enabled and (f is None or f <= now) and (t is None or now < t)


# ---------- policies ----------


def create_policy(db: Session, actor: str, body: PolicyBody, ctx: Ctx) -> AlertEscalationPolicy:
    for tg in body.targets:
        if not _valid_target(ctx, tg, assignee=True):
            raise DomainValidationError(
                f"targets: '{tg}' is not a configured user (editor or higher) or team:editor|reviewer|admin"
            )
    if db.scalar(select(AlertEscalationPolicy.id).where(AlertEscalationPolicy.name == body.name)):
        raise ConflictError(f"A policy named '{body.name}' already exists.")
    if body.resolve_minutes is not None and body.resolve_minutes < body.ack_minutes:
        raise DomainValidationError("resolve_minutes: must not be shorter than ack_minutes")
    p = AlertEscalationPolicy(
        policy_uid=uid("EP"),
        name=body.name,
        enabled=body.enabled,
        ack_minutes=body.ack_minutes,
        resolve_minutes=body.resolve_minutes,
        escalate_after_minutes=body.escalate_after_minutes,
        targets=body.targets,
        created_by=actor,
    )
    db.add(p)
    db.flush()
    return p


def update_policy(
    db: Session, policy: AlertEscalationPolicy, body: PolicyBody, expected_version: int, ctx: Ctx
) -> AlertEscalationPolicy:
    if policy.version != expected_version:
        raise VersionConflictError(f"The policy changed (now version {policy.version}); reload before editing.")
    for tg in body.targets:
        if not _valid_target(ctx, tg, assignee=True):
            raise DomainValidationError(
                f"targets: '{tg}' is not a configured user (editor or higher) or team:editor|reviewer|admin"
            )
    if body.resolve_minutes is not None and body.resolve_minutes < body.ack_minutes:
        raise DomainValidationError("resolve_minutes: must not be shorter than ack_minutes")
    other = db.scalar(
        select(AlertEscalationPolicy.id).where(
            AlertEscalationPolicy.name == body.name, AlertEscalationPolicy.id != policy.id
        )
    )
    if other:
        raise ConflictError(f"A policy named '{body.name}' already exists.")
    policy.name, policy.enabled, policy.ack_minutes, policy.resolve_minutes = (
        body.name,
        body.enabled,
        body.ack_minutes,
        body.resolve_minutes,
    )
    policy.escalate_after_minutes, policy.targets = body.escalate_after_minutes, body.targets
    policy.version += 1  # alerts already raised keep the deadlines they were given
    return policy


# ---------- events ----------


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: Literal[
        "supplier", "facility", "product", "lane", "delivery", "source", "scenario", "run", "product_facility"
    ]
    entity_id: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")]


class EventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str
    source: Annotated[str, StringConstraints(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
    source_record_id: Annotated[str | None, StringConstraints(max_length=128)] = None
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    schema_version: str = EVENT_SCHEMA_VERSION
    occurred_at: datetime
    provenance: Literal["USER_PROVIDED", "IMPORTED_EXTERNAL", "SYNTHETIC_DEMO"] = "IMPORTED_EXTERNAL"
    correlation_id: Annotated[str | None, StringConstraints(max_length=64)] = None
    causation_id: Annotated[str | None, StringConstraints(max_length=64)] = None
    entities: list[EntityRef] = Field(default_factory=list, max_length=50)
    payload: dict[str, Any] = Field(default_factory=dict)


def _kind_ok(v: Any, k: str) -> bool:
    if k == "str":
        return isinstance(v, str) and 0 < len(v) <= 200
    return isinstance(v, int | float) and not isinstance(v, bool)


def validate_event(body: EventBody, now: datetime) -> tuple[dict[str, Any], list[str]]:
    spec = EVENT_TYPES.get(body.event_type)
    if spec is None:
        raise DomainValidationError(f"event_type: unknown; supported types are {', '.join(EVENT_TYPES)}")
    if body.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DomainValidationError(
            f"schema_version: '{body.schema_version}' is not supported (supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    raw = json.dumps(body.payload, default=str)
    if len(raw.encode()) > LIMITS["payload_bytes"]:
        raise DomainValidationError(f"payload: larger than {LIMITS['payload_bytes']} bytes")
    errs: list[str] = []
    allowed = {**spec["required"], **spec["optional"]}
    for k in body.payload:
        if k not in allowed:
            errs.append(f"payload.{k}: not part of {body.event_type} {EVENT_SCHEMA_VERSION}")
    for k in spec["required"]:
        if k not in body.payload:
            errs.append(f"payload.{k}: required")
    for k, v in body.payload.items():
        if k in allowed and not _kind_ok(v, allowed[k]):
            errs.append(f"payload.{k}: must be {'text up to 200 characters' if allowed[k] == 'str' else 'a number'}")
    occ = aware(body.occurred_at) or now
    if occ > now + timedelta(minutes=5):
        errs.append("occurred_at: is in the future")
    if errs:
        raise DomainValidationError("; ".join(errs))
    warns = (
        []
        if occ >= now - timedelta(days=30)
        else ["occurred_at is more than 30 days before ingestion; it was accepted as given"]
    )
    return {k: v for k, v in body.payload.items()}, warns


def ingest_event(
    db: Session, actor: str, body: EventBody, ctx: Ctx, now: datetime | None = None
) -> tuple[AlertEvent, bool, dict[str, Any]]:
    """Validate, store (idempotently) and process one event. Returns (event, was_duplicate, processing summary)."""
    now = now or utcnow()
    payload, warns = validate_event(body, now)
    phash = hashlib.sha256(
        json.dumps(
            {
                "t": body.event_type,
                "o": aware(body.occurred_at).isoformat(),
                "p": payload,
                "e": [e.model_dump() for e in body.entities],
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()  # type: ignore[union-attr]
    prior = db.scalar(
        select(AlertEvent).where(AlertEvent.source == body.source, AlertEvent.idempotency_key == body.idempotency_key)
    )
    if prior is not None:
        if prior.payload_hash != phash:
            raise ConflictError(
                "This idempotency key was already used with different content; use a new key for a different event."
            )
        prior.duplicate_count += 1  # safe re-delivery: no second event, no second alert
        return prior, True, prior.result or {}
    ev = AlertEvent(
        event_uid=uid("EV"), event_type=body.event_type, source=body.source, source_record_id=body.source_record_id, idempotency_key=body.idempotency_key, payload_hash=phash,
        schema_version=body.schema_version, occurred_at=aware(body.occurred_at), ingested_at=now, provenance=body.provenance, correlation_id=body.correlation_id, causation_id=body.causation_id,
        entities=[e.model_dump() for e in body.entities], payload=redact(payload), warnings=warns or None, ingested_by=actor,
    )  # fmt: skip
    db.add(ev)
    db.flush()
    rules = db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True)).order_by(AlertRule.id)).all()
    summary: dict[str, Any] = {"rules": [], "alerts_created": 0, "alerts_updated": 0}
    for rule in rules:
        if DETECTORS.get(rule.detector, {}).get("mode") != "event" or not _effective(rule, now):
            continue
        res = D.EVENT[rule.detector](ev, rule.params)
        counts = process_matches(db, rule, res, now, ctx, trigger="event", actor=actor)
        summary["alerts_created"] += counts["created"]
        summary["alerts_updated"] += counts["updated"]
        summary["rules"].append(
            {"rule": rule.rule_uid, "outcome": res.outcome, "matched": len(res.matches), "notes": res.notes[:3]}
        )
    ev.status, ev.result = "PROCESSED", summary
    dispatch_due(db, ctx, now)
    return ev, False, summary


# ---------- prioritisation ----------

_BASE = {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 4, "INFO": 4}


def prioritise(
    severity: str, basis: str, related: int, days_to_threshold: float | None, confidence: str | None
) -> tuple[int, list[dict[str, Any]]]:
    """Transparent priority P1 (act first) to P4. Severity sets the base; each documented factor moves it exactly one step; there are no weights."""
    p = _BASE[severity]
    steps: list[dict[str, Any]] = [{"factor": "severity", "effect": f"{severity} starts at P{p}"}]
    up = 0
    if basis == "observed" and severity in ("HIGH", "MEDIUM"):
        up += 1
        steps.append({"factor": "observed_condition", "effect": "+1 urgency: the condition is observed, not predicted"})
    if related > 0:
        up += 1
        steps.append(
            {
                "factor": "related_unresolved_alerts",
                "effect": f"+1 urgency: {related} other unresolved alert(s) on the same entity",
            }
        )
    if days_to_threshold is not None and days_to_threshold <= 7:
        up += 1
        steps.append(
            {
                "factor": "time_to_threshold",
                "effect": f"+1 urgency: the threshold is reached within {days_to_threshold:g} day(s)",
            }
        )
    up = min(up, 2)  # at most two steps up, so a LOW alert can never outrank a CRITICAL one
    p = max(1, p - up)
    if confidence == "LOW" and basis in ("predicted", "assessed") and severity != "CRITICAL":
        p = min(4, p + 1)
        steps.append(
            {"factor": "low_confidence_signal", "effect": "-1 urgency: the underlying signal has low confidence"}
        )
    if basis == "hypothetical":
        p = max(p, 3)
        steps.append({"factor": "hypothetical_scenario", "effect": "capped at P3: a what-if analysis is not an event"})
    steps.append({"factor": "result", "effect": f"priority P{p}"})
    return p, steps


# ---------- matching to alerts ----------


def _dedup_key(rule: AlertRule, m: D.Match) -> str:
    return hashlib.sha256(
        f"{rule.rule_uid}|{rule.detector}|{m.entity_type}|{m.entity_id}|{m.dims}".encode()
    ).hexdigest()[:40]


def _corr_key(m: D.Match) -> str:
    return hashlib.sha256(f"{m.entity_type}|{m.entity_id}".encode()).hexdigest()[:40]


def _policy_of(db: Session, rule: AlertRule) -> AlertEscalationPolicy | None:
    return db.get(AlertEscalationPolicy, rule.escalation_policy_id) if rule.escalation_policy_id else None


def _active_suppression(db: Session, rule: AlertRule, m: D.Match, now: datetime) -> AlertSuppression | None:
    q = select(AlertSuppression).where(
        AlertSuppression.revoked_at.is_(None), AlertSuppression.starts_at <= now, AlertSuppression.expires_at > now
    )
    for s in db.scalars(q.order_by(AlertSuppression.id)):
        if (s.rule_id is None or s.rule_id == rule.id) and (
            s.entity_id is None or (s.entity_type == m.entity_type and s.entity_id == m.entity_id)
        ):
            return s
    return None


def _notify_targets(rule: AlertRule, alert: Alert) -> list[str]:
    t = list((rule.notify or {}).get("recipients", []))
    if alert.assignee and alert.assignee not in t:
        t.append(alert.assignee)
    return t


def _record_transition(
    db: Session, alert: Alert, to: str, actor: str, reason: str | None, mode: str, now: datetime
) -> None:
    db.add(
        AlertTransition(
            alert_id=alert.id,
            from_state=alert.status if alert.id else None,
            to_state=to,
            actor=actor,
            mode=mode,
            reason=(reason or None) and reason[:600],
            at=now,
        )
    )


def _add_occurrence(db: Session, alert: Alert, m: D.Match, now: datetime) -> None:
    db.add(
        AlertOccurrence(
            alert_id=alert.id,
            event_id=m.event_id,
            observed_at=m.observed_at or now,
            value=m.value,
            detail=redact(
                {
                    "summary": m.summary,
                    **{
                        k: v
                        for k, v in m.evidence.items()
                        if k in ("assessment_uid", "analysis_uid", "event_uid", "run")
                    },
                }
            ),
        )
    )


def _condition(m: D.Match) -> dict[str, Any]:
    return {"signal": m.signal, "operator": m.operator, "threshold": m.threshold, "value": m.value, "unit": m.unit}


def process_matches(
    db: Session, rule: AlertRule, res: D.Result, now: datetime, ctx: Ctx, *, trigger: str, actor: str
) -> dict[str, int]:
    t0 = time.perf_counter()
    c = {"created": 0, "updated": 0, "suppressed": 0, "reopened": 0, "throttled": 0}
    pol = _policy_of(db, rule)
    matched_keys: set[str] = set()
    for m in res.matches:
        key = _dedup_key(rule, m)
        matched_keys.add(key)
        active = db.scalar(select(Alert).where(Alert.dedup_key == key, Alert.status.in_(ACTIVE_STATES)))
        if active is not None:
            _repeat(db, active, rule, m, now, ctx, c)
            continue
        window = timedelta(minutes=rule.dedup_window_minutes)
        resolved = db.scalar(
            select(Alert)
            .where(Alert.dedup_key == key, Alert.status == "resolved", Alert.resolved_at >= now - window)
            .order_by(Alert.id.desc())
        )
        if resolved is not None:
            _reopen_on_recurrence(db, resolved, rule, pol, m, now, ctx, c)
            continue
        try:
            with db.begin_nested():
                _create(db, rule, pol, m, key, now, ctx, c)
        except IntegrityError:  # a concurrent evaluation created the same live alert first: treat as a repeat
            existing = db.scalar(select(Alert).where(Alert.dedup_key == key, Alert.status.in_(ACTIVE_STATES)))
            if existing is not None:
                _repeat(db, existing, rule, m, now, ctx, c)
    resolved_n = (
        _auto_resolve(db, rule, res, matched_keys, now) if rule.auto_resolve and rule.detector in AUTO_RESOLVABLE else 0
    )
    outcome = "matched" if res.matches else res.outcome
    db.add(AlertRuleRun(rule_id=rule.id, rule_version=rule.version, started_at=now, trigger=trigger, outcome=outcome, evaluated=res.evaluated, matched=len(res.matches), created=c["created"], updated=c["updated"] + c["reopened"], suppressed=c["suppressed"], truncated=res.truncated,
                        detail={"no_data": res.no_data, "skipped": res.skipped, "notes": res.notes[:5], "auto_resolved": resolved_n, "throttled_notifications": c["throttled"], "actor": actor}, duration_ms=int((time.perf_counter() - t0) * 1000)))  # fmt: skip
    return c


def _create(
    db: Session,
    rule: AlertRule,
    pol: AlertEscalationPolicy | None,
    m: D.Match,
    key: str,
    now: datetime,
    ctx: Ctx,
    c: dict[str, int],
) -> Alert:
    sev = "CRITICAL" if (m.critical and SEV_RANK[rule.severity] < SEV_RANK["CRITICAL"]) else rule.severity
    corr = _corr_key(m)
    related = db.scalars(
        select(Alert).where(Alert.correlation_key == corr, Alert.status.in_(ACTIVE_STATES)).limit(20)
    ).all()
    prio, steps = prioritise(sev, m.basis, len(related), m.days_to_threshold, m.confidence)
    why = [
        f"Severity {sev} comes from rule '{rule.name}' v{rule.version}"
        + (
            f"; raised from {rule.severity} because the value passed the rule's critical value"
            if sev != rule.severity
            else ""
        )
    ]
    sup = _active_suppression(db, rule, m, now)
    status = "open"
    suppression_id = None
    if sup is not None:
        if sev == "CRITICAL":
            why.append(
                f"A suppression (#{sup.id}) matched but is not applied to CRITICAL alerts: they are never silently hidden"
            )
        else:
            status, suppression_id = "suppressed", sup.id
            sup.matched_count += 1
            c["suppressed"] += 1
    assignee = rule.default_assignee
    alert = Alert(
        alert_uid=uid("AL"), rule_id=rule.id, rule_version=rule.version, last_rule_version=rule.version, detector=rule.detector, source=m.extra_source or DETECTORS[rule.detector]["source"], dedup_key=key, correlation_key=corr,
        title=m.title[:200], summary=m.summary[:600], basis=m.basis, severity=sev, priority=prio, explanation={"severity": why, "priority": steps, "engine_version": "al-1"}, entity_type=m.entity_type, entity_id=m.entity_id,
        entity_label=m.entity_label[:200], condition=_condition(m), evidence=redact(m.evidence), link=m.link, status=status, assignee=assignee, escalation_policy_id=pol.id if pol else None,
        due_ack_at=now + timedelta(minutes=pol.ack_minutes) if pol else None, due_resolve_at=now + timedelta(minutes=pol.resolve_minutes) if pol and pol.resolve_minutes else None,
        next_escalation_at=now + timedelta(minutes=pol.ack_minutes) if pol and status == "open" else None, first_seen_at=now, last_seen_at=now, suppression_id=suppression_id,
    )  # fmt: skip
    db.add(alert)
    db.flush()
    db.add(
        AlertTransition(
            alert_id=alert.id,
            from_state=None,
            to_state=status,
            actor="system",
            mode="system",
            reason="created by rule evaluation"
            + (f"; suppressed by suppression #{sup.id}: {sup.reason}" if status == "suppressed" and sup else ""),
            at=now,
        )
    )
    _add_occurrence(db, alert, m, now)
    for o in related:
        if o.dedup_key != key:
            db.add(
                AlertLink(
                    alert_id=alert.id,
                    related_alert_id=o.id,
                    reason="same entity, different condition (correlated, not merged)",
                    created_by="system",
                )
            )
    c["created"] += 1
    if status == "open":
        _notify(db, alert, rule, "created", "created", ctx, now)
        alert.last_notified_at = now
        from app.integrations import events as integration_events  # local: avoids an import cycle

        integration_events.emit(  # transactional outbox: queued in this transaction, delivered by the worker
            db, alert.organization_id, "alert.created",
            {"alert_uid": alert.alert_uid, "title": alert.title, "severity": alert.severity, "priority": alert.priority, "entity_type": alert.entity_type, "entity_id": alert.entity_id},
            now=now,
        )  # fmt: skip
    return alert


def _repeat(db: Session, alert: Alert, rule: AlertRule, m: D.Match, now: datetime, ctx: Ctx, c: dict[str, int]) -> None:
    alert.occurrence_count += 1
    alert.last_seen_at = now
    alert.last_rule_version = rule.version
    alert.condition, alert.evidence, alert.summary = _condition(m), redact(m.evidence), m.summary[:600]
    _add_occurrence(db, alert, m, now)
    c["updated"] += 1
    cool = timedelta(minutes=rule.cooldown_minutes)
    if alert.status == "open":
        if alert.last_notified_at is None or now - aware(alert.last_notified_at) >= cool:
            _notify(db, alert, rule, "repeat", f"repeat:{alert.occurrence_count}", ctx, now)
            alert.last_notified_at = now
        else:
            alert.notifications_throttled += 1
            c["throttled"] += 1
    # acknowledged / investigating / escalated / suppressed alerts count the repeat but never re-notify


def _reopen_on_recurrence(
    db: Session,
    alert: Alert,
    rule: AlertRule,
    pol: AlertEscalationPolicy | None,
    m: D.Match,
    now: datetime,
    ctx: Ctx,
    c: dict[str, int],
) -> None:
    _record_transition(
        db, alert, "open", "system", "reopened: the condition recurred within the de-duplication window", "system", now
    )
    alert.status, alert.reopened_count = "open", alert.reopened_count + 1
    alert.resolved_at = alert.resolved_by = alert.resolution_mode = None
    alert.last_seen_at, alert.occurrence_count = now, alert.occurrence_count + 1
    alert.last_rule_version, alert.condition, alert.evidence = rule.version, _condition(m), redact(m.evidence)
    if pol:
        alert.due_ack_at = now + timedelta(minutes=pol.ack_minutes)
        alert.next_escalation_at = alert.due_ack_at
        alert.escalation_level = 0
    _add_occurrence(db, alert, m, now)
    c["reopened"] += 1
    _notify(db, alert, rule, "repeat", f"reopen:{alert.reopened_count}", ctx, now)
    alert.last_notified_at = now


def _auto_resolve(db: Session, rule: AlertRule, res: D.Result, matched: set[str], now: datetime) -> int:
    """Resolve live alerts whose subject was evaluated this time and no longer matches. Subjects without data are left alone."""
    n = 0
    if res.truncated:
        return 0
    for a in db.scalars(
        select(Alert).where(
            Alert.rule_id == rule.id, Alert.status.in_(("open", "acknowledged", "investigating", "escalated"))
        )
    ):
        if a.dedup_key in matched:
            continue
        if not res.complete and (a.entity_type, a.entity_id) not in res.entities:
            continue
        _record_transition(
            db,
            a,
            "resolved",
            "system",
            "condition no longer met in the latest evaluation (inferred automatically)",
            "system",
            now,
        )
        a.status, a.resolved_at, a.resolved_by, a.resolution_mode, a.resolution_note, a.next_escalation_at = (
            "resolved",
            now,
            "system",
            "auto",
            "Condition no longer met in the latest evaluation.",
            None,
        )
        n += 1
    return n


# ---------- evaluation ----------


def evaluate(
    db: Session, actor: str, ctx: Ctx, now: datetime | None = None, rule_uids: list[str] | None = None
) -> dict[str, Any]:
    """Run every enabled pull rule once (bounded). A failing rule is recorded and does not stop the others."""
    now = now or utcnow()
    q = select(AlertRule).where(AlertRule.enabled.is_(True)).order_by(AlertRule.id)
    if rule_uids:
        q = q.where(AlertRule.rule_uid.in_(rule_uids))
    out: dict[str, Any] = {"rules": [], "created": 0, "updated": 0, "reopened": 0, "suppressed": 0, "skipped_rules": 0}
    for rule in db.scalars(q.limit(LIMITS["rules_per_evaluation"])).all():
        spec = DETECTORS.get(rule.detector)
        if spec is None or spec["mode"] != "pull" or not _effective(rule, now):
            out["skipped_rules"] += 1
            continue
        try:
            with db.begin_nested():
                res = D.PULL[rule.detector](db, rule.params, now)
                c = process_matches(db, rule, res, now, ctx, trigger="evaluate", actor=actor)
            for k in ("created", "updated", "reopened", "suppressed"):
                out[k] += c[k]
            out["rules"].append(
                {
                    "rule": rule.rule_uid,
                    "name": rule.name,
                    "outcome": res.outcome,
                    "evaluated": res.evaluated,
                    "matched": len(res.matches),
                    "no_data": res.no_data,
                    "truncated": res.truncated,
                    "notes": res.notes[:3],
                }
            )
        except Exception as e:  # noqa: BLE001  (isolate one rule's failure; never log payloads)
            log.warning("alert rule evaluation failed", extra={"rule": rule.rule_uid, "error": type(e).__name__})
            db.add(
                AlertRuleRun(
                    rule_id=rule.id,
                    rule_version=rule.version,
                    started_at=now,
                    trigger="evaluate",
                    outcome="error",
                    detail={"error": type(e).__name__, "actor": actor},
                )
            )
            out["rules"].append(
                {"rule": rule.rule_uid, "name": rule.name, "outcome": "error", "error": type(e).__name__}
            )
    dispatch_due(db, ctx, now)
    return out


# ---------- notifications ----------


def _notify(
    db: Session,
    alert: Alert,
    rule: AlertRule | None,
    kind: str,
    bucket: str,
    ctx: Ctx,
    now: datetime,
    targets: list[str] | None = None,
) -> None:
    notify = (rule.notify if rule else None) or {"channels": ["in_app"], "min_severity": "LOW"}
    tg = (
        targets
        if targets is not None
        else (_notify_targets(rule, alert) if rule else ([alert.assignee] if alert.assignee else []))
    )
    N.plan(db, alert, kind, bucket, notify, tg, ctx.directory, now, ctx.max_attempts, ctx.webhooks)


def dispatch_due(db: Session, ctx: Ctx, now: datetime, limit: int = 100) -> dict[str, int]:
    counts = {"attempted": 0, "delivered": 0, "accepted": 0, "failed": 0, "retry_scheduled": 0}
    rows = db.scalars(
        select(NotificationDelivery)
        .join(
            Alert, Alert.id == NotificationDelivery.alert_id
        )  # organisation-scoped: only this organisation's deliveries
        .where(NotificationDelivery.status.in_(("QUEUED", "ATTEMPTED")), NotificationDelivery.next_attempt_at <= now)
        .order_by(NotificationDelivery.id)
        .limit(limit)
    ).all()
    for d in rows:
        if not N.claim(db, d):
            continue
        db.refresh(d)
        alert = db.get(Alert, d.alert_id)
        if alert is None:
            continue
        N.attempt(
            db,
            d,
            alert,
            now,
            provider=ctx.provider,
            webhooks=ctx.webhooks,
            allowed_hosts=ctx.allowed_hosts,
            timeout=ctx.timeout,
            retry_base=ctx.retry_base,
        )
        counts["attempted"] += 1
        counts[
            {"DELIVERED": "delivered", "ACCEPTED": "accepted", "ATTEMPTED": "retry_scheduled"}.get(d.status, "failed")
        ] += 1
    return counts


# ---------- lifecycle ----------


def get_alert(db: Session, alert_uid: str) -> Alert:
    a = db.scalar(select(Alert).where(Alert.alert_uid == alert_uid))
    if a is None:
        note_probe(db, Alert, Alert.alert_uid, alert_uid, "alert")
        raise NotFoundError(f"Alert {alert_uid} was not found.")
    return a


def transition(
    db: Session,
    alert: Alert,
    to: str,
    actor: str,
    reason: str | None,
    ctx: Ctx,
    *,
    expected_version: int | None = None,
    now: datetime | None = None,
    mode: str = "manual",
) -> Alert:
    now = now or utcnow()
    if expected_version is not None and alert.version != expected_version:
        raise VersionConflictError(f"The alert changed (now version {alert.version}); reload before acting.")
    if to not in TRANSITIONS.get(alert.status, ()):
        raise InvalidTransitionError(f"An alert that is {alert.status} cannot move to {to}.")
    if to in ("resolved", "closed") and not (reason and reason.strip()):
        raise DomainValidationError("A reason is required to resolve or close an alert.")
    pol = db.get(AlertEscalationPolicy, alert.escalation_policy_id) if alert.escalation_policy_id else None
    _record_transition(db, alert, to, actor, reason, mode, now)
    prev = alert.status
    alert.status = to
    if to == "acknowledged":
        alert.acknowledged_at, alert.acknowledged_by = now, actor
        alert.next_escalation_at = (
            alert.due_resolve_at
        )  # an acknowledged alert is next due at its resolution target, if any
    elif to == "investigating":
        alert.next_escalation_at = alert.due_resolve_at
    elif to == "resolved":
        alert.resolved_at, alert.resolved_by, alert.resolution_mode, alert.resolution_note, alert.next_escalation_at = (
            now,
            actor,
            mode if mode != "system" else "auto",
            (reason or "")[:600],
            None,
        )
    elif to == "closed":
        alert.closed_at, alert.next_escalation_at = now, None
    elif to == "open":
        alert.next_escalation_at = None
        if prev == "resolved":  # manual reopen
            alert.reopened_count += 1
            alert.resolved_at = alert.resolved_by = alert.resolution_mode = None
            alert.escalation_level = 0
            if pol:
                alert.due_ack_at = now + timedelta(minutes=pol.ack_minutes)
                alert.next_escalation_at = alert.due_ack_at
        elif prev == "suppressed":
            if pol:
                alert.next_escalation_at = now + timedelta(minutes=pol.ack_minutes)
    db.flush()
    return alert


def assign(
    db: Session,
    alert: Alert,
    assignee: str,
    actor: str,
    note: str | None,
    ctx: Ctx,
    *,
    expected_version: int | None = None,
    now: datetime | None = None,
) -> Alert:
    now = now or utcnow()
    if expected_version is not None and alert.version != expected_version:
        raise VersionConflictError(f"The alert changed (now version {alert.version}); reload before acting.")
    if alert.status in ("closed",):
        raise InvalidTransitionError("A closed alert cannot be reassigned.")
    if not _valid_target(ctx, assignee, assignee=True):
        raise DomainValidationError(
            "assignee: must be a configured user with the editor role or higher, or team:editor|reviewer|admin"
        )
    prev = alert.assignee
    alert.assignee = assignee
    db.add(
        AlertNote(
            alert_id=alert.id,
            kind="assignment",
            text=f"Assigned to {assignee}" + (f" (was {prev})" if prev else "") + (f": {note}" if note else ""),
            author=actor,
            created_at=now,
        )
    )
    db.flush()
    rule = db.get(AlertRule, alert.rule_id) if alert.rule_id else None
    _notify(db, alert, rule, "created", f"assign:{assignee}:{alert.version}", ctx, now, targets=[assignee])
    dispatch_due(db, ctx, now)
    return alert


def add_note(db: Session, alert: Alert, kind: str, text: str, actor: str, now: datetime | None = None) -> AlertNote:
    if not text.strip():
        raise DomainValidationError("The note is empty.")
    n = AlertNote(
        alert_id=alert.id,
        kind=kind,
        text=text.strip()[: LIMITS["note_chars"]],
        author=actor,
        created_at=now or utcnow(),
    )
    db.add(n)
    db.flush()
    return n


def link_alerts(db: Session, alert: Alert, other: Alert, reason: str, actor: str) -> AlertLink:
    if alert.id == other.id:
        raise DomainValidationError("An alert cannot be linked to itself.")
    a, b = sorted((alert.id, other.id))
    if db.scalar(select(AlertLink.id).where(AlertLink.alert_id.in_((a, b)), AlertLink.related_alert_id.in_((a, b)))):
        raise ConflictError("These alerts are already linked.")
    lk = AlertLink(alert_id=a, related_alert_id=b, reason=reason[:200] or "linked by analyst", created_by=actor)
    db.add(lk)
    db.flush()
    return lk


def escalate(
    db: Session,
    alert: Alert,
    actor: str,
    reason: str,
    ctx: Ctx,
    *,
    trigger: str = "manual",
    now: datetime | None = None,
    target: str | None = None,
) -> AlertEscalation | None:
    """Escalate one level. (alert, level) is unique, so the same level cannot be recorded twice even if two workers race."""
    now = now or utcnow()
    if alert.status not in ("open", "acknowledged", "investigating", "escalated"):
        raise InvalidTransitionError(f"An alert that is {alert.status} cannot be escalated.")
    pol = db.get(AlertEscalationPolicy, alert.escalation_policy_id) if alert.escalation_policy_id else None
    level = alert.escalation_level + 1
    if target is None:
        if pol is None:
            raise DomainValidationError("This alert has no escalation policy; supply a target to escalate it manually.")
        if level > len(pol.targets):
            if trigger == "auto":
                alert.next_escalation_at = None
                return None
            raise InvalidTransitionError("The alert is already at the highest escalation level of its policy.")
        target = pol.targets[level - 1]
    elif not _valid_target(ctx, target, assignee=True):
        raise DomainValidationError(
            "target: must be a configured user with the editor role or higher, or team:editor|reviewer|admin"
        )
    rec = AlertEscalation(
        alert_id=alert.id,
        level=level,
        policy_id=pol.id if pol else None,
        policy_version=pol.version if pol else None,
        from_target=alert.assignee,
        to_target=target,
        trigger=trigger,
        actor=actor,
        reason=reason[:400],
        at=now,
    )
    try:
        with db.begin_nested():
            db.add(rec)
            db.flush()
    except IntegrityError:
        return None  # already escalated to this level by another worker
    if alert.status != "escalated":
        _record_transition(db, alert, "escalated", actor, reason, "system" if trigger == "auto" else "manual", now)
        alert.status = "escalated"
    alert.escalation_level, alert.assignee = level, target
    more = pol is not None and level < len(pol.targets)
    alert.next_escalation_at = now + timedelta(minutes=pol.escalate_after_minutes) if more and pol else None
    db.add(
        AlertNote(
            alert_id=alert.id,
            kind="assignment",
            text=f"Escalated to level {level}: {target}. {reason}"[:600],
            author=actor,
            created_at=now,
        )
    )
    db.flush()
    rule = db.get(AlertRule, alert.rule_id) if alert.rule_id else None
    _notify(db, alert, rule, "escalated", f"escalated:{level}", ctx, now, targets=[target])
    dispatch_due(db, ctx, now)
    return rec


def tick(db: Session, actor: str, ctx: Ctx, now: datetime | None = None) -> dict[str, Any]:
    """Process what is due: expired suppressions, overdue escalations, notification retries, inbox retention. Safe to run repeatedly."""
    now = now or utcnow()
    out: dict[str, Any] = {"unsuppressed": 0, "escalated": 0, "notifications": {}, "purged_inbox": 0}
    live = {
        s.id
        for s in db.scalars(
            select(AlertSuppression).where(
                AlertSuppression.revoked_at.is_(None),
                AlertSuppression.starts_at <= now,
                AlertSuppression.expires_at > now,
            )
        )
    }
    for a in db.scalars(select(Alert).where(Alert.status == "suppressed")).all():
        if a.suppression_id not in live:
            pol = db.get(AlertEscalationPolicy, a.escalation_policy_id) if a.escalation_policy_id else None
            _record_transition(db, a, "open", "system", "the suppression expired or was revoked", "system", now)
            a.status = "open"
            a.next_escalation_at = now + timedelta(minutes=pol.ack_minutes) if pol else None
            rule = db.get(AlertRule, a.rule_id) if a.rule_id else None
            _notify(db, a, rule, "created", "unsuppressed", ctx, now)
            out["unsuppressed"] += 1
    due = db.scalars(
        select(Alert)
        .where(
            Alert.next_escalation_at.is_not(None),
            Alert.next_escalation_at <= now,
            Alert.status.in_(("open", "escalated", "acknowledged", "investigating")),
        )
        .order_by(Alert.id)
        .limit(200)
    ).all()
    for a in due:
        db.refresh(a)  # re-check the state right before acting: it may have been acknowledged or resolved meanwhile
        if (
            a.status not in ("open", "escalated", "acknowledged", "investigating")
            or a.next_escalation_at is None
            or aware(a.next_escalation_at) > now
        ):
            continue
        why = (
            "acknowledgement deadline passed"
            if a.status in ("open", "escalated") and not a.acknowledged_at
            else "resolution target passed"
        )
        if escalate(db, a, actor, f"Automatic escalation: {why}.", ctx, trigger="auto", now=now) is not None:
            out["escalated"] += 1
    out["notifications"] = dispatch_due(db, ctx, now)
    cutoff = now - timedelta(days=ctx.retention_days)
    for n in db.scalars(
        select(InboxNotification)
        .where(InboxNotification.read_at.is_not(None), InboxNotification.read_at < cutoff)
        .limit(500)
    ).all():
        db.delete(n)
        out["purged_inbox"] += 1
    return out


# ---------- suppression ----------


def create_suppression(
    db: Session,
    actor: str,
    *,
    rule: AlertRule | None,
    entity_type: str | None,
    entity_id: str | None,
    reason: str,
    minutes: int,
    now: datetime | None = None,
) -> AlertSuppression:
    now = now or utcnow()
    if rule is None and not entity_id:
        raise DomainValidationError(
            "A suppression must name a rule, an entity, or both; a blanket suppression is not allowed."
        )
    if entity_id and entity_type not in ENTITY_TYPES:
        raise DomainValidationError("entity_type: unknown")
    if not reason.strip():
        raise DomainValidationError("A reason is required.")
    if not 1 <= minutes <= 60 * 24 * 30:
        raise DomainValidationError("minutes: a suppression lasts between 1 minute and 30 days")
    s = AlertSuppression(
        rule_id=rule.id if rule else None,
        entity_type=entity_type if entity_id else None,
        entity_id=entity_id,
        reason=reason.strip()[:400],
        created_by=actor,
        starts_at=now,
        expires_at=now + timedelta(minutes=minutes),
    )
    db.add(s)
    db.flush()
    return s


# ---------- reading ----------


def counts_by(db: Session, col: Any, where: Any = None) -> dict[str, int]:
    q = select(col, func.count()).group_by(col)
    if where is not None:
        q = q.where(where)
    return {str(k): int(v) for k, v in db.execute(q).all()}
