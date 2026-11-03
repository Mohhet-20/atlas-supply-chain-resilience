"""Operational endpoints for platform administrators: metrics, dependency health, queue depth. No tenant data, no secrets."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

from app.core.auth import Actor
from app.core.permissions import Permission, require_permission
from app.models.integrations import ExportJob, IntegrationRun, WebhookDelivery
from app.models.types import utcnow
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/system", tags=["system"])
Sysadmin = Annotated[Actor, Depends(require_permission(Permission.SYSTEM_ADMIN))]
ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}


def _queues(request: Request) -> dict[str, Any]:
    """Cross-organisation job counts (platform view; unscoped session by design, counts only)."""
    now = utcnow()
    with request.app.state.session_factory() as db:
        deliveries = dict(
            db.execute(select(WebhookDelivery.status, func.count()).group_by(WebhookDelivery.status)).all()
        )
        oldest = db.scalar(
            select(func.min(WebhookDelivery.next_attempt_at)).where(
                WebhookDelivery.status.in_(("queued", "attempted")), WebhookDelivery.next_attempt_at <= now
            )
        )
        runs = dict(
            db.execute(
                select(IntegrationRun.status, func.count())
                .where(IntegrationRun.status.in_(("queued", "running")))
                .group_by(IntegrationRun.status)
            ).all()
        )
        exports = dict(
            db.execute(
                select(ExportJob.status, func.count())
                .where(ExportJob.status.in_(("queued", "running")))
                .group_by(ExportJob.status)
            ).all()
        )
    lag = max(0, int((now - oldest).total_seconds())) if oldest else 0
    return {
        "webhook_deliveries": deliveries,
        "oldest_due_delivery_age_seconds": lag,
        "sync_runs": runs,
        "exports": exports,
    }


def _workers(request: Request) -> dict[str, Any]:
    stale = request.app.state.settings.worker_stale_seconds
    beats: dict[str, float] = getattr(request.app.state, "worker_beats", {})
    now = time.time()
    out = {}
    for name, ts in beats.items():
        age = int(now - ts)
        out[name] = {"last_beat_seconds_ago": age, "status": "ok" if age <= stale else "stale"}
    return out


@router.get("/health", summary="Dependency and worker health (platform administrators)", responses=ERR)
def system_health(request: Request, _a: Sysadmin) -> dict[str, Any]:
    st = request.app.state
    t0 = time.perf_counter()
    try:
        with st.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db = {"status": "ok", "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception:
        db = {"status": "unavailable", "latency_ms": None}
    pool = getattr(st.engine, "pool", None)
    pool_info = {"size": pool.size(), "checked_out": pool.checkedout()} if hasattr(pool, "checkedout") else None
    workers = _workers(request)
    configured = st.settings.alerting_tick_seconds > 0
    status = "ok"
    if db["status"] != "ok":
        status = "unavailable"
    elif any(w["status"] == "stale" for w in workers.values()):
        status = "degraded"
    return {
        "status": status,
        "database": db,
        "pool": pool_info,
        "workers": {"in_process_enabled": configured, "beats": workers},
        "queues": _queues(request) if db["status"] == "ok" else None,
        "cache": st.cache.stats(),
        "note": "Process-local view. With several processes each reports its own cache, metrics and worker beats.",
    }


@router.get("/metrics", summary="Request and event metrics (JSON)", responses=ERR)
def system_metrics(request: Request, _a: Sysadmin) -> dict[str, Any]:
    return {**request.app.state.metrics.snapshot(), "cache": request.app.state.cache.stats()}


@router.get(
    "/metrics/prometheus", summary="Metrics in Prometheus text format", response_class=PlainTextResponse, responses=ERR
)
def system_metrics_prom(request: Request, _a: Sysadmin) -> PlainTextResponse:
    st = request.app.state
    c = st.cache.stats()
    extra = {
        "atlas_cache_hits_total": c["hits"],
        "atlas_cache_misses_total": c["misses"],
        "atlas_cache_entries": c["entries"],
    }
    return PlainTextResponse(st.metrics.prometheus(extra), media_type="text/plain; version=0.0.4")
