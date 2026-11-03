"""Alerting storage: events, alert rules, alerts and their insert-only history, notifications, escalation and suppression.

Alerts are a *workflow layer*: they never replace the signals that earlier modules store (risk assessments, stockout findings, delivery
assessments, runs, quality). An alert references the evidence it was raised from and keeps the rule version that raised it. Occurrences,
transitions, notes, links, escalations and delivery attempts are insert-only; history is never deleted to tidy a dashboard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime, utcnow

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
ALERT_STATES = ("open", "acknowledged", "investigating", "escalated", "resolved", "closed", "suppressed")
ACTIVE_STATES = ("open", "acknowledged", "investigating", "escalated", "suppressed")
BASES = ("observed", "predicted", "assessed", "hypothetical")
EVENT_STATUSES = ("RECEIVED", "PROCESSED", "REJECTED")
DELIVERY_STATES = ("QUEUED", "ATTEMPTED", "ACCEPTED", "DELIVERED", "FAILED", "EXHAUSTED", "SKIPPED")
_ACTIVE_SQL = "status IN ('open','acknowledged','investigating','escalated','suppressed')"


class AlertEvent(Base):
    """An event ATLAS received. ``occurred_at`` (when it happened at the source) and ``ingested_at`` (when ATLAS got it) are kept apart."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), default=1, server_default="1", index=True
    )
    event_type: Mapped[str] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    provenance: Mapped[str] = mapped_column(String(24))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    causation_id: Mapped[str | None] = mapped_column(String(64))
    entities: Mapped[Any] = mapped_column(JSON, nullable=True)
    payload: Mapped[Any] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10), default="RECEIVED")
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    ingested_by: Mapped[str] = mapped_column(String(64))
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("organization_id", "source", "idempotency_key", name="uq_alert_events_idempotency"),
        CheckConstraint(f"status in {EVENT_STATUSES}", name="ck_alert_events_status"),
        Index("ix_alert_events_type", "event_type", "occurred_at"),
    )


class AlertEscalationPolicy(Base):
    __tablename__ = "alert_escalation_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), default=1, server_default="1", index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    ack_minutes: Mapped[int] = mapped_column(Integer)
    resolve_minutes: Mapped[int | None] = mapped_column(Integer)
    escalate_after_minutes: Mapped[int] = mapped_column(Integer)  # delay between levels
    targets: Mapped[Any] = mapped_column(JSON)  # ordered: level 1, level 2, ... (actor names or team:<role>)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_alert_policies_org_name"),
        CheckConstraint("ack_minutes >= 1 AND escalate_after_minutes >= 1", name="ck_alert_policy_minutes"),
    )


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), default=1, server_default="1", index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")
    detector: Mapped[str] = mapped_column(String(40))
    params: Mapped[Any] = mapped_column(JSON)  # validated against the detector's schema
    severity: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    dedup_window_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    auto_resolve: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    notify: Mapped[Any] = mapped_column(JSON)  # {"channels": [...], "recipients": [...], "min_severity": "LOW"}
    default_assignee: Mapped[str | None] = mapped_column(String(64))
    escalation_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_escalation_policies.id", ondelete="RESTRICT")
    )
    effective_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_alert_rules_org_name"),
        CheckConstraint(f"severity in {SEVERITIES}", name="ck_alert_rules_severity"),
        CheckConstraint("dedup_window_minutes >= 0 AND cooldown_minutes >= 0", name="ck_alert_rules_windows"),
    )


class AlertRuleVersion(Base):
    """Insert-only snapshot of every rule version, so an alert can show the exact definition that raised it."""

    __tablename__ = "alert_rule_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[Any] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_alert_rule_versions"),)


class AlertRuleRun(Base):
    """What one evaluation of one rule did, so an operator can see why a condition did or did not create an alert."""

    __tablename__ = "alert_rule_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="RESTRICT"))
    rule_version: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    trigger: Mapped[str] = mapped_column(String(16))  # evaluate | event | tick
    outcome: Mapped[str] = mapped_column(String(16))  # matched | no_match | no_data | skipped | error
    evaluated: Mapped[int] = mapped_column(Integer, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    suppressed: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    detail: Mapped[Any] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_alert_rule_runs_rule", "rule_id", "id"),)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), default=1, server_default="1", index=True
    )
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="RESTRICT"))
    rule_version: Mapped[int | None] = mapped_column(Integer)  # version that CREATED the alert
    last_rule_version: Mapped[int | None] = mapped_column(Integer)
    detector: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(
        String(40)
    )  # supplier_risk | inventory | delivery | disruption | data_quality | processing | forecast | event
    dedup_key: Mapped[str] = mapped_column(String(64))
    correlation_key: Mapped[str] = mapped_column(String(64))  # entity + category, used to relate (never merge) alerts
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(600))
    basis: Mapped[str] = mapped_column(String(12))
    severity: Mapped[str] = mapped_column(String(10))
    priority: Mapped[int] = mapped_column(Integer)  # 1 (most urgent) .. 4
    explanation: Mapped[Any] = mapped_column(JSON)  # severity and priority factors
    entity_type: Mapped[str | None] = mapped_column(String(24))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    entity_label: Mapped[str | None] = mapped_column(String(200))
    condition: Mapped[Any] = mapped_column(JSON)  # signal, operator, threshold, observed value, unit
    evidence: Mapped[Any] = mapped_column(JSON, nullable=True)
    link: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(14), default="open")
    assignee: Mapped[str | None] = mapped_column(String(64))
    escalation_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_escalation_policies.id", ondelete="RESTRICT")
    )
    escalation_level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    due_ack_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    due_resolve_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_escalation_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notifications_throttled: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_mode: Mapped[str | None] = mapped_column(String(10))  # manual | auto
    resolution_note: Mapped[str | None] = mapped_column(String(600))
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    suppression_id: Mapped[int | None] = mapped_column(Integer)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        CheckConstraint(f"status in {ALERT_STATES}", name="ck_alerts_status"),
        CheckConstraint(f"severity in {SEVERITIES}", name="ck_alerts_severity"),
        CheckConstraint(f"basis in {BASES}", name="ck_alerts_basis"),
        CheckConstraint("priority >= 1 AND priority <= 4", name="ck_alerts_priority"),
        Index("ix_alerts_status", "status", "priority", "id"),
        Index("ix_alerts_org_last_seen", "organization_id", "last_seen_at"),
        Index("ix_alerts_correlation", "correlation_key"),
        Index("ix_alerts_entity", "entity_type", "entity_id"),
        Index("ix_alerts_assignee", "assignee", "status"),
        # One live alert per dedup key; repeats add an occurrence instead of inserting.
        Index(
            "uq_alerts_active_dedup",
            "dedup_key",
            unique=True,
            sqlite_where=text(_ACTIVE_SQL),
            postgresql_where=text(_ACTIVE_SQL),
        ),
    )


class AlertOccurrence(Base):
    __tablename__ = "alert_occurrences"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("alert_events.id", ondelete="RESTRICT"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    value: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[Any] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_alert_occurrences_alert", "alert_id", "id"),)


class AlertTransition(Base):
    __tablename__ = "alert_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    from_state: Mapped[str | None] = mapped_column(String(14))
    to_state: Mapped[str] = mapped_column(String(14))
    actor: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(10))  # manual | system
    reason: Mapped[str | None] = mapped_column(String(600))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_alert_transitions_alert", "alert_id", "id"),)


class AlertNote(Base):
    __tablename__ = "alert_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(12))  # note | mitigation | assignment
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("kind in ('note','mitigation','assignment')", name="ck_alert_notes_kind"),
        Index("ix_alert_notes_alert", "alert_id", "id"),
    )


class AlertLink(Base):
    __tablename__ = "alert_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    related_alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))  # an actor name, or "system" for automatic correlation
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("alert_id", "related_alert_id", name="uq_alert_links_pair"),
        CheckConstraint("alert_id <> related_alert_id", name="ck_alert_links_distinct"),
    )


class AlertSuppression(Base):
    __tablename__ = "alert_suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), default=1, server_default="1", index=True
    )
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="RESTRICT"))
    entity_type: Mapped[str | None] = mapped_column(String(24))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(400))
    created_by: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by: Mapped[str | None] = mapped_column(String(64))
    matched_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("rule_id IS NOT NULL OR entity_id IS NOT NULL", name="ck_alert_suppressions_scoped"),
        CheckConstraint("expires_at > starts_at", name="ck_alert_suppressions_window"),
    )


class AlertEscalation(Base):
    """One escalation of one alert to one level. (alert, level) is unique, so a repeated tick cannot escalate twice."""

    __tablename__ = "alert_escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    level: Mapped[int] = mapped_column(Integer)
    policy_id: Mapped[int | None] = mapped_column(Integer)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    from_target: Mapped[str | None] = mapped_column(String(64))
    to_target: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(10))  # auto | manual
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(400))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("alert_id", "level", name="uq_alert_escalations_level"),)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_uid: Mapped[str] = mapped_column(String(40), unique=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    channel: Mapped[str] = mapped_column(String(12))  # in_app | webhook
    recipient: Mapped[str] = mapped_column(String(200))  # actor name, or a configured webhook name (never a raw URL)
    kind: Mapped[str] = mapped_column(String(12))  # created | repeat | escalated | resolved
    status: Mapped[str] = mapped_column(String(10), default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=4)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(String(300))
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notification_deliveries_idem"),
        CheckConstraint(f"status in {DELIVERY_STATES}", name="ck_notification_deliveries_status"),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_alert", "alert_id", "id"),
    )


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("notification_deliveries.id", ondelete="RESTRICT"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    outcome: Mapped[str] = mapped_column(String(12))  # accepted | delivered | transient | permanent
    detail: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_attempts_no"),)


class InboxNotification(Base):
    """The in-app notification centre. Read state belongs to the recipient; reading never changes the alert."""

    __tablename__ = "inbox_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient: Mapped[str] = mapped_column(String(64))
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="RESTRICT"))
    delivery_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    severity: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(600))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_inbox_recipient", "recipient", "read_at", "id"),)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), unique=True)
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    min_severity: Mapped[str] = mapped_column(String(10), default="LOW")
    quiet_start_hour: Mapped[int | None] = mapped_column(
        Integer
    )  # UTC hours 0-23; quiet hours defer non-critical delivery
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (CheckConstraint(f"min_severity in {SEVERITIES}", name="ck_notification_preferences_sev"),)
