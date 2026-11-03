"""Integrations storage: service accounts and API keys, the integration registry, sync runs/checkpoints/errors, webhook endpoints
and deliveries (the transactional event outbox), inbound-event inbox, and export jobs.

No secret is stored. API keys are SHA-256 digests of 256-bit random values. Integration credentials are *references*
(``env:NAME``) resolved from the deployment environment at call time. Webhook signing secrets are derived from a server master
key and the endpoint id/version (see ``app/integrations/secrets.py``), so only the version number is stored.
Every table here carries ``organization_id`` and is covered by the tenant filter in ``app/db/tenancy.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime, utcnow

INTEGRATION_STATES = ("draft", "configured", "connected", "syncing", "healthy", "degraded", "failed", "disabled")
RUN_STATES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
DELIVERY_STATES = ("queued", "attempted", "delivered", "failed", "dead")
EXPORT_STATES = ("queued", "running", "ready", "failed", "expired")


def _org() -> Mapped[int]:
    return mapped_column(ForeignKey("organizations.id"), default=1, server_default="1", index=True)


class ServiceAccount(Base):
    __tablename__ = "service_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    sa_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(10))  # ceiling for every key: viewer | editor | reviewer (never admin)
    status: Mapped[str] = mapped_column(String(10), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("role in ('viewer', 'editor', 'reviewer')", name="ck_service_accounts_role"),
        CheckConstraint("status in ('active', 'disabled')", name="ck_service_accounts_status"),
        Index("uq_service_accounts_org_name", "organization_id", "name", unique=True),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    key_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    service_account_id: Mapped[int] = mapped_column(ForeignKey("service_accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # shown in lists so a key can be recognised; not secret
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(10))
    scopes: Mapped[Any] = mapped_column(JSON, default=list)  # narrows permission-checked routes; empty = role defaults
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    rotated_from: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (CheckConstraint("role in ('viewer', 'editor', 'reviewer')", name="ck_api_keys_role"),)


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    integration_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    name: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(24))
    provider: Mapped[str] = mapped_column(String(48))  # key into the connector allow-list, never a code path
    description: Mapped[str | None] = mapped_column(String(300))
    config: Mapped[Any] = mapped_column(JSON, default=dict)  # non-secret settings only
    credential_ref: Mapped[str | None] = mapped_column(String(80))  # "env:NAME"; the secret itself is never stored
    mapping: Mapped[Any] = mapped_column(JSON, default=dict)
    target: Mapped[str] = mapped_column(String(24), default="alert_events")
    status: Mapped[str] = mapped_column(String(12), default="draft")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_summary: Mapped[str | None] = mapped_column(String(300))
    consecutive_failed_runs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    circuit_open_until: Mapped[datetime | None] = mapped_column(UTCDateTime)  # scheduled syncs are skipped until then
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint(f"status in {INTEGRATION_STATES}", name="ck_integrations_status"),
        Index("uq_integrations_org_name", "organization_id", "name", unique=True),
    )


class IntegrationRun(Base):
    __tablename__ = "integration_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(12))  # full | incremental
    trigger: Mapped[str] = mapped_column(String(12))  # manual | scheduled
    status: Mapped[str] = mapped_column(String(10), default="queued")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    discovered: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    checkpoint: Mapped[Any] = mapped_column(JSON, default=dict)
    error_summary: Mapped[str | None] = mapped_column(String(300))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint(f"status in {RUN_STATES}", name="ck_integration_runs_status"),
        # Overlap prevention enforced by the database: at most one active run per integration.
        Index(
            "uq_integration_runs_active",
            "integration_id",
            unique=True,
            sqlite_where=text("status in ('queued', 'running')"),
            postgresql_where=text("status in ('queued', 'running')"),
        ),
    )


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = _org()
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), unique=True)
    cursor: Mapped[Any] = mapped_column(JSON, default=dict)  # last *committed* position for incremental runs
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class SyncError(Base):
    __tablename__ = "sync_errors"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = _org()
    run_id: Mapped[int] = mapped_column(ForeignKey("integration_runs.id", ondelete="CASCADE"), index=True)
    record_ref: Mapped[str | None] = mapped_column(String(128))
    code: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    name: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))  # outbound | inbound
    url: Mapped[str | None] = mapped_column(String(500))  # outbound destination
    events: Mapped[Any] = mapped_column(JSON, default=list)  # subscribed event types (outbound)
    secret_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="active")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("direction in ('outbound', 'inbound')", name="ck_webhook_endpoints_direction"),
        CheckConstraint("status in ('active', 'disabled')", name="ck_webhook_endpoints_status"),
        Index("uq_webhook_endpoints_org_name", "organization_id", "name", unique=True),
    )


class WebhookDelivery(Base):
    """Transactional outbox: written in the same transaction as the business event, delivered afterwards at least once."""

    __tablename__ = "webhook_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[Any] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(300))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)  # claimed by one worker until this instant
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (
        CheckConstraint(f"status in {DELIVERY_STATES}", name="ck_webhook_deliveries_status"),
        Index("uq_webhook_deliveries_endpoint_event", "endpoint_id", "event_id", unique=True),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )


class InboundEvent(Base):
    """Inbox: one row per accepted inbound webhook, unique per (endpoint, provider event id) for replay protection."""

    __tablename__ = "inbound_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = _org()
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("webhook_endpoints.id", ondelete="CASCADE"))
    provider_event_id: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    outcome: Mapped[str] = mapped_column(String(16))  # accepted | duplicate_event | rejected
    detail: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (Index("uq_inbound_events_endpoint_event", "endpoint_id", "provider_event_id", unique=True),)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    export_uid: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = _org()
    dataset: Mapped[str] = mapped_column(String(24))
    format: Mapped[str] = mapped_column(String(6))
    filters: Mapped[Any] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="queued")
    row_count: Mapped[int | None] = mapped_column(Integer)
    file_name: Mapped[str | None] = mapped_column(
        String(80)
    )  # generated name under the exports directory; never user input
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (
        CheckConstraint(f"status in {EXPORT_STATES}", name="ck_export_jobs_status"),
        CheckConstraint("format in ('csv', 'json')", name="ck_export_jobs_format"),
        Index("ix_export_jobs_org_created", "organization_id", "created_at"),
    )


__all__ = [
    "ApiKey", "ExportJob", "InboundEvent", "Integration", "IntegrationRun", "ServiceAccount", "SyncCheckpoint", "SyncError", "WebhookDelivery", "WebhookEndpoint",
]  # fmt: skip
