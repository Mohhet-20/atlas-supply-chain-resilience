"""Integration registry: connector catalogue, integrations, health, synchronisation runs."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from app.api.v1.alerting import build_ctx
from app.api.v1.intcommon import ERR, Admin, Db, Lim, Off, Reader, Uid, iso, log, page, writes
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.integrations import connectors as C
from app.integrations import health, mapping, secrets, sync
from app.integrations.webhooks import hosts_of
from app.models.integrations import Integration, IntegrationRun, SyncError
from app.models.types import utcnow

router = APIRouter(prefix="/integrations", tags=["integrations"])
logger = logging.getLogger("atlas.integrations")


def _info(i: C.ConnectorInfo) -> dict[str, Any]:
    return {"key": i.key, "name": i.name, "category": i.category, "provider": i.provider, "description": i.description, "status": i.status, "version": i.version, "capabilities": list(i.capabilities), "needs_credential": i.needs_credential, "config_fields": i.config_fields, "creatable": i.status != "extension_point"}  # fmt: skip


def _row(db: Db, i: Integration, with_health: bool = True) -> dict[str, Any]:
    d = {
        "integration_uid": i.integration_uid, "name": i.name, "category": i.category, "provider": i.provider, "description": i.description, "config": i.config or {}, "mapping": i.mapping or {}, "target": i.target,
        "credential": {"reference": i.credential_ref, "status": secrets.credential_status(i.credential_ref)},  # the reference name and whether it resolves; never the secret
        "status": i.status, "enabled": i.enabled, "schedule_minutes": i.schedule_minutes, "next_run_at": iso(i.next_run_at), "last_success_at": iso(i.last_success_at), "last_failure_at": iso(i.last_failure_at),
        "error_summary": i.error_summary, "version": i.version, "created_at": iso(i.created_at), "updated_at": iso(i.updated_at),
    }  # fmt: skip
    if with_health:
        d["health"] = health.compute(db, i)
    return d


def _run(r: IntegrationRun) -> dict[str, Any]:
    return {
        "run_uid": r.run_uid, "integration_id": r.integration_id, "mode": r.mode, "trigger": r.trigger, "status": r.status, "started_at": iso(r.started_at), "finished_at": iso(r.finished_at), "duration_ms": r.duration_ms,
        "discovered": r.discovered, "processed": r.processed, "created": r.created, "updated": r.updated, "skipped": r.skipped, "rejected": r.rejected, "failed": r.failed, "retries": r.retries,
        "checkpoint": r.checkpoint or {}, "error_summary": r.error_summary, "trigger_by": r.created_by,
    }  # fmt: skip


def _get(db: Db, uid: str) -> Integration:
    i = db.scalar(select(Integration).where(Integration.integration_uid == uid))
    if i is None:
        raise NotFoundError(f"Integration {uid} was not found.")
    return i


@router.get("/connectors", responses=ERR, summary="Connector catalogue (implemented, sandbox and extension points)")
def connectors(actor: Reader) -> dict[str, Any]:
    return {"items": [_info(i) for i in C.catalogue()]}


@router.get("/overview", responses=ERR, summary="Counts, health, active syncs and recent failures")
def overview(db: Db, actor: Reader) -> dict[str, Any]:
    return health.overview(db)


@router.get("", responses=ERR, summary="Integrations of my organisation")
def list_integrations(
    db: Db, actor: Reader, status: str | None = Query(default=None, max_length=12), category: str | None = Query(default=None, max_length=24), provider: str | None = Query(default=None, max_length=48),
    q: str | None = Query(default=None, max_length=80), sort: Literal["name", "created", "status"] = "name", limit: Lim = 50, offset: Off = 0,
) -> dict[str, Any]:  # fmt: skip
    stmt = select(Integration)
    for col, val in ((Integration.status, status), (Integration.category, category), (Integration.provider, provider)):
        if val:
            stmt = stmt.where(col == val)
    if q:
        like = f"%{q.lower().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(func.lower(Integration.name).like(like), func.lower(Integration.description).like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = {"name": Integration.name, "created": Integration.id.desc(), "status": Integration.status}[sort]
    rows = db.scalars(stmt.order_by(order).limit(limit).offset(offset)).all()
    return page([_row(db, i) for i in rows], total, limit, offset)


class IntegrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=80)
    provider: str = Field(min_length=2, max_length=48)
    description: str | None = Field(default=None, max_length=300)
    config: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = Field(default=None, max_length=80)
    mapping: dict[str, Any] | None = None
    schedule_minutes: int | None = Field(default=None, ge=5, le=10_080)


def _validate(cfg: Any, i: Integration) -> list[str]:
    conn = C.get_connector(i.provider)
    errs = conn.validate_config(i.config or {}, hosts_of(cfg))
    errs += [f"mapping: {e}" for e in mapping.validate_spec(i.mapping or {})]
    if conn.info.needs_credential and not i.credential_ref:
        errs.append("credential_ref: required for this connector (env:ATLAS_CRED_NAME)")
    return errs


@router.post("", status_code=201, responses=ERR, summary="Create an integration (draft until validated)")
def create_integration(body: IntegrationBody, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    conn = C.get_connector(body.provider)  # raises for unknown keys and extension points
    if db.scalar(select(Integration.id).where(Integration.name == body.name)):
        raise ConflictError("An integration with that name already exists.")
    i = Integration(
        integration_uid="IG-" + __import__("uuid").uuid4().hex[:12].upper(), organization_id=actor.org_id, name=body.name, category=conn.info.category, provider=body.provider, description=body.description,
        config=body.config, credential_ref=secrets.validate_ref(body.credential_ref), mapping=body.mapping or (mapping.SANDBOX_MAPPING if body.provider == "sandbox_inventory" else {}),
        schedule_minutes=body.schedule_minutes, status="draft", enabled=False, created_by=actor.name,
    )  # fmt: skip
    db.add(i)
    db.flush()
    log(db, actor, request, "integration.created", "integration", i.integration_uid, {"provider": i.provider})
    return _row(db, i)


@router.get("/{uid}", responses=ERR, summary="One integration (credentials are never returned)")
def get_integration(uid: Uid, db: Db, actor: Reader) -> dict[str, Any]:
    return _row(db, _get(db, uid))


class IntegrationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    description: str | None = Field(default=None, max_length=300)
    config: dict[str, Any] | None = None
    credential_ref: str | None = Field(default=None, max_length=80)
    mapping: dict[str, Any] | None = None
    schedule_minutes: int | None = Field(default=None, ge=5, le=10_080)


@router.patch("/{uid}", responses=ERR, summary="Change configuration (back to draft until validated again)")
def patch_integration(uid: Uid, body: IntegrationPatch, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    if i.version != body.expected_version:
        raise ConflictError("The integration changed since you loaded it. Reload and retry.")
    sent = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    for k, v in sent.items():
        setattr(i, k, secrets.validate_ref(v) if k == "credential_ref" else v)
    if {"config", "credential_ref", "mapping"} & set(sent):
        i.status, i.enabled = "draft", False
    i.version += 1
    log(db, actor, request, "integration.updated", "integration", uid, {"fields": sorted(sent)})
    return _row(db, i)


@router.post("/{uid}/validate", responses=ERR, summary="Validate configuration and mapping (no network call)")
def validate_integration(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    errs = _validate(request.app.state.settings, i)
    if not errs and i.status == "draft":
        i.status = "configured"
    log(db, actor, request, "integration.validated", "integration", uid, {"errors": len(errs)})
    return {"valid": not errs, "errors": errs, "status": i.status}


def _test(request: Request, i: Integration) -> C.TestResult:
    cfg = request.app.state.settings
    conn = C.get_connector(i.provider)
    errs = _validate(cfg, i)
    if errs:
        return C.TestResult(False, "invalid configuration: " + "; ".join(errs[:3]))
    credential = secrets.resolve_ref(i.credential_ref)
    if conn.info.needs_credential and not credential:
        return C.TestResult(False, "the credential reference does not resolve in this environment")
    try:
        return conn.test_connection(i.config or {}, credential, hosts_of(cfg))
    except C.ConnectorError as e:
        return C.TestResult(False, e.message)


@router.post(
    "/{uid}/test", responses=ERR, summary="Test the connection (also marks the integration connected on success)"
)
def test_integration(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    res = _test(request, i)
    if res.ok:
        if i.status in ("draft", "configured", "failed"):
            i.status, i.error_summary = "connected", None
    else:
        i.error_summary, i.last_failure_at = res.detail[:300], utcnow()
        if i.status in ("connected", "healthy", "degraded"):
            i.status = "failed"
    log(db, actor, request, "integration.tested", "integration", uid, {"ok": res.ok})
    return {"ok": res.ok, "detail": res.detail, "latency_ms": res.latency_ms, "status": i.status}


@router.post("/{uid}/disconnect", responses=ERR, summary="Disconnect and stop scheduling")
def disconnect(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    i.status, i.enabled, i.next_run_at = "configured", False, None
    log(db, actor, request, "integration.disconnected", "integration", uid)
    return _row(db, i)


@router.post("/{uid}/enable", responses=ERR, summary="Enable synchronisation (needs a successful connection test)")
def enable(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    if i.status not in ("connected", "healthy", "degraded", "failed", "disabled") or (
        i.status in ("failed", "disabled") and not i.last_success_at and i.status != "disabled"
    ):
        raise ConflictError("Test the connection successfully before enabling.")
    if i.status == "disabled":
        i.status = "connected"
    i.enabled = True
    i.next_run_at = utcnow() if i.schedule_minutes else None
    log(db, actor, request, "integration.enabled", "integration", uid)
    return _row(db, i)


@router.post("/{uid}/disable", responses=ERR, summary="Disable synchronisation")
def disable(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    i.enabled, i.status, i.next_run_at = False, "disabled", None
    log(db, actor, request, "integration.disabled", "integration", uid)
    return _row(db, i)


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["full", "incremental"] = "incremental"
    resume: bool = False


def _background(app: Any, org_id: int, run_uid: str) -> None:
    try:
        sync.execute_run(app.state.session_factory, app.state.settings, org_id, run_uid, build_ctx(app, org_id=org_id))
    except Exception:
        logger.exception("synchronisation %s failed unexpectedly", run_uid)


@router.post("/{uid}/sync", status_code=202, responses=ERR, summary="Start a synchronisation (runs in the background)")
def start_sync(
    uid: Uid, body: SyncBody, request: Request, tasks: BackgroundTasks, db: Db, actor: Admin
) -> dict[str, Any]:
    writes(request)
    i = _get(db, uid)
    run = sync.start_run(db, i, body.mode, "manual", actor.name, resume=body.resume)
    log(db, actor, request, "integration.sync.started", "integration", uid, {"run": run.run_uid, "mode": body.mode})
    db.commit()  # the run row must exist before the background task looks for it
    tasks.add_task(_background, request.app, actor.org_id, run.run_uid)
    return _run(run)


@router.get("/{uid}/runs", responses=ERR, summary="Synchronisation history")
def list_runs(
    uid: Uid,
    db: Db,
    actor: Reader,
    status: str | None = Query(default=None, max_length=10),
    limit: Lim = 25,
    offset: Off = 0,
) -> dict[str, Any]:
    i = _get(db, uid)
    stmt = select(IntegrationRun).where(IntegrationRun.integration_id == i.id)
    if status:
        stmt = stmt.where(IntegrationRun.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(IntegrationRun.id.desc()).limit(limit).offset(offset)).all()
    return page([_run(r) for r in rows], total, limit, offset)


@router.get("/runs/{run_uid}", responses=ERR, summary="One run with its recorded record-level errors")
def get_run(run_uid: Uid, db: Db, actor: Reader) -> dict[str, Any]:
    r = db.scalar(select(IntegrationRun).where(IntegrationRun.run_uid == run_uid))
    if r is None:
        raise NotFoundError(f"Run {run_uid} was not found.")
    errs = db.scalars(select(SyncError).where(SyncError.run_id == r.id).order_by(SyncError.id).limit(100)).all()
    return {
        **_run(r),
        "errors": [
            {"record_ref": e.record_ref, "code": e.code, "message": e.message, "at": iso(e.created_at)} for e in errs
        ],
    }


@router.post("/runs/{run_uid}/cancel", responses=ERR, summary="Request cancellation (honoured between pages)")
def cancel_run(run_uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    r = db.scalar(select(IntegrationRun).where(IntegrationRun.run_uid == run_uid))
    if r is None:
        raise NotFoundError(f"Run {run_uid} was not found.")
    if r.status not in ("queued", "running"):
        raise ConflictError(f"The run is already {r.status}.")
    r.cancel_requested = True
    if r.status == "queued":
        r.status, r.finished_at = "cancelled", utcnow()
    log(db, actor, request, "integration.sync.cancel_requested", "integration_run", run_uid)
    return _run(r)


@router.get("/{uid}/health", responses=ERR, summary="Derived health with its reasons")
def integration_health(uid: Uid, db: Db, actor: Reader) -> dict[str, Any]:
    return health.compute(db, _get(db, uid))


@router.delete(
    "/{uid}",
    status_code=204,
    responses=ERR,
    summary="Delete a draft, configured or disabled integration with no active run",
)
def delete_integration(uid: Uid, request: Request, db: Db, actor: Admin):  # noqa: ANN201
    from fastapi import Response

    writes(request)
    i = _get(db, uid)
    if i.enabled or i.status in ("syncing", "connected", "healthy", "degraded"):
        raise ConflictError("Disable or disconnect the integration before deleting it.")
    if db.scalar(
        select(IntegrationRun.id).where(
            IntegrationRun.integration_id == i.id, IntegrationRun.status.in_(("queued", "running"))
        )
    ):
        raise ConflictError("A synchronisation is in progress.")
    if i.status == "failed" and False:
        raise DomainValidationError("unreachable")
    log(db, actor, request, "integration.deleted", "integration", uid)
    db.query(SyncError).filter(
        SyncError.run_id.in_(select(IntegrationRun.id).where(IntegrationRun.integration_id == i.id))
    ).delete(synchronize_session=False)
    db.query(IntegrationRun).filter(IntegrationRun.integration_id == i.id).delete(synchronize_session=False)
    db.delete(i)
    return Response(status_code=204)
