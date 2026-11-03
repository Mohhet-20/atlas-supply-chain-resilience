"""Notification dispatch: in-app inbox (foundational) and an allow-listed webhook channel.

Delivery states: QUEUED (waiting) -> ATTEMPTED (a try failed transiently, retry scheduled) -> ACCEPTED (the provider returned
success) or DELIVERED (written to the recipient's in-app inbox, which ATLAS itself confirms) or FAILED (permanent) or EXHAUSTED (retries used up).
A queued job is never reported as delivered, and no provider is contacted unless one is explicitly configured.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.alerting.config import SEV_RANK
from app.models.alerting import (
    Alert,
    InboxNotification,
    NotificationAttempt,
    NotificationDelivery,
    NotificationPreference,
)


def _uid() -> str:
    import uuid

    return "ND-" + uuid.uuid4().hex[:12].upper()


# ---------- destination safety (SSRF) ----------


class UnsafeDestination(ValueError):
    pass


def _blocked(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


def validate_webhook_url(url: str, allowed_hosts: set[str], *, resolve: bool = True) -> str:
    """Return the host of a safe destination or raise UnsafeDestination. Never echoes the URL (it may carry a secret)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise UnsafeDestination("the destination is not a valid URL") from None
    if parts.scheme != "https":
        raise UnsafeDestination("only https destinations are allowed")
    if not host:
        raise UnsafeDestination("the destination has no host")
    if parts.username or parts.password:
        raise UnsafeDestination("credentials in the URL are not allowed")
    if port not in (None, 443):
        raise UnsafeDestination("only port 443 is allowed")
    if host not in allowed_hosts:
        raise UnsafeDestination("the host is not on the configured allow-list")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _blocked(literal):
        raise UnsafeDestination("the destination address is not publicly routable")
    if resolve and literal is None:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise UnsafeDestination("the host could not be resolved") from None
        for info in infos:
            if _blocked(ipaddress.ip_address(info[4][0])):
                raise UnsafeDestination("the host resolves to a non-public address")
    return host


def parse_webhooks(raw: str | None, allowed_hosts: set[str]) -> dict[str, str]:
    """Configured webhooks, validated without DNS. Invalid entries are dropped (and so unusable), not partially trusted."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for name, url in data.items():
            try:
                if isinstance(name, str) and isinstance(url, str):
                    validate_webhook_url(url, allowed_hosts, resolve=False)
                    out[name] = url
            except UnsafeDestination:
                continue
    return out


@dataclass
class SendResult:
    kind: str  # accepted | transient | permanent
    detail: str = ""
    ref: str | None = None


class Provider(Protocol):
    def send(self, url: str, body: bytes, timeout: float, idempotency_key: str) -> SendResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any):  # noqa: ANN201
        return None


class UrllibProvider:
    """The real HTTPS sender: no redirects (a redirect could point inside the network), bounded timeout, no body echoed back."""

    def send(self, url: str, body: bytes, timeout: float, idempotency_key: str) -> SendResult:
        req = urllib.request.Request(  # noqa: S310  (https and allow-list enforced before)
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                "User-Agent": "ATLAS-alerting/1",
            },
        )
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:  # noqa: S310  (https and allow-list enforced before)
                return SendResult("accepted", f"HTTP {r.status}", r.headers.get("X-Request-Id"))
        except urllib.error.HTTPError as e:
            return SendResult("transient" if e.code >= 500 or e.code in (408, 429) else "permanent", f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError):
            return SendResult("transient", "network error or timeout")


# ---------- planning and sending ----------


def expand(directory: dict[str, int], target: str) -> list[str]:
    """Actor name -> [name]; team:<role> -> every configured actor with exactly that role."""
    if target.startswith("team:"):
        want = target[5:].upper()
        from app.core.auth import Role

        return sorted(n for n, r in directory.items() if Role(r).name == want)
    return [target] if target in directory else []


def _quiet_until(pref: NotificationPreference | None, now: datetime) -> datetime | None:
    if pref is None or pref.quiet_start_hour is None or pref.quiet_end_hour is None:
        return None
    s, e, h = pref.quiet_start_hour, pref.quiet_end_hour, now.hour
    inside = (s <= h < e) if s < e else (h >= s or h < e)
    if not inside:
        return None
    end = now.replace(minute=0, second=0, microsecond=0)
    while end.hour != e or end <= now:
        end += timedelta(hours=1)
    return end


def _key(alert: Alert, kind: str, bucket: str, channel: str, recipient: str) -> str:
    return hashlib.sha256(f"{alert.alert_uid}|{kind}|{bucket}|{channel}|{recipient}".encode()).hexdigest()[:40]


def plan(
    db: Session,
    alert: Alert,
    kind: str,
    bucket: str,
    notify: dict[str, Any],
    targets: list[str],
    directory: dict[str, int],
    now: datetime,
    max_attempts: int,
    webhooks: dict[str, str],
) -> list[NotificationDelivery]:
    """Create delivery rows (idempotent per alert/kind/bucket/channel/recipient). Returns the rows created now."""
    created: list[NotificationDelivery] = []
    if SEV_RANK[alert.severity] < SEV_RANK.get(notify.get("min_severity", "LOW"), 1) and kind != "escalated":
        return created
    names: list[str] = []
    for t in targets:
        for n in expand(directory, t):
            if n not in names:
                names.append(n)

    def add(channel: str, recipient: str, status: str, nxt: datetime | None, err: str | None = None) -> None:
        idem = _key(alert, kind, bucket, channel, recipient)
        if db.scalar(select(NotificationDelivery.id).where(NotificationDelivery.idempotency_key == idem)):
            return
        d = NotificationDelivery(
            delivery_uid=_uid(),
            alert_id=alert.id,
            channel=channel,
            recipient=recipient,
            kind=kind,
            status=status,
            max_attempts=max_attempts,
            next_attempt_at=nxt,
            last_error=err,
            idempotency_key=idem,
        )
        db.add(d)
        db.flush()
        created.append(d)

    if "in_app" in notify.get("channels", ["in_app"]):
        for n in names:
            pref = db.scalar(select(NotificationPreference).where(NotificationPreference.actor == n))
            if pref is not None and not pref.in_app_enabled:
                add("in_app", n, "SKIPPED", None, "recipient disabled in-app notifications")
            elif (
                pref is not None
                and SEV_RANK[alert.severity] < SEV_RANK[pref.min_severity]
                and alert.severity != "CRITICAL"
            ):
                add("in_app", n, "SKIPPED", None, f"below the recipient's minimum severity ({pref.min_severity})")
            else:
                until = None if alert.severity == "CRITICAL" else _quiet_until(pref, now)
                add("in_app", n, "QUEUED", until or now)
    if "webhook" in notify.get("channels", []):
        name = notify.get("webhook")
        if name and name in webhooks:
            add("webhook", name, "QUEUED", now)
        else:
            add(
                "webhook",
                name or "(none)",
                "SKIPPED",
                None,
                "the webhook is not configured or its destination is not allowed",
            )
    return created


def _deliver_in_app(db: Session, d: NotificationDelivery, alert: Alert, now: datetime) -> tuple[str, str]:
    db.add(
        InboxNotification(
            recipient=d.recipient,
            alert_id=alert.id,
            delivery_id=d.id,
            kind=d.kind,
            severity=alert.severity,
            source=alert.source,
            title=alert.title[:200],
            body=alert.summary[:600],
        )
    )
    return "delivered", "written to the recipient's inbox"


def attempt(
    db: Session,
    d: NotificationDelivery,
    alert: Alert,
    now: datetime,
    *,
    provider: Provider,
    webhooks: dict[str, str],
    allowed_hosts: set[str],
    timeout: float,
    retry_base: int,
) -> None:
    """Make one delivery attempt for a due delivery and record the outcome."""
    n = d.attempts + 1
    outcome, detail, ref = "permanent", "unsupported channel", None
    if d.channel == "in_app":
        outcome, detail = _deliver_in_app(db, d, alert, now)
    elif d.channel == "webhook":
        url = webhooks.get(d.recipient)
        if not url:
            outcome, detail = "permanent", "the webhook is no longer configured"
        else:
            try:
                validate_webhook_url(url, allowed_hosts)  # resolved addresses are re-checked on every attempt
                body = json.dumps(
                    {
                        "alert": alert.alert_uid,
                        "title": alert.title[:200],
                        "severity": alert.severity,
                        "status": alert.status,
                        "kind": d.kind,
                        "link": f"/alerting/alerts/{alert.alert_uid}",
                    }
                ).encode()
                r = provider.send(url, body, timeout, d.idempotency_key)
                outcome, detail, ref = r.kind, r.detail, r.ref
            except UnsafeDestination as e:
                outcome, detail = "permanent", f"destination refused: {e}"
    db.add(NotificationAttempt(delivery_id=d.id, attempt_no=n, outcome=outcome, detail=detail[:300]))
    d.attempts = n
    d.provider_ref = ref
    if outcome in ("delivered", "accepted"):
        d.status = "DELIVERED" if outcome == "delivered" else "ACCEPTED"
        d.next_attempt_at, d.last_error = None, None
    elif outcome == "permanent":
        d.status, d.next_attempt_at, d.last_error = "FAILED", None, detail[:300]
    elif n >= d.max_attempts:
        d.status, d.next_attempt_at, d.last_error = "EXHAUSTED", None, detail[:300]
    else:
        d.status, d.last_error = "ATTEMPTED", detail[:300]
        d.next_attempt_at = now + timedelta(seconds=retry_base * 2 ** (n - 1))  # bounded exponential backoff


def claim(db: Session, d: NotificationDelivery) -> bool:
    """Optimistically claim a delivery for one attempt (guards against two workers attempting the same try)."""
    r = db.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == d.id,
            NotificationDelivery.attempts == d.attempts,
            NotificationDelivery.status.in_(("QUEUED", "ATTEMPTED")),
        )
        .values(updated_at=datetime.now(timezone.utc))
    )
    return r.rowcount == 1
