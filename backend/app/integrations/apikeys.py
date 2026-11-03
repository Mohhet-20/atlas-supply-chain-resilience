"""Service accounts and API keys.

A key is ``atl_k_`` + 256 random bits, shown once and stored only as SHA-256. It belongs to a service account of one organisation,
has a role *ceiling* (viewer/editor/reviewer - never admin), optional narrower permission scopes, a mandatory expiry (default 90 days,
at most 365) and can be revoked or rotated. Scopes narrow routes guarded by the identity permission map; routes that still use the
older role check are limited by the role ceiling only - pick the lowest role that works.
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor, Role
from app.core.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError
from app.core.permissions import ROLE_PERMISSIONS, Permission
from app.models.identity import Organization
from app.models.integrations import ApiKey, ServiceAccount
from app.models.types import utcnow

KEY_PREFIX = "atl_k_"
KEY_ROLES = ("viewer", "editor", "reviewer")
FORBIDDEN_SCOPES = {
    Permission.USER_MANAGE,
    Permission.INTEGRATION_ADMIN,
    Permission.ORG_MANAGE,
    Permission.SYSTEM_ADMIN,
}
DEFAULT_TTL_DAYS, MAX_TTL_DAYS = 90, 365
USAGE_WRITE_INTERVAL = 60  # seconds


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(d: datetime | None) -> datetime | None:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=utcnow().tzinfo)
    return d


def get_sa(db: Session, sa_uid: str) -> ServiceAccount:
    sa = db.scalar(select(ServiceAccount).where(ServiceAccount.sa_uid == sa_uid))
    if sa is None:
        raise NotFoundError("Service account not found.")
    return sa


def create_sa(db: Session, actor: Actor, name: str, description: str | None, role: str) -> ServiceAccount:
    if role not in KEY_ROLES:
        raise DomainValidationError(
            f"Role must be one of: {', '.join(KEY_ROLES)}. Service accounts are never administrators."
        )
    if Role[role.upper()] > actor.role:
        raise ForbiddenError("You cannot create a service account above your own role.")
    if db.scalar(select(ServiceAccount.id).where(ServiceAccount.name == name)):
        raise ConflictError("A service account with that name already exists.")
    sa = ServiceAccount(
        sa_uid="SA-" + uuid.uuid4().hex[:12].upper(),
        organization_id=actor.org_id,
        name=name,
        description=description,
        role=role,
        created_by=actor.name,
    )
    db.add(sa)
    db.flush()
    return sa


def _check_scopes(role: str, scopes: list[str]) -> list[str]:
    allowed = {p.value for p in ROLE_PERMISSIONS[Role[role.upper()]] if p not in FORBIDDEN_SCOPES}
    bad = sorted(set(scopes) - allowed)
    if bad:
        raise DomainValidationError(f"Scopes not allowed for role {role}: {', '.join(bad)}.")
    return sorted(set(scopes))


def create_key(
    db: Session,
    actor: Actor,
    sa: ServiceAccount,
    name: str,
    role: str | None,
    scopes: list[str],
    ttl_days: int | None,
    rotated_from: str | None = None,
    now: datetime | None = None,
) -> tuple[str, ApiKey]:
    now = now or utcnow()
    if sa.status != "active":
        raise ConflictError("The service account is disabled.")
    role = role or sa.role
    if role not in KEY_ROLES or Role[role.upper()] > Role[sa.role.upper()]:
        raise DomainValidationError("A key's role cannot exceed its service account's role.")
    ttl = ttl_days if ttl_days is not None else DEFAULT_TTL_DAYS
    if not 1 <= ttl <= MAX_TTL_DAYS:
        raise DomainValidationError(f"Key lifetime must be 1-{MAX_TTL_DAYS} days.")
    raw = KEY_PREFIX + pysecrets.token_urlsafe(32)
    key = ApiKey(
        key_uid="AK-" + uuid.uuid4().hex[:12].upper(), organization_id=sa.organization_id, service_account_id=sa.id, name=name, prefix=raw[:12],
        key_hash=digest(raw), role=role, scopes=_check_scopes(role, scopes), expires_at=now + timedelta(days=ttl), created_by=actor.name, rotated_from=rotated_from,
    )  # fmt: skip
    db.add(key)
    db.flush()
    return raw, key


def get_key(db: Session, key_uid: str) -> ApiKey:
    key = db.scalar(select(ApiKey).where(ApiKey.key_uid == key_uid))
    if key is None:
        raise NotFoundError("API key not found.")
    return key


def revoke(db: Session, key: ApiKey, now: datetime | None = None) -> None:
    if key.revoked_at is None:
        key.revoked_at = now or utcnow()


def rotate(
    db: Session, actor: Actor, key: ApiKey, grace_hours: int = 24, now: datetime | None = None
) -> tuple[str, ApiKey]:
    """Issue a replacement with the same settings; the old key keeps working for the grace period, then expires."""
    now = now or utcnow()
    if key.revoked_at is not None:
        raise ConflictError("A revoked key cannot be rotated.")
    sa = db.get(ServiceAccount, key.service_account_id)
    ttl = max(1, min(MAX_TTL_DAYS, DEFAULT_TTL_DAYS))
    raw, new = create_key(
        db, actor, sa, key.name, key.role, list(key.scopes or []), ttl, rotated_from=key.key_uid, now=now
    )
    soon = now + timedelta(hours=grace_hours)
    old_exp = _aware(key.expires_at)
    key.expires_at = soon if old_exp is None or old_exp > soon else old_exp
    return raw, new


def resolve_key(db: Session, raw: str, now: datetime | None = None) -> Actor | None:
    """The actor for a valid key, or None. Never raises, so every failure looks identical to the caller."""
    now = now or utcnow()
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest(raw)).execution_options(skip_tenant=True))
    if key is None or key.revoked_at is not None:
        return None
    exp = _aware(key.expires_at)
    if exp is not None and exp <= now:
        return None
    sa = db.scalar(
        select(ServiceAccount).where(ServiceAccount.id == key.service_account_id).execution_options(skip_tenant=True)
    )
    org = db.get(Organization, key.organization_id)
    if (
        sa is None
        or sa.status != "active"
        or org is None
        or org.status != "active"
        or sa.organization_id != key.organization_id
    ):
        return None
    last = _aware(key.last_used_at)
    if last is None or (now - last).total_seconds() > USAGE_WRITE_INTERVAL:
        key.last_used_at = now
        key.use_count = (key.use_count or 0) + 1  # sampled: at most one increment per minute
        db.flush()
    role = Role[min(key.role, sa.role, key=lambda r: Role[r.upper()]).upper()]
    return Actor(
        name=f"sa:{sa.name}",
        role=role,
        authenticated=True,
        org_id=key.organization_id,
        scopes=frozenset(key.scopes or []) or None,
        api_key_id=key.id,
    )
