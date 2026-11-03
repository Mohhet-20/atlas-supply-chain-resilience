"""Append-only, hash-chained audit log for registry actions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor
from app.models.enums import AuditOutcome
from app.models.registry import AuditEvent
from app.models.types import utcnow

GENESIS = "0" * 64
_SECRET_KEY = re.compile(r"(secret|token|password|passwd|authorization|api[_-]?key|credential)", re.I)
MAX_DETAIL_CHARS = 4000


def safe_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Drop secret-looking keys, shorten long strings and bound the overall size. Never pass raw payloads."""
    out: dict[str, Any] = {}
    for key, value in (details or {}).items():
        if _SECRET_KEY.search(str(key)):
            continue
        out[str(key)[:64]] = _clean(value)
    text = json.dumps(out, default=str, sort_keys=True)
    if len(text) > MAX_DETAIL_CHARS:
        return {"truncated": True, "keys": sorted(out)[:30]}
    return out


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return str(value)[:80]
    if isinstance(value, dict):
        return {
            str(k)[:64]: _clean(v, depth + 1) for k, v in list(value.items())[:30] if not _SECRET_KEY.search(str(k))
        }
    if isinstance(value, (list, tuple, set)):
        return [_clean(v, depth + 1) for v in list(value)[:30]]
    return str(value)[:80]


def _hash(prev: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256((prev + blob).encode("utf-8")).hexdigest()


def record(
    session: Session,
    actor: Actor,
    action: str,
    *,
    target_type: str | None = None,
    target_id: int | str | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
    organization_id: int | None = None,
) -> AuditEvent:
    """Add an audit event inside the caller's transaction (so it commits or rolls back with the action)."""
    prev = session.scalar(select(AuditEvent.event_hash).order_by(AuditEvent.id.desc()).limit(1)) or GENESIS
    occurred = utcnow()
    clean = safe_details(details)
    payload = {
        "occurred_at": occurred.isoformat(),
        "actor": actor.name,
        "actor_role": actor.role.name,
        "authenticated": actor.authenticated,
        "action": action,
        "target_type": target_type,
        "target_id": None if target_id is None else str(target_id),
        "outcome": outcome.value,
        "request_id": request_id,
        "details": clean,
    }
    org = organization_id if organization_id is not None else getattr(actor, "org_id", None)
    if org is not None:  # included in the hash only when present, so events recorded before tenancy still verify
        payload["organization_id"] = org
    event = AuditEvent(
        occurred_at=occurred,
        actor=actor.name,
        actor_role=actor.role.name,
        authenticated=actor.authenticated,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        outcome=outcome,
        request_id=request_id,
        organization_id=org,
        details=clean,
        prev_hash=prev,
        event_hash=_hash(prev, payload),
    )
    session.add(event)
    session.flush()
    return event


def request_id_of(request: Request) -> str | None:
    return (request.scope.get("state") or {}).get("request_id")


def record_denied(request: Request, actor: Actor, why: str) -> None:
    """Persist a DENIED event in its own short transaction (the request itself is about to fail)."""
    factory = request.app.state.session_factory
    with factory() as session, session.begin():
        record(
            session,
            actor,
            "authz.denied",
            outcome=AuditOutcome.DENIED,
            details={"path": request.url.path, "method": request.method, "reason": why},
            request_id=request_id_of(request),
        )


def verify_chain(session: Session, limit: int = 100_000) -> dict[str, Any]:
    """Recompute the hash chain; reports the first inconsistent event id, if any."""
    prev = GENESIS
    count = 0
    for ev in session.scalars(select(AuditEvent).order_by(AuditEvent.id).limit(limit)):
        payload = {
            "occurred_at": ev.occurred_at.isoformat(),
            "actor": ev.actor,
            "actor_role": ev.actor_role,
            "authenticated": ev.authenticated,
            "action": ev.action,
            "target_type": ev.target_type,
            "target_id": ev.target_id,
            "outcome": ev.outcome.value,
            "request_id": ev.request_id,
            "details": ev.details,
        }
        if ev.organization_id is not None:
            payload["organization_id"] = ev.organization_id
        if ev.prev_hash != prev or ev.event_hash != _hash(prev, payload):
            return {"ok": False, "checked": count, "first_bad_event_id": ev.id}
        prev = ev.event_hash
        count += 1
    return {"ok": True, "checked": count, "first_bad_event_id": None}


def audit_scope(actor: Actor):
    """SQL condition limiting audit rows to the actor's organisation (the default organisation also owns pre-tenancy rows)."""
    from sqlalchemy import or_

    if actor.org_id == 1:
        return or_(AuditEvent.organization_id == 1, AuditEvent.organization_id.is_(None))
    return AuditEvent.organization_id == actor.org_id
