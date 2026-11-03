"""Event envelope, catalogue and the transactional outbox fan-out.

``emit`` writes one ``WebhookDelivery`` row per subscribed outbound endpoint *in the caller's transaction*, so an event is queued
if and only if the business change commits. Delivery happens afterwards (see ``webhooks.py``), at least once; receivers dedupe on
``event_id``. No external broker is used: the outbox table plus the existing timer is enough for the target workload and keeps
operations to one database. Revisit if sustained throughput or fan-out outgrows a single SQLite/PostgreSQL writer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integrations import WebhookDelivery, WebhookEndpoint
from app.models.types import utcnow

SCHEMA_VERSION = "1"

# type -> (description, emitted by this build)
CATALOGUE: dict[str, tuple[str, bool]] = {
    "alert.created": ("A new alert was raised for the organisation.", True),
    "integration.sync.completed": ("An integration synchronisation finished (succeeded or partial).", True),
    "integration.sync.failed": ("An integration synchronisation failed.", True),
    "export.ready": ("An export file is ready to download.", True),
    "webhook.test": ("A test event sent on request to one endpoint.", True),
    "supplier.risk.changed": ("A supplier's risk level changed.", False),
    "supplier.status.changed": ("A supplier's status changed.", False),
    "facility.disruption.detected": ("A facility disruption was detected.", False),
    "inventory.threshold.breached": ("An inventory threshold was breached.", False),
    "inventory.stockout.risk_increased": ("Stockout risk increased.", False),
    "delivery.delay.detected": ("A delivery delay was detected.", False),
    "forecast.updated": ("A demand forecast was updated.", False),
    "scenario.completed": ("A disruption scenario run completed.", False),
    "optimization.completed": ("An optimisation run completed.", False),
    "incident.escalated": ("An incident was escalated.", False),
}


def catalogue() -> list[dict[str, Any]]:
    return [
        {"event_type": t, "version": SCHEMA_VERSION, "description": d, "emitted": e}
        for t, (d, e) in sorted(CATALOGUE.items())
    ]


def envelope(
    event_type: str, org_id: int, payload: dict[str, Any], correlation_id: str | None = None, source: str = "atlas"
) -> dict[str, Any]:
    return {
        "event_id": "EV-" + uuid.uuid4().hex[:16].upper(),
        "event_type": event_type,
        "event_version": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "organization_id": org_id,
        "occurred_at": utcnow().isoformat(),
        "correlation_id": correlation_id or uuid.uuid4().hex,
        "source": source,
        "payload": payload,
    }  # fmt: skip


def subscribed(endpoint: WebhookEndpoint, event_type: str) -> bool:
    events = endpoint.events or []
    return "*" in events or event_type in events


def emit(
    db: Session,
    org_id: int,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Queue the event for every active outbound endpoint of the organisation that subscribes to it. Returns the fan-out count."""
    if event_type not in CATALOGUE:
        raise ValueError(f"unknown event type {event_type!r}")
    env = envelope(event_type, org_id, payload, correlation_id)
    now = now or utcnow()
    n = 0
    endpoints = db.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.organization_id == org_id,
            WebhookEndpoint.direction == "outbound",
            WebhookEndpoint.status == "active",
        )
    )
    for ep in endpoints:
        if not subscribed(ep, event_type):
            continue
        db.add(
            WebhookDelivery(
                delivery_uid="WD-" + uuid.uuid4().hex[:14].upper(), organization_id=org_id, endpoint_id=ep.id, event_id=env["event_id"],
                event_type=event_type, envelope=env, status="queued", attempts=0, next_attempt_at=now,
            )
        )  # fmt: skip
        n += 1
    return n
