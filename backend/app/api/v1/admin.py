"""Administration: users and organisations of the caller's organisation, and a security summary.

Every lookup is organisation-scoped by the authenticated actor. A user or organisation outside the caller's scope is
reported as not found. Role and status changes end the affected user's sessions at once (session epoch).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import service as svc
from app.core.auth import Actor
from app.core.errors import ConflictError, NotFoundError, WriteDisabledError
from app.core.permissions import Permission, require_permission
from app.models.enums import AuditOutcome
from app.models.identity import Organization, User
from app.models.registry import AuditEvent
from app.models.types import utcnow
from app.registry import audit
from app.registry.audit import audit_scope, request_id_of
from app.schemas.common import ErrorResponse

ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
router = APIRouter(prefix="/admin", tags=["administration"])
Manager = Annotated[Actor, Depends(require_permission(Permission.USER_MANAGE))]
OrgAdmin = Annotated[Actor, Depends(require_permission(Permission.ORG_MANAGE))]
Auditor = Annotated[Actor, Depends(require_permission(Permission.AUDIT_READ))]


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


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Administration write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _user(u: User) -> dict[str, Any]:
    return {
        "user_uid": u.user_uid,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "platform_admin": bool(u.platform_admin),
        "must_change_password": u.must_change_password,
        "organization_id": u.organization_id,
        "created_at": _iso(u.created_at),
        "last_login_at": _iso(u.last_login_at),
        "created_by": u.created_by,
    }


def _org(o: Organization) -> dict[str, Any]:
    return {"org_uid": o.org_uid, "slug": o.slug, "name": o.name, "status": o.status, "created_at": _iso(o.created_at)}


def _org_by_slug(db: Session, actor: Actor, slug: str | None) -> int:
    """The organisation an administrator acts on: their own, unless a platform administrator names another by slug."""
    if slug is None:
        return actor.org_id
    org = db.scalar(select(Organization).where(Organization.slug == slug))
    if org is None or (org.id != actor.org_id and not actor.platform_admin):
        raise NotFoundError("Organization not found.")
    return org.id


# ----------------------------------------------------------------- users


@router.get("/users", responses=ERR, summary="Users of my organisation")
def list_users(
    db: Db,
    actor: Manager,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    organization: Annotated[str | None, Query(max_length=48)] = None,
) -> dict[str, Any]:
    org_id = _org_by_slug(db, actor, organization)
    q = select(User).where(User.organization_id == org_id)
    total = int(db.scalar(select(func.count()).select_from(q.subquery())) or 0)
    rows = db.scalars(q.order_by(User.id).limit(limit).offset(offset)).all()
    return {"items": [_user(u) for u in rows], "total": total, "limit": limit, "offset": offset}


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: Literal["viewer", "editor", "reviewer", "admin"]
    display_name: str | None = Field(default=None, max_length=120)
    organization: str | None = Field(default=None, max_length=48)
    must_change_password: bool = True
    platform_admin: bool = False


@router.post("/users", status_code=201, responses=ERR, summary="Create a user")
def create_user(body: UserCreate, request: Request, db: Db, actor: Manager) -> dict[str, Any]:
    _writes(request)
    org_id = _org_by_slug(db, actor, body.organization)
    u = svc.create_user(
        db,
        request.app.state.settings,
        actor,
        username=body.username,
        password=body.password,
        role=body.role,
        org_id=org_id,
        display_name=body.display_name,
        platform_admin=body.platform_admin,
        must_change=body.must_change_password,
    )
    audit.record(
        db,
        actor,
        "admin.user.created",
        target_type="user",
        target_id=u.user_uid,
        details={"role": u.role, "organization_id": u.organization_id},
        request_id=request_id_of(request),
        organization_id=u.organization_id,
    )
    return _user(u)


@router.get("/users/{user_uid}", responses=ERR, summary="One user")
def get_user(user_uid: str, db: Db, actor: Manager) -> dict[str, Any]:
    return _user(svc.get_user(db, actor, user_uid))


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["viewer", "editor", "reviewer", "admin"] | None = None
    status: Literal["active", "disabled", "suspended"] | None = None
    display_name: str | None = Field(default=None, max_length=120)
    platform_admin: bool | None = None


@router.patch(
    "/users/{user_uid}", responses=ERR, summary="Change a user's role, status or name (ends that user's sessions)"
)
def patch_user(user_uid: str, body: UserPatch, request: Request, db: Db, actor: Manager) -> dict[str, Any]:
    _writes(request)
    u = svc.get_user(db, actor, user_uid)
    try:
        changed = svc.update_user(
            db,
            actor,
            u,
            role=body.role,
            status=body.status,
            display_name=body.display_name,
            platform_admin=body.platform_admin,
        )
    except Exception:
        db.rollback()
        with request.app.state.session_factory() as s2, s2.begin():
            audit.record(
                s2,
                actor,
                "admin.user.change_refused",
                target_type="user",
                target_id=user_uid,  # not u.user_uid: after rollback that would re-open a read lock on db
                outcome=AuditOutcome.DENIED,
                details={"requested": body.model_dump(exclude_none=True)},
                request_id=request_id_of(request),
            )
        raise
    if changed:
        action = (
            "admin.user.role_changed"
            if "role" in changed
            else "admin.user.status_changed"
            if "status" in changed
            else "admin.user.updated"
        )
        audit.record(
            db,
            actor,
            action,
            target_type="user",
            target_id=u.user_uid,
            details=changed,
            request_id=request_id_of(request),
            organization_id=u.organization_id,
        )
    return _user(u)


@router.post("/users/{user_uid}/revoke-sessions", responses=ERR, summary="End every session of a user")
def revoke_sessions(user_uid: str, request: Request, db: Db, actor: Manager) -> dict[str, int]:
    _writes(request)
    u = svc.get_user(db, actor, user_uid)
    n = svc.revoke_user_sessions(db, u, "admin_revoked")
    audit.record(
        db,
        actor,
        "admin.user.sessions_revoked",
        target_type="user",
        target_id=u.user_uid,
        details={"count": n},
        request_id=request_id_of(request),
        organization_id=u.organization_id,
    )
    return {"revoked": n}


@router.post(
    "/users/{user_uid}/password-reset",
    responses=ERR,
    summary="Issue a one-time reset token (shown once; deliver it out of band)",
)
def issue_reset(user_uid: str, request: Request, db: Db, actor: Manager) -> dict[str, Any]:
    _writes(request)
    u = svc.get_user(db, actor, user_uid)
    if u.status != "active":
        raise ConflictError("Only active accounts can be reset.")
    cfg = request.app.state.settings
    raw = svc.issue_reset(db, cfg, u, actor.name)
    audit.record(
        db,
        actor,
        "admin.user.password_reset_issued",
        target_type="user",
        target_id=u.user_uid,
        details={"ttl_minutes": cfg.auth_reset_ttl_minutes},
        request_id=request_id_of(request),
        organization_id=u.organization_id,
    )
    return {
        "token": raw,
        "expires_in_minutes": cfg.auth_reset_ttl_minutes,
        "note": "Shown once. It is stored only as a hash and works once.",
    }


# ----------------------------------------------------------------- organisations


@router.get(
    "/organizations", responses=ERR, summary="Organisations (platform administrators see all; others see their own)"
)
def list_orgs(db: Db, actor: Manager) -> dict[str, Any]:
    q = select(Organization).order_by(Organization.id)
    if not actor.platform_admin:
        q = q.where(Organization.id == actor.org_id)
    return {"items": [_org(o) for o in db.scalars(q)]}


class OrgCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=2, max_length=120)
    admin_username: str = Field(min_length=3, max_length=64)
    admin_password: str = Field(min_length=1, max_length=256)


@router.post(
    "/organizations",
    status_code=201,
    responses=ERR,
    summary="Create an organisation with its first administrator (platform administrators)",
)
def create_org(body: OrgCreate, request: Request, db: Db, actor: OrgAdmin) -> dict[str, Any]:
    _writes(request)
    org = svc.create_org(db, actor, body.slug, body.name)
    admin = svc.create_user(
        db,
        request.app.state.settings,
        actor,
        username=body.admin_username,
        password=body.admin_password,
        role="admin",
        org_id=org.id,
        must_change=True,
    )
    audit.record(
        db,
        actor,
        "admin.organization.created",
        target_type="organization",
        target_id=org.org_uid,
        details={"slug": org.slug, "first_admin": admin.user_uid},
        request_id=request_id_of(request),
        organization_id=org.id,
    )
    return {**_org(org), "first_admin": _user(admin)}


class OrgPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "suspended"]


@router.patch(
    "/organizations/{org_uid}", responses=ERR, summary="Suspend or reactivate an organisation (platform administrators)"
)
def patch_org(org_uid: str, body: OrgPatch, request: Request, db: Db, actor: OrgAdmin) -> dict[str, Any]:
    _writes(request)
    org = db.scalar(select(Organization).where(Organization.org_uid == org_uid))
    if org is None:
        raise NotFoundError("Organization not found.")
    svc.set_org_status(db, actor, org, body.status)
    audit.record(
        db,
        actor,
        "admin.organization.status_changed",
        target_type="organization",
        target_id=org.org_uid,
        details={"status": org.status},
        request_id=request_id_of(request),
        organization_id=org.id,
    )
    return _org(org)


# ----------------------------------------------------------------- security summary

WATCHED = (
    "auth.login.failed",
    "auth.login.throttled",
    "authz.denied",
    "security.cross_tenant_access",
    "auth.password_reset.requested",
    "auth.password_reset.failed",
    "admin.user.role_changed",
    "admin.user.status_changed",
    "admin.user.change_refused",
    "export.created",
)


@router.get("/security/summary", responses=ERR, summary="Counts of security-relevant events in my organisation")
def security_summary(db: Db, actor: Auditor, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=hours)
    rows = db.execute(
        select(AuditEvent.action, func.count())
        .where(AuditEvent.occurred_at >= since, AuditEvent.action.in_(WATCHED), audit_scope(actor))
        .group_by(AuditEvent.action)
    ).all()
    counts = {a: int(c) for a, c in rows}
    return {
        "window_hours": hours,
        "counts": {a: counts.get(a, 0) for a in WATCHED},
        "note": "Counts come from the audit trail of this organisation. Zero means none were recorded, not that none occurred outside the audited paths.",
    }
