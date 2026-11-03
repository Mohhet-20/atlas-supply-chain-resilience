"""Service accounts and API keys, outbound/inbound webhooks, and export jobs."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.alerting import service as S
from app.alerting.config import EVENT_TYPES
from app.alerting.notify import UnsafeDestination
from app.api.v1.alerting import build_ctx
from app.api.v1.intcommon import ERR, Admin, Db, Exporter, Lim, Off, Reader, Uid, iso, log, page, writes
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.db.tenancy import scope_session
from app.integrations import apikeys, events, exports, secrets
from app.integrations import webhooks as W
from app.models.integrations import ExportJob, InboundEvent, ServiceAccount, WebhookDelivery, WebhookEndpoint
from app.models.types import utcnow

keys_router = APIRouter(prefix="/service-accounts", tags=["api-keys"])
hooks_router = APIRouter(prefix="/webhooks", tags=["webhooks"])
inbound_router = APIRouter(prefix="/webhooks/inbound", tags=["webhooks"])
exports_router = APIRouter(prefix="/exports", tags=["exports"])
logger = logging.getLogger("atlas.integrations")


# ── service accounts & API keys ────────────────────────────────────────────────
def _sa(sa: ServiceAccount) -> dict[str, Any]:
    return {"sa_uid": sa.sa_uid, "name": sa.name, "description": sa.description, "role": sa.role, "status": sa.status, "created_at": iso(sa.created_at), "created_by": sa.created_by}  # fmt: skip


def _key(k: Any) -> dict[str, Any]:
    return {
        "key_uid": k.key_uid, "name": k.name, "prefix": k.prefix, "role": k.role, "scopes": list(k.scopes or []), "expires_at": iso(k.expires_at), "revoked_at": iso(k.revoked_at),
        "created_at": iso(k.created_at), "last_used_at": iso(k.last_used_at), "use_count": k.use_count, "rotated_from": k.rotated_from,
    }  # fmt: skip


class SaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=64)
    description: str | None = Field(default=None, max_length=300)
    role: Literal["viewer", "editor", "reviewer"] = "viewer"


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=64)
    role: Literal["viewer", "editor", "reviewer"] | None = None
    scopes: list[str] = Field(default_factory=list, max_length=30)
    ttl_days: int | None = Field(default=None, ge=1, le=365)


@keys_router.get("", responses=ERR, summary="Service accounts")
def list_sas(db: Db, actor: Admin, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(ServiceAccount)) or 0
    rows = db.scalars(select(ServiceAccount).order_by(ServiceAccount.id).limit(limit).offset(offset)).all()
    return page([_sa(s) for s in rows], total, limit, offset)


@keys_router.post("", status_code=201, responses=ERR, summary="Create a service account (never an administrator)")
def create_sa(body: SaBody, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    sa = apikeys.create_sa(db, actor, body.name, body.description, body.role)
    log(db, actor, request, "service_account.created", "service_account", sa.sa_uid, {"role": sa.role})
    return _sa(sa)


@keys_router.post("/{uid}/disable", responses=ERR, summary="Disable a service account and revoke its keys")
def disable_sa(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    sa = apikeys.get_sa(db, uid)
    sa.status = "disabled"
    from app.models.integrations import ApiKey

    for k in db.scalars(select(ApiKey).where(ApiKey.service_account_id == sa.id)):
        apikeys.revoke(db, k)
    log(db, actor, request, "service_account.disabled", "service_account", uid)
    return _sa(sa)


@keys_router.get("/{uid}/keys", responses=ERR, summary="Keys of a service account (never the secret)")
def list_keys(uid: Uid, db: Db, actor: Admin) -> dict[str, Any]:
    from app.models.integrations import ApiKey

    sa = apikeys.get_sa(db, uid)
    return {
        "items": [
            _key(k)
            for k in db.scalars(select(ApiKey).where(ApiKey.service_account_id == sa.id).order_by(ApiKey.id.desc()))
        ]
    }


@keys_router.post(
    "/{uid}/keys",
    status_code=201,
    responses=ERR,
    summary="Create a key. The secret is returned once and cannot be shown again",
)
def create_key(uid: Uid, body: KeyBody, request: Request, db: Db, actor: Admin) -> JSONResponse:
    writes(request)
    sa = apikeys.get_sa(db, uid)
    raw, key = apikeys.create_key(db, actor, sa, body.name, body.role, body.scopes, body.ttl_days)
    log(
        db,
        actor,
        request,
        "api_key.created",
        "api_key",
        key.key_uid,
        {"service_account": uid, "role": key.role, "scopes": len(key.scopes or [])},
    )
    return JSONResponse({**_key(key), "secret": raw}, status_code=201, headers={"Cache-Control": "no-store"})


@keys_router.post("/keys/{key_uid}/revoke", responses=ERR, summary="Revoke a key immediately")
def revoke_key(key_uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    k = apikeys.get_key(db, key_uid)
    apikeys.revoke(db, k)
    log(db, actor, request, "api_key.revoked", "api_key", key_uid)
    return _key(k)


@keys_router.post(
    "/keys/{key_uid}/rotate", responses=ERR, summary="Issue a replacement; the old key works for a 24 hour grace period"
)
def rotate_key(key_uid: Uid, request: Request, db: Db, actor: Admin) -> JSONResponse:
    writes(request)
    raw, new = apikeys.rotate(db, actor, apikeys.get_key(db, key_uid))
    log(db, actor, request, "api_key.rotated", "api_key", key_uid, {"replacement": new.key_uid})
    return JSONResponse({**_key(new), "secret": raw}, status_code=201, headers={"Cache-Control": "no-store"})


# ── webhook endpoints ──────────────────────────────────────────────────────────
def _ep(ep: WebhookEndpoint) -> dict[str, Any]:
    return {
        "endpoint_uid": ep.endpoint_uid, "name": ep.name, "direction": ep.direction, "url": ep.url, "events": list(ep.events or []), "secret_version": ep.secret_version, "status": ep.status,
        "consecutive_failures": ep.consecutive_failures, "disabled_reason": ep.disabled_reason, "created_at": iso(ep.created_at),
        "receive_url": f"/api/v1/webhooks/inbound/{ep.endpoint_uid}" if ep.direction == "inbound" else None,
    }  # fmt: skip


def _delivery(d: WebhookDelivery) -> dict[str, Any]:
    return {"delivery_uid": d.delivery_uid, "event_id": d.event_id, "event_type": d.event_type, "status": d.status, "attempts": d.attempts, "next_attempt_at": iso(d.next_attempt_at), "last_status_code": d.last_status_code, "last_error": d.last_error, "created_at": iso(d.created_at), "delivered_at": iso(d.delivered_at)}  # fmt: skip


def _endpoint(db: Db, uid: str) -> WebhookEndpoint:
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.endpoint_uid == uid))
    if ep is None:
        raise NotFoundError("Webhook endpoint not found.")
    return ep


class HookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=64)
    direction: Literal["outbound", "inbound"] = "outbound"
    url: str | None = Field(default=None, max_length=500)
    events: list[str] = Field(default_factory=list, max_length=40)


def _secret_for(request: Request, ep: WebhookEndpoint) -> str:
    return secrets.derive_secret(
        request.app.state.settings, "out" if ep.direction == "outbound" else "in", ep.endpoint_uid, ep.secret_version
    )


@hooks_router.get("/events", responses=ERR, summary="Event catalogue with versions and whether each is emitted today")
def event_catalogue(actor: Reader) -> dict[str, Any]:
    return {"items": events.catalogue()}


@hooks_router.get("", responses=ERR, summary="Webhook endpoints")
def list_endpoints(
    db: Db, actor: Reader, direction: Literal["outbound", "inbound"] | None = None, limit: Lim = 50, offset: Off = 0
) -> dict[str, Any]:
    stmt = select(WebhookEndpoint)
    if direction:
        stmt = stmt.where(WebhookEndpoint.direction == direction)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(WebhookEndpoint.id).limit(limit).offset(offset)).all()
    return page([_ep(e) for e in rows], total, limit, offset)


@hooks_router.post(
    "", status_code=201, responses=ERR, summary="Create an endpoint. The signing secret is returned once"
)
def create_endpoint(body: HookBody, request: Request, db: Db, actor: Admin) -> JSONResponse:
    writes(request)
    cfg = request.app.state.settings
    if body.direction == "outbound":
        if not body.url:
            raise DomainValidationError("url is required for an outbound endpoint.")
        try:
            W.validate_destination(cfg, body.url, resolve=getattr(request.app.state, "webhook_resolve", True))
        except UnsafeDestination as e:
            raise DomainValidationError(f"Destination refused: {e}") from e
        unknown = sorted(set(body.events) - set(events.CATALOGUE) - {"*"})
        if unknown:
            raise DomainValidationError(f"Unknown event types: {', '.join(unknown)}")
        if not body.events:
            raise DomainValidationError("Subscribe to at least one event type (or *).")
    elif body.url or body.events:
        raise DomainValidationError("Inbound endpoints take neither a url nor event subscriptions.")
    ep = WebhookEndpoint(endpoint_uid="WH-" + uuid.uuid4().hex[:12].upper(), organization_id=actor.org_id, name=body.name, direction=body.direction, url=body.url, events=body.events, created_by=actor.name)  # fmt: skip
    db.add(ep)
    db.flush()
    secret = _secret_for(request, ep)  # raises 503 when the master key is not configured, rolling the transaction back
    log(
        db,
        actor,
        request,
        "webhook.created",
        "webhook",
        ep.endpoint_uid,
        {"direction": ep.direction, "events": len(body.events)},
    )
    return JSONResponse({**_ep(ep), "secret": secret}, status_code=201, headers={"Cache-Control": "no-store"})


@hooks_router.get("/{uid}", responses=ERR, summary="One endpoint (the secret is never returned again)")
def get_endpoint(uid: Uid, db: Db, actor: Reader) -> dict[str, Any]:
    return _ep(_endpoint(db, uid))


@hooks_router.post(
    "/{uid}/rotate-secret",
    responses=ERR,
    summary="New signing secret (returned once); the previous one stops working immediately",
)
def rotate_secret(uid: Uid, request: Request, db: Db, actor: Admin) -> JSONResponse:
    writes(request)
    ep = _endpoint(db, uid)
    ep.secret_version += 1
    secret = _secret_for(request, ep)
    log(db, actor, request, "webhook.secret_rotated", "webhook", uid, {"version": ep.secret_version})
    return JSONResponse({**_ep(ep), "secret": secret}, headers={"Cache-Control": "no-store"})


@hooks_router.post("/{uid}/enable", responses=ERR, summary="Re-enable an endpoint")
def enable_endpoint(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    ep = _endpoint(db, uid)
    ep.status, ep.disabled_reason, ep.consecutive_failures = "active", None, 0
    log(db, actor, request, "webhook.enabled", "webhook", uid)
    return _ep(ep)


@hooks_router.post("/{uid}/disable", responses=ERR, summary="Disable an endpoint")
def disable_endpoint(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    ep = _endpoint(db, uid)
    ep.status, ep.disabled_reason = "disabled", "disabled by an administrator"
    log(db, actor, request, "webhook.disabled", "webhook", uid)
    return _ep(ep)


@hooks_router.delete("/{uid}", status_code=204, responses=ERR, summary="Delete an endpoint and its delivery history")
def delete_endpoint(uid: Uid, request: Request, db: Db, actor: Admin):  # noqa: ANN201
    from fastapi import Response

    writes(request)
    ep = _endpoint(db, uid)
    log(db, actor, request, "webhook.deleted", "webhook", uid)
    db.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == ep.id).delete(synchronize_session=False)
    db.query(InboundEvent).filter(InboundEvent.endpoint_id == ep.id).delete(synchronize_session=False)
    db.delete(ep)
    return Response(status_code=204)


@hooks_router.post("/{uid}/test", status_code=202, responses=ERR, summary="Queue a webhook.test event to this endpoint")
def test_endpoint(uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    ep = _endpoint(db, uid)
    if ep.direction != "outbound" or ep.status != "active":
        raise ConflictError("Only an active outbound endpoint can receive a test event.")
    env = events.envelope("webhook.test", actor.org_id, {"endpoint_uid": ep.endpoint_uid, "message": "test delivery"})
    d = WebhookDelivery(delivery_uid="WD-" + uuid.uuid4().hex[:14].upper(), organization_id=actor.org_id, endpoint_id=ep.id, event_id=env["event_id"], event_type="webhook.test", envelope=env, status="queued", attempts=0, next_attempt_at=utcnow())  # fmt: skip
    db.add(d)
    db.flush()
    log(db, actor, request, "webhook.test_queued", "webhook", uid)
    return _delivery(d)


@hooks_router.get("/{uid}/deliveries", responses=ERR, summary="Delivery history")
def deliveries(
    uid: Uid,
    db: Db,
    actor: Reader,
    status: str | None = Query(default=None, max_length=10),
    limit: Lim = 25,
    offset: Off = 0,
) -> dict[str, Any]:
    ep = _endpoint(db, uid)
    stmt = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == ep.id)
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(WebhookDelivery.id.desc()).limit(limit).offset(offset)).all()
    return page([_delivery(d) for d in rows], total, limit, offset)


@hooks_router.post(
    "/deliveries/{delivery_uid}/redeliver",
    status_code=202,
    responses=ERR,
    summary="Retry a failed or dead delivery (same event id)",
)
def redeliver(delivery_uid: Uid, request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    d = db.scalar(select(WebhookDelivery).where(WebhookDelivery.delivery_uid == delivery_uid))
    if d is None:
        raise NotFoundError("Delivery not found.")
    if d.status not in ("failed", "dead"):
        raise ConflictError(f"Only failed or dead deliveries can be redelivered (this one is {d.status}).")
    W.redeliver(d)
    log(db, actor, request, "webhook.redelivery", "webhook_delivery", delivery_uid)
    return _delivery(d)


@hooks_router.post("/deliver-now", responses=ERR, summary="Run the delivery worker once for my organisation")
def deliver_now(request: Request, db: Db, actor: Admin) -> dict[str, Any]:
    writes(request)
    st = request.app.state
    result = W.deliver_due(
        db,
        st.settings,
        getattr(st, "webhook_sender", None) or W.HttpSender(),
        resolve=getattr(st, "webhook_resolve", True),
    )
    log(db, actor, request, "webhook.worker_run", "webhook", None, result)
    return {"result": result}


# ── inbound (public path, authenticated by signature) ───────────────────────────
class InboundBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)
    event_type: str
    occurred_at: Any
    payload: dict[str, Any] = Field(default_factory=dict)
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


_DENY = {"error": {"code": "unauthorized", "message": "The webhook could not be authenticated."}}


@inbound_router.post("/{uid}", status_code=202, summary="Receive a signed event from an external system")
async def receive(
    uid: Uid, request: Request, x_atlas_signature: str | None = Header(default=None, max_length=300)
) -> JSONResponse:
    st = request.app.state
    cfg = st.settings
    raw = await request.body()
    if len(raw) > 256 * 1024:
        return JSONResponse(
            {"error": {"code": "payload_too_large", "message": "The payload exceeds 256 KiB."}}, status_code=413
        )
    with st.session_factory() as db:
        ep = db.scalar(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.endpoint_uid == uid, WebhookEndpoint.direction == "inbound")
            .execution_options(skip_tenant=True)
        )
        if ep is None or ep.status != "active" or not x_atlas_signature:
            return JSONResponse(_DENY, status_code=401)  # unknown, disabled and unsigned look identical
        try:
            ok = secrets.verify(
                secrets.derive_secret(cfg, "in", ep.endpoint_uid, ep.secret_version),
                x_atlas_signature,
                raw,
                int(time.time()),
                cfg.webhook_tolerance_seconds,
            )
        except secrets.NotConfiguredError:
            return JSONResponse(_DENY, status_code=401)
        if not ok:
            return JSONResponse(_DENY, status_code=401)
        org_id, ep_id = ep.organization_id, ep.id
    try:
        data = InboundBody.model_validate(json.loads(raw))
        if data.event_type not in EVENT_TYPES:
            raise ValueError("unsupported event_type")
    except Exception as e:
        return JSONResponse(
            {"error": {"code": "invalid_event", "message": f"The event was rejected: {str(e)[:160]}"}}, status_code=422
        )
    with st.session_factory() as db:
        scope_session(db, org_id)
        inbox = InboundEvent(
            organization_id=org_id, endpoint_id=ep_id, provider_event_id=data.event_id, outcome="accepted"
        )
        db.add(inbox)
        try:
            db.flush()
        except Exception:  # unique (endpoint, event id): a replay or a redelivery
            db.rollback()
            return JSONResponse({"status": "duplicate_event"}, status_code=200)
        try:
            body = S.EventBody(
                event_type=data.event_type, source="wh-" + uid.lower(), source_record_id=data.event_id, idempotency_key=data.event_id, occurred_at=data.occurred_at, provenance="IMPORTED_EXTERNAL",
                payload=data.payload, entities=data.entities,
            )  # fmt: skip
            _ev, dup, _s = S.ingest_event(db, "webhook:" + uid, body, build_ctx(request.app, org_id=org_id))
            inbox.outcome = "duplicate_event" if dup else "accepted"
        except Exception as e:  # validation of the mapped event
            db.rollback()
            logger.info("inbound webhook %s rejected: %s", uid, type(e).__name__)
            return JSONResponse(
                {"error": {"code": "invalid_event", "message": "The event failed validation."}}, status_code=422
            )
        db.commit()
    return JSONResponse({"status": inbox.outcome}, status_code=202)


# ── exports ─────────────────────────────────────────────────────────────────────
def _job(j: ExportJob) -> dict[str, Any]:
    return {"export_uid": j.export_uid, "dataset": j.dataset, "format": j.format, "filters": j.filters or {}, "status": j.status, "row_count": j.row_count, "error": j.error, "created_at": iso(j.created_at), "finished_at": iso(j.finished_at), "expires_at": iso(j.expires_at), "created_by": j.created_by, "download_url": f"/api/v1/exports/{j.export_uid}/download" if j.status == "ready" else None}  # fmt: skip


class ExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str = Field(min_length=3, max_length=24)
    format: Literal["csv", "json"] = "csv"
    filters: dict[str, str] = Field(default_factory=dict, max_length=8)


def _run_export(app: Any, org_id: int, uid: str) -> None:
    try:
        exports.run_job(app.state.session_factory, app.state.settings, org_id, uid)
    except Exception:
        logger.exception("export %s failed unexpectedly", uid)


@exports_router.get("/datasets", responses=ERR, summary="Exportable datasets and their columns")
def datasets(actor: Exporter) -> dict[str, Any]:
    return {
        "items": [
            {"dataset": k, "columns": list(v[1]) if len(v) > 1 else [], "filters": list(v[2])}
            for k, v in exports.COLUMNS.items()
        ]
    }


@exports_router.get("", responses=ERR, summary="My organisation's export jobs")
def list_exports(db: Db, actor: Exporter, limit: Lim = 25, offset: Off = 0) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(ExportJob)) or 0
    rows = db.scalars(select(ExportJob).order_by(ExportJob.id.desc()).limit(limit).offset(offset)).all()
    return page([_job(j) for j in rows], total, limit, offset)


@exports_router.post("", status_code=202, responses=ERR, summary="Create an asynchronous export (files expire)")
def create_export(
    body: ExportBody, request: Request, tasks: BackgroundTasks, db: Db, actor: Exporter
) -> dict[str, Any]:
    writes(request)
    job = exports.create_job(db, actor, body.dataset, body.format, body.filters)
    log(
        db,
        actor,
        request,
        "export.created",
        "export",
        job.export_uid,
        {"dataset": body.dataset, "format": body.format, "filters": body.filters},
    )
    db.commit()
    tasks.add_task(_run_export, request.app, actor.org_id, job.export_uid)
    return _job(job)


@exports_router.get("/{uid}", responses=ERR, summary="Export status")
def get_export(uid: Uid, db: Db, actor: Exporter) -> dict[str, Any]:
    return _job(exports.get_job(db, uid))


@exports_router.get("/{uid}/download", responses=ERR, summary="Download a ready export")
def download(uid: Uid, request: Request, db: Db, actor: Exporter) -> FileResponse:
    job = exports.get_job(db, uid)
    path = exports.file_for(request.app.state.settings, job)
    log(db, actor, request, "export.downloaded", "export", uid)
    media = "text/csv" if job.format == "csv" else "application/json"
    return FileResponse(
        path,
        media_type=media,
        filename=f"atlas-{job.dataset}-{uid}.{job.format}",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
