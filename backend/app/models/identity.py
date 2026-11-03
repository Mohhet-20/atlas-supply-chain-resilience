"""Identity storage: organisations, users, server-side sessions, one-time reset tokens and login throttling.

Secrets are never stored: passwords as salted scrypt hashes, session and reset tokens as SHA-256 digests of high-entropy
random values. The default organisation (id 1, slug ``default``) owns every record created before tenancy existed and
every static API-token actor.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime, utcnow

ORG_STATUSES = ("active", "suspended")
USER_STATUSES = ("active", "disabled", "suspended", "pending")
USER_ROLES = ("viewer", "editor", "reviewer", "admin")
DEFAULT_ORG_ID = 1


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_uid: Mapped[str] = mapped_column(String(40), unique=True)
    slug: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (CheckConstraint(f"status in {ORG_STATUSES}", name="ck_organizations_status"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_uid: Mapped[str] = mapped_column(String(40), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    role: Mapped[str] = mapped_column(String(10))
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="active")
    password_hash: Mapped[str] = mapped_column(String(300))
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    session_epoch: Mapped[int] = mapped_column(Integer, default=1)  # bumped to invalidate every session of the user
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64))
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(f"status in {USER_STATUSES}", name="ck_users_status"),
        CheckConstraint(f"role in {USER_ROLES}", name="ck_users_role"),
        Index("ix_users_org", "organization_id"),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(String(40))
    client_hash: Mapped[str | None] = mapped_column(
        String(16)
    )  # truncated hash of address + agent, never the raw values

    __table_args__ = (Index("ix_auth_sessions_user", "user_id"),)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    issued_by: Mapped[str | None] = mapped_column(String(64))  # an administrator, or "self-service"

    __table_args__ = (Index("ix_password_reset_user", "user_id"),)


class LoginThrottle(Base):
    """Failure counter per (username, client) pair. Keyed by a hash so no username or address is stored."""

    __tablename__ = "login_throttles"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    blocked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
