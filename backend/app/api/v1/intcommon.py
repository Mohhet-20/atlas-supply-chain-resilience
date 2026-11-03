"""Shared plumbing for the integrations routers: one tenant-scoped transaction per request, paging, write gate, audit helper."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.v1.alerting import _tx
from app.core.auth import Actor
from app.core.errors import WriteDisabledError
from app.core.permissions import Permission, require_permission
from app.models.enums import AuditOutcome
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
Uid = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Lim = Annotated[int, Query(ge=1, le=100)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
Reader = Annotated[Actor, Depends(require_permission(Permission.INTEGRATION_READ))]
Admin = Annotated[Actor, Depends(require_permission(Permission.INTEGRATION_ADMIN))]
Exporter = Annotated[Actor, Depends(require_permission(Permission.EXPORT))]
AnyReader = Annotated[Actor, Depends(require_permission(Permission.DATA_READ))]


Db = Annotated[Session, Depends(_tx)]


def writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Integration write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def page(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def log(
    db: Session,
    actor: Actor,
    request: Request,
    action: str,
    target_type: str,
    target_id: str | None,
    details: dict[str, Any] | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
) -> None:
    audit.record(db, actor, action, target_type=target_type, target_id=target_id, details=details, outcome=outcome, request_id=request_id_of(request))  # fmt: skip
