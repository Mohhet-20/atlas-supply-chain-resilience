"""Users, organisations, sessions, password changes and resets. Everything is organisation-scoped by the *actor*, never by a client id.

Security properties (each has a test in tests/test_security_*.py):
- passwords: salted scrypt; policy checked; unknown and wrong-password logins take the same work and return the same message;
- sessions: opaque random tokens, only their SHA-256 digest is stored; absolute and idle expiry; revoked on logout, on password
  change/reset, on role or status change and when the account or its organisation is disabled or suspended;
- throttling: failures are counted per (username, client) so one attacker cannot lock a real user out from elsewhere; the back-off
  is bounded and exponential, never a permanent lock;
- reset tokens: 256-bit random, hashed at rest, single use, short expiry, previous unused tokens are voided when a new one is issued.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import passwords
from app.core.auth import Actor, Role
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    DomainValidationError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnauthenticatedError,
)
from app.models.identity import (
    DEFAULT_ORG_ID,
    USER_ROLES,
    AuthSession,
    LoginThrottle,
    Organization,
    PasswordResetToken,
    User,
)
from app.models.types import utcnow

SESSION_PREFIX = "atl_s_"
GENERIC_LOGIN_ERROR = "Invalid username or password."
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}\Z")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}\Z")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_uid(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6).upper()}"


def role_of(user: User) -> Role:
    return Role[user.role.upper()]


def actor_of(user: User, session: AuthSession | None = None) -> Actor:
    return Actor(
        user.username,
        role_of(user),
        True,
        user.organization_id,
        user.id,
        bool(user.platform_admin),
        session.id if session else None,
    )


# ------------------------------------------------------------------ throttling


def _throttle_key(username: str, client: str) -> str:
    return digest(f"{username.strip().lower()}|{client}")


def _check_throttle(db: Session, key: str, now: datetime) -> None:
    row = db.get(LoginThrottle, key)
    if row and row.blocked_until and row.blocked_until > now:
        raise TooManyRequestsError(
            "Too many failed attempts. Try again later.", int((row.blocked_until - now).total_seconds()) + 1
        )


def _record_failure(db: Session, cfg: Settings, key: str, now: datetime) -> None:
    row = db.get(LoginThrottle, key)
    if row is None:
        row = LoginThrottle(key_hash=key, failures=0, window_started_at=now)
        db.add(row)
    if (now - row.window_started_at).total_seconds() > cfg.auth_login_window_seconds:
        row.failures, row.window_started_at, row.blocked_until = 0, now, None
    row.failures += 1
    over = row.failures - cfg.auth_login_max_failures
    if over >= 0:  # 30 s, 60 s, 120 s ... capped at 15 minutes: slows guessing without a permanent lock
        row.blocked_until = now + timedelta(seconds=min(900, 30 * 2**over))
    db.flush()


def _clear_failures(db: Session, key: str) -> None:
    row = db.get(LoginThrottle, key)
    if row:
        db.delete(row)
        db.flush()


# ------------------------------------------------------------------ sessions


class LoginFailed(UnauthenticatedError):
    """Carries the internal reason for the audit trail; the message shown to the client is always generic."""

    def __init__(self, reason: str, user: User | None = None) -> None:
        super().__init__(GENERIC_LOGIN_ERROR)
        self.reason, self.user = reason, user


def login(
    db: Session, cfg: Settings, username: str, password: str, client: str, now: datetime | None = None
) -> tuple[str, User, AuthSession]:
    now = now or utcnow()
    key = _throttle_key(username, client)
    _check_throttle(db, key, now)
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    n, r, p = cfg.auth_scrypt_n, cfg.auth_scrypt_r, cfg.auth_scrypt_p
    if user is None:
        passwords.dummy_verify(password, n, r, p)
        _record_failure(db, cfg, key, now)
        raise LoginFailed("unknown_user")
    if not passwords.verify_password(password, user.password_hash):
        _record_failure(db, cfg, key, now)
        raise LoginFailed("bad_password", user)
    org = db.get(Organization, user.organization_id)
    if user.status != "active" or org is None or org.status != "active":
        _record_failure(db, cfg, key, now)
        raise LoginFailed(f"account_{user.status}" if user.status != "active" else "organization_suspended", user)
    _clear_failures(db, key)
    if passwords.needs_rehash(user.password_hash, n, r, p):
        user.password_hash = passwords.hash_password(password, n, r, p)
    raw = SESSION_PREFIX + secrets.token_urlsafe(32)
    sess = AuthSession(
        token_hash=digest(raw), user_id=user.id, epoch=user.session_epoch, created_at=now, last_seen_at=now,
        expires_at=now + timedelta(minutes=cfg.auth_session_ttl_minutes), client_hash=digest(client)[:16],
    )  # fmt: skip
    db.add(sess)
    user.last_login_at = now
    db.flush()
    return raw, user, sess


def resolve_session(db: Session, cfg: Settings, raw: str, now: datetime | None = None) -> Actor | None:
    """The actor for a valid session token, or None. Never raises, so callers can treat every failure identically."""
    now = now or utcnow()
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
    if sess is None or sess.revoked_at is not None or sess.expires_at <= now:
        return None
    if (now - sess.last_seen_at).total_seconds() > cfg.auth_session_idle_minutes * 60:
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.status != "active" or user.session_epoch != sess.epoch:
        return None
    org = db.get(Organization, user.organization_id)
    if org is None or org.status != "active":
        return None
    if (now - sess.last_seen_at).total_seconds() > 30:  # bound the write rate
        # Best effort: under concurrent writers SQLite can refuse this upgrade from read to write at once. Skipping one
        # touch is harmless (the idle window is minutes, the next request retries); failing the request is not.
        try:
            with db.begin_nested():
                sess.last_seen_at = now
        except OperationalError:
            pass
    return actor_of(user, sess)


def revoke_session(db: Session, session_id: int, reason: str, now: datetime | None = None) -> None:
    sess = db.get(AuthSession, session_id)
    if sess and sess.revoked_at is None:
        sess.revoked_at, sess.revoked_reason = now or utcnow(), reason
        db.flush()


def revoke_user_sessions(
    db: Session, user: User, reason: str, except_session: int | None = None, now: datetime | None = None
) -> int:
    now = now or utcnow()
    q = update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
    if except_session is not None:
        q = q.where(AuthSession.id != except_session)
    res = db.execute(q.values(revoked_at=now, revoked_reason=reason))
    db.flush()
    return int(res.rowcount or 0)


# ------------------------------------------------------------------ passwords


def change_password(db: Session, cfg: Settings, user: User, current: str, new: str, keep_session: int | None) -> int:
    if not passwords.verify_password(current, user.password_hash):
        raise ForbiddenError("The current password is not correct.")
    if current == new:
        raise DomainValidationError("The new password must differ from the current one.")
    passwords.validate_policy(new, username=user.username, min_length=cfg.auth_min_password_length)
    user.password_hash = passwords.hash_password(new, cfg.auth_scrypt_n, cfg.auth_scrypt_r, cfg.auth_scrypt_p)
    user.password_changed_at, user.must_change_password = utcnow(), False
    return revoke_user_sessions(db, user, "password_changed", except_session=keep_session)


def issue_reset(db: Session, cfg: Settings, user: User, issued_by: str, now: datetime | None = None) -> str:
    now = now or utcnow()
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            token_hash=digest(raw),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(minutes=cfg.auth_reset_ttl_minutes),
            issued_by=issued_by,
        )
    )
    db.flush()
    return raw


def confirm_reset(db: Session, cfg: Settings, raw: str, new: str, now: datetime | None = None) -> User:
    """Consume a reset token. Every failure (unknown, expired, used, disabled account) gives the same error."""
    now = now or utcnow()
    bad = DomainValidationError("The reset link is invalid or has expired.")
    tok = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == digest(raw)))
    if tok is None or tok.used_at is not None or tok.expires_at <= now:
        raise bad
    user = db.get(User, tok.user_id)
    if user is None or user.status != "active":
        raise bad
    passwords.validate_policy(new, username=user.username, min_length=cfg.auth_min_password_length)
    tok.used_at = now
    user.password_hash = passwords.hash_password(new, cfg.auth_scrypt_n, cfg.auth_scrypt_r, cfg.auth_scrypt_p)
    user.password_changed_at, user.must_change_password = now, False
    revoke_user_sessions(db, user, "password_reset", now=now)
    db.flush()
    return user


# ------------------------------------------------------------------ administration


def _admin_count(db: Session, org_id: int, exclude: int | None = None) -> int:
    q = (
        select(func.count())
        .select_from(User)
        .where(User.organization_id == org_id, User.role == "admin", User.status == "active")
    )
    if exclude is not None:
        q = q.where(User.id != exclude)
    return int(db.scalar(q) or 0)


def get_user(db: Session, actor: Actor, user_uid: str) -> User:
    """Organisation-scoped lookup: a user of another organisation is indistinguishable from a missing one."""
    user = db.scalar(select(User).where(User.user_uid == user_uid))
    if user is None or (user.organization_id != actor.org_id and not actor.platform_admin):
        raise NotFoundError("User not found.")
    return user


def create_user(db: Session, cfg: Settings, actor: Actor | None, *, username: str, password: str, role: str, org_id: int, display_name: str | None = None,
                platform_admin: bool = False, must_change: bool = False) -> User:  # fmt: skip
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise DomainValidationError("Username must be 3-64 characters: lowercase letters, digits, '.', '_' or '-'.")
    if role not in USER_ROLES:
        raise DomainValidationError(f"Role must be one of: {', '.join(USER_ROLES)}.")
    if actor is not None:
        if org_id != actor.org_id and not actor.platform_admin:
            raise NotFoundError("Organization not found.")  # never reveal or accept another organisation's id
        if platform_admin and not actor.platform_admin:
            raise ForbiddenError("Only a platform administrator can create a platform administrator.")
        if Role[role.upper()] > actor.role:
            raise ForbiddenError("You cannot grant a role higher than your own.")
    org = db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("Organization not found.")
    if db.scalar(select(User.id).where(User.username == username)):
        raise ConflictError("That username is already taken.")
    passwords.validate_policy(password, username=username, min_length=cfg.auth_min_password_length)
    user = User(
        user_uid=new_uid("US"), username=username, display_name=display_name, organization_id=org_id, role=role, platform_admin=platform_admin,
        password_hash=passwords.hash_password(password, cfg.auth_scrypt_n, cfg.auth_scrypt_r, cfg.auth_scrypt_p), must_change_password=must_change,
        created_by=actor.name if actor else "cli",
    )  # fmt: skip
    db.add(user)
    db.flush()
    return user


def update_user(
    db: Session,
    actor: Actor,
    user: User,
    *,
    role: str | None,
    status: str | None,
    display_name: str | None,
    platform_admin: bool | None,
) -> dict[str, Any]:
    """Apply an administrator's change. Returns what changed (safe to audit). Self-changes of role or status are refused."""
    changed: dict[str, Any] = {}
    is_self = user.id == actor.user_id or user.username == actor.name
    if role is not None and role != user.role:
        if is_self:
            raise ForbiddenError("You cannot change your own role.")
        if role not in USER_ROLES:
            raise DomainValidationError(f"Role must be one of: {', '.join(USER_ROLES)}.")
        if Role[role.upper()] > actor.role or role_of(user) > actor.role:
            raise ForbiddenError("You cannot grant or change a role higher than your own.")
        if user.role == "admin" and role != "admin" and _admin_count(db, user.organization_id, exclude=user.id) == 0:
            raise ConflictError("An organisation must keep at least one active administrator.")
        changed["role"] = {"from": user.role, "to": role}
        user.role = role
    if status is not None and status != user.status:
        if is_self:
            raise ForbiddenError("You cannot change your own account status.")
        if status not in ("active", "disabled", "suspended"):
            raise DomainValidationError("Status must be active, disabled or suspended.")
        if user.role == "admin" and status != "active" and _admin_count(db, user.organization_id, exclude=user.id) == 0:
            raise ConflictError("An organisation must keep at least one active administrator.")
        changed["status"] = {"from": user.status, "to": status}
        user.status = status
    if platform_admin is not None and platform_admin != user.platform_admin:
        if not actor.platform_admin:
            raise ForbiddenError("Only a platform administrator can change platform administration.")
        if is_self:
            raise ForbiddenError("You cannot change your own platform administration.")
        changed["platform_admin"] = {"from": user.platform_admin, "to": platform_admin}
        user.platform_admin = platform_admin
    if display_name is not None and display_name != user.display_name:
        user.display_name = display_name
        changed["display_name"] = True
    if "role" in changed or "status" in changed or "platform_admin" in changed:
        user.session_epoch += 1  # every existing session of this user stops working immediately
        changed["sessions_revoked"] = True
    db.flush()
    return changed


def create_org(db: Session, actor: Actor, slug: str, name: str) -> Organization:
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise DomainValidationError("Slug must be 2-48 characters: lowercase letters, digits and '-'.")
    if db.scalar(select(Organization.id).where(Organization.slug == slug)):
        raise ConflictError("That organisation slug is already taken.")
    org = Organization(org_uid=new_uid("OR"), slug=slug, name=name.strip(), created_by=actor.name)
    db.add(org)
    db.flush()
    return org


def set_org_status(db: Session, actor: Actor, org: Organization, status: str) -> None:
    if org.id == DEFAULT_ORG_ID and status != "active":
        raise ConflictError("The default organisation cannot be suspended.")
    if status not in ("active", "suspended"):
        raise DomainValidationError("Status must be active or suspended.")
    org.status = status
    db.flush()
