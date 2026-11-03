"""Outbound delivery worker and inbound verification.

Outbound: signed (``X-Atlas-Signature: t=<unix>,v1=<hmac>``), https-only, host allow-list, private-network and redirect refusal
(reusing the alerting destination validator), bounded timeout, exponential back-off, dead-letter after ``webhook_max_attempts``,
auto-disable after repeated permanent failures, manual redelivery. Inbound: signature + timestamp window + replay table.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.alerting.notify import SendResult, UnsafeDestination, _NoRedirect, validate_webhook_url
from app.core.config import Settings
from app.db.session import write_transaction
from app.integrations import secrets
from app.integrations.events import envelope
from app.models.integrations import WebhookDelivery, WebhookEndpoint
from app.models.types import utcnow

AUTO_DISABLE_AFTER = 10
MAX_BACKOFF_SECONDS = 3600


class Sender(Protocol):
    def send(self, url: str, body: bytes, timeout: float, headers: dict[str, str]) -> SendResult: ...


class HttpSender:
    """Real HTTPS sender. No redirects, bounded timeout, response body is never read into the delivery record."""

    def send(self, url: str, body: bytes, timeout: float, headers: dict[str, str]) -> SendResult:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "ATLAS-webhooks/1", **headers},
        )  # noqa: S310  (https + allow-list validated by the caller)
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:  # noqa: S310
                return SendResult("accepted", f"HTTP {r.status}", r.headers.get("X-Request-Id"))
        except urllib.error.HTTPError as e:
            return SendResult(
                "transient" if e.code >= 500 or e.code in (408, 425, 429) else "permanent", f"HTTP {e.code}"
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            return SendResult("transient", "network error or timeout")


def hosts_of(cfg: Settings) -> set[str]:
    return {h.strip().lower() for h in cfg.integration_hosts.split(",") if h.strip()}


def backoff(cfg: Settings, attempts: int) -> int:
    return min(cfg.webhook_retry_base_seconds * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS)


def validate_destination(cfg: Settings, url: str, *, resolve: bool = True) -> None:
    validate_webhook_url(url, hosts_of(cfg), resolve=resolve)


def headers_for(cfg: Settings, ep: WebhookEndpoint, d: WebhookDelivery, body: bytes, ts: int) -> dict[str, str]:
    secret = secrets.derive_secret(cfg, "out", ep.endpoint_uid, ep.secret_version)
    return {
        "X-Atlas-Signature": secrets.sign(secret, ts, body),
        "X-Atlas-Event-Id": d.event_id,
        "X-Atlas-Event-Type": d.event_type,
        "X-Atlas-Delivery": d.delivery_uid,
        "Idempotency-Key": d.event_id,
    }


def deliver_one(
    db: Session, cfg: Settings, sender: Sender, d: WebhookDelivery, now: datetime, *, resolve: bool = True
) -> str:
    import json

    ep = db.get(WebhookEndpoint, d.endpoint_id)
    problem: str | None = None
    if ep is None or ep.status != "active" or not ep.url:
        problem = "endpoint is disabled or removed"
    else:
        try:
            validate_destination(cfg, ep.url, resolve=resolve)
        except UnsafeDestination as e:
            problem = f"blocked: {e}"[:300]
    # All reads are done. End the read snapshot, then take the write lock first for the attempt record: a deferred
    # transaction that read and then writes is refused at once with "database is locked" when another worker wrote
    # in between, whatever the busy timeout.
    db.commit()
    write_transaction(db)
    d.attempts += 1
    if problem is not None:
        d.status, d.last_error, d.next_attempt_at = "failed", problem, None
        return d.status
    body = json.dumps(d.envelope, separators=(",", ":"), sort_keys=True).encode()
    db.commit()  # persist the attempt and release the lock before the slow network call
    res = sender.send(ep.url, body, cfg.webhook_timeout_seconds, headers_for(cfg, ep, d, body, int(time.time())))
    write_transaction(db)
    d.last_error = None if res.kind == "accepted" else res.detail[:300]
    if res.kind == "accepted":
        d.status, d.delivered_at, d.next_attempt_at = "delivered", now, None
        ep.consecutive_failures = 0
    elif res.kind == "transient" and d.attempts < cfg.webhook_max_attempts:
        d.status, d.next_attempt_at = "attempted", now + timedelta(seconds=backoff(cfg, d.attempts))
    else:
        d.status, d.next_attempt_at = (
            ("dead" if res.kind == "transient" else "failed"),
            None,
        )  # dead-letter after the retry budget
        ep.consecutive_failures += 1
        if ep.consecutive_failures >= AUTO_DISABLE_AFTER:
            ep.status, ep.disabled_reason = "disabled", "disabled automatically after repeated delivery failures"
    return d.status


def deliver_due(
    db: Session, cfg: Settings, sender: Sender, now: datetime | None = None, limit: int = 50, *, resolve: bool = True
) -> dict[str, int]:
    now = now or utcnow()
    due = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.status.in_(("queued", "attempted")), WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.id)
        .limit(limit)
    ).all()
    # End the read snapshot first: a connection that read and then tries to write can be refused at once by SQLite
    # ("database is locked") instead of waiting for the competing writer, so the claim must start its own transaction.
    db.commit()
    out: dict[str, int] = {}
    lease = timedelta(seconds=cfg.job_lease_seconds)
    for d in due:
        write_transaction(db)
        # Atomic claim: only one worker flips the lease for a delivery whose lease is free or expired (crash recovery).
        claimed = db.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == d.id,
                WebhookDelivery.status.in_(("queued", "attempted")),
                WebhookDelivery.next_attempt_at <= now,
                or_(WebhookDelivery.lease_until.is_(None), WebhookDelivery.lease_until < now),
            )
            .values(lease_until=now + lease)
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed != 1:
            db.rollback()
            continue
        db.commit()  # make the claim visible to other workers before the slow network call
        db.refresh(d)
        state = deliver_one(db, cfg, sender, d, now, resolve=resolve)
        d.lease_until = None
        out[state] = out.get(state, 0) + 1
        db.commit()
    return out


def redeliver(d: WebhookDelivery, now: datetime | None = None) -> None:
    """Manual redelivery of a failed or dead delivery: the same event id, so receivers can still dedupe."""
    d.status, d.attempts, d.next_attempt_at, d.last_error = "queued", 0, now or utcnow(), None
    d.lease_until = None


def inbound_envelope(ep: WebhookEndpoint, data: dict[str, Any]) -> dict[str, Any]:
    return envelope("webhook.inbound", ep.organization_id, data, source=f"inbound:{ep.endpoint_uid}")
