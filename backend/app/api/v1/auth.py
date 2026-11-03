"""Authentication endpoints: login, logout, who am I, password change and one-time password reset.

Login and reset endpoints need no credentials, so they are rate limited per client address and, for login, throttled per
(username, client). Every outcome is audited; a failed login never records the submitted username (it may be a mistyped
password) - only whether the account existed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import service as svc
from app.core.auth import Actor, CurrentActor, token_table
from app.core.errors import ForbiddenError, TooManyRequestsError, UnauthenticatedError
from app.core.permissions import permissions_of
from app.core.ratelimit import auth_rate_limit, client_of
from app.models.enums import AuditOutcome
from app.models.identity import AuthSession, Organization, User
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}}
router = APIRouter(prefix="/auth", tags=["authentication"])
SYSTEM = Actor("anonymous", svc.Role.VIEWER, False)


def _tx(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


Db = Annotated[Session, Depends(_tx)]


def _commit_audit(
    request: Request,
    actor: Actor,
    action: str,
    outcome: AuditOutcome,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    org: int | None = None,
) -> None:
    """Audit in its own transaction so a failed login (which raises) still leaves a record."""
    with request.app.state.session_factory() as db, db.begin():
        audit.record(
            db,
            actor,
            action,
            target_type="user" if target else None,
            target_id=target,
            outcome=outcome,
            details=details,
            request_id=request_id_of(request),
            organization_id=org,
        )


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _profile(user: User, org: Organization | None) -> dict[str, Any]:
    return {
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "platform_admin": bool(user.platform_admin),
        "organization": {"uid": org.org_uid, "slug": org.slug, "name": org.name} if org else None,
        "must_change_password": user.must_change_password,
    }


@router.post(
    "/login", dependencies=[Depends(auth_rate_limit)], responses=ERR, summary="Sign in with a username and password"
)
def login(body: LoginBody, request: Request, db: Db) -> dict[str, Any]:
    cfg = request.app.state.settings
    client = client_of(request)
    try:
        raw, user, sess = svc.login(db, cfg, body.username, body.password, client)
    except svc.LoginFailed as e:
        db.commit()  # keep the throttle counter even though the request fails
        details = {"reason": e.reason}
        _commit_audit(
            request,
            SYSTEM if e.user is None else svc.actor_of(e.user),
            "auth.login.failed",
            AuditOutcome.FAILED,
            e.user.user_uid if e.user else None,
            details,
            e.user.organization_id if e.user else None,
        )
        raise
    except TooManyRequestsError:
        db.rollback()  # release the read lock so the audit write below cannot block on it
        _commit_audit(request, SYSTEM, "auth.login.throttled", AuditOutcome.DENIED)
        raise
    org = db.get(Organization, user.organization_id)
    actor = svc.actor_of(user, sess)
    audit.record(
        db,
        actor,
        "auth.login.succeeded",
        target_type="user",
        target_id=user.user_uid,
        details={"session_id": sess.id},
        request_id=request_id_of(request),
    )
    return {
        "token": raw,
        "token_type": "bearer",
        "expires_at": sess.expires_at.isoformat(),
        "idle_timeout_minutes": cfg.auth_session_idle_minutes,
        "user": _profile(user, org),
    }


@router.post("/logout", status_code=204, responses=ERR, summary="End the current session")
def logout(request: Request, db: Db, actor: CurrentActor) -> Response:
    if actor.session_id is not None:
        svc.revoke_session(db, actor.session_id, "logout")
        audit.record(
            db,
            actor,
            "auth.logout",
            target_type="user",
            details={"session_id": actor.session_id},
            request_id=request_id_of(request),
        )
    return Response(status_code=204)


@router.post("/logout-all", status_code=204, responses=ERR, summary="End every session of the signed-in user")
def logout_all(request: Request, db: Db, actor: CurrentActor) -> Response:
    if actor.user_id is None:
        raise ForbiddenError("Service tokens have no user sessions.")
    user = db.get(User, actor.user_id)
    n = svc.revoke_user_sessions(db, user, "logout_all") if user else 0
    audit.record(
        db,
        actor,
        "auth.session.revoked_all",
        target_type="user",
        details={"count": n},
        request_id=request_id_of(request),
    )
    return Response(status_code=204)


@router.get("/me", responses=ERR, summary="Who am I, in which organisation, with which permissions")
def me(request: Request, db: Db, actor: CurrentActor) -> dict[str, Any]:
    org = db.get(Organization, actor.org_id)
    user = db.get(User, actor.user_id) if actor.user_id else None
    return {
        "actor": actor.name, "role": actor.role.name.lower(), "authenticated": actor.authenticated, "kind": "user" if user else "service",
        "platform_admin": actor.platform_admin, "organization": {"uid": org.org_uid, "slug": org.slug, "name": org.name} if org else None,
        "permissions": sorted(p.value for p in permissions_of(actor)), "must_change_password": bool(user and user.must_change_password),
        "auth_required": bool(token_table(request)) or user is not None, "writes_enabled": request.app.state.settings.write_api_allowed,
    }  # fmt: skip


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


@router.post("/password", status_code=204, responses=ERR, summary="Change my password (other sessions are ended)")
def change_password(body: PasswordChange, request: Request, db: Db, actor: CurrentActor) -> Response:
    if actor.user_id is None:
        raise ForbiddenError("Service tokens have no password.")
    user = db.get(User, actor.user_id)
    if user is None:
        raise UnauthenticatedError("The session is not valid.")
    try:
        ended = svc.change_password(
            db, request.app.state.settings, user, body.current_password, body.new_password, actor.session_id
        )
    except ForbiddenError:
        db.commit()
        _commit_audit(request, actor, "auth.password.change_failed", AuditOutcome.FAILED, user.user_uid)
        raise
    audit.record(
        db,
        actor,
        "auth.password.changed",
        target_type="user",
        target_id=user.user_uid,
        details={"other_sessions_ended": ended},
        request_id=request_id_of(request),
    )
    return Response(status_code=204)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)


@router.post(
    "/password-reset/request",
    status_code=202,
    dependencies=[Depends(auth_rate_limit)],
    responses=ERR,
    summary="Ask for a password reset (always answers the same way)",
)
def reset_request(body: ResetRequest, request: Request, db: Db) -> dict[str, str]:
    """Same response whether or not the account exists. ATLAS has no email transport, so a self-service request cannot deliver a
    token: it is recorded for an administrator, who can issue a one-time token out of band (POST /admin/users/{uid}/password-reset)."""
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if user is not None and user.status == "active":
        audit.record(
            db,
            svc.actor_of(user),
            "auth.password_reset.requested",
            target_type="user",
            target_id=user.user_uid,
            details={"delivered": False},
            request_id=request_id_of(request),
        )
    return {
        "status": "accepted",
        "message": "If the account exists, an administrator has been asked to issue a reset. No email is sent by ATLAS.",
    }


class ResetConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=128)
    new_password: str = Field(min_length=1, max_length=256)


@router.post(
    "/password-reset/confirm",
    status_code=204,
    dependencies=[Depends(auth_rate_limit)],
    responses=ERR,
    summary="Set a new password with a one-time token",
)
def reset_confirm(body: ResetConfirm, request: Request, db: Db) -> Response:
    try:
        user = svc.confirm_reset(db, request.app.state.settings, body.token, body.new_password)
    except Exception:
        db.commit()
        _commit_audit(request, SYSTEM, "auth.password_reset.failed", AuditOutcome.FAILED)
        raise
    audit.record(
        db,
        svc.actor_of(user),
        "auth.password_reset.completed",
        target_type="user",
        target_id=user.user_uid,
        request_id=request_id_of(request),
    )
    return Response(status_code=204)


@router.get("/sessions", responses=ERR, summary="My active sessions")
def sessions(db: Db, actor: CurrentActor) -> dict[str, Any]:
    if actor.user_id is None:
        return {"items": []}
    rows = db.scalars(
        select(AuthSession)
        .where(AuthSession.user_id == actor.user_id, AuthSession.revoked_at.is_(None))
        .order_by(AuthSession.id.desc())
        .limit(50)
    ).all()
    return {
        "items": [
            {
                "id": s.id,
                "created_at": s.created_at.isoformat(),
                "last_seen_at": s.last_seen_at.isoformat(),
                "expires_at": s.expires_at.isoformat(),
                "current": s.id == actor.session_id,
            }
            for s in rows
        ]
    }
