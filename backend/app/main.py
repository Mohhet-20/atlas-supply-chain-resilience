"""FastAPI application factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from app.api.v1.admin import router as admin_router
from app.api.v1.alerting import router as alerting_router
from app.api.v1.alerting import run_tick_loop
from app.api.v1.analytics import router as analytics_router
from app.api.v1.auth import router as auth_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.decision import router as decision_router
from app.api.v1.forecast import router as forecast_router
from app.api.v1.graph import router as graph_router
from app.api.v1.impact import router as impact_router
from app.api.v1.ingestion import lineage_router
from app.api.v1.ingestion import router as ingestion_router
from app.api.v1.integrations import router as integrations_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.leadtime import router as leadtime_router
from app.api.v1.modelops import router as modelops_router
from app.api.v1.quality import router as quality_router
from app.api.v1.registry import router as registry_router
from app.api.v1.replenishment import router as replenishment_router
from app.api.v1.resilience import router as resilience_router
from app.api.v1.risk import router as risk_router
from app.api.v1.routes import router as v1_router
from app.api.v1.routing import router as routing_router
from app.api.v1.scenarios import router as scenarios_router
from app.api.v1.sourcing import router as sourcing_router
from app.api.v1.system import router as system_router
from app.api.v1.webhooks import exports_router, hooks_router, inbound_router, keys_router
from app.core.cache import TTLCache
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.metrics import Metrics, MetricsMiddleware
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import ApiRateLimitMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.db.session import create_engine_from_settings, make_session_factory
from app.integrations.worker import run_loop as run_integration_loop


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    engine = engine or create_engine_from_settings(settings)
    is_prod = settings.production_like

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop, worker, iworker = threading.Event(), None, None
        if settings.alerting_tick_seconds > 0 and settings.embedded_worker:
            worker = threading.Thread(target=run_tick_loop, args=(_app, stop), name="atlas-alert-tick", daemon=True)
            worker.start()
            iworker = threading.Thread(
                target=run_integration_loop, args=(_app, stop), name="atlas-integrations", daemon=True
            )
            iworker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=5)
            if iworker is not None:
                iworker.join(timeout=5)

    app = FastAPI(
        lifespan=lifespan,
        title=f"{settings.app_name} API",
        description="ATLAS - Global Supply Chain Resilience Intelligence Platform. Decision-support only; "
        "Authenticated with bearer credentials (session, API key or static token); each operation "
        "lists the role or permission it requires. Responses distinguish data provenance.",
        version="0.1.0",
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.cache = TTLCache(settings.cache_max_entries, settings.cache_ttl_seconds, settings.cache_enabled)
    app.state.metrics = Metrics()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "Authorization"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(
        RequestContextMiddleware,
        max_request_bytes=settings.max_request_bytes,
        upload_prefix=f"{settings.api_prefix}/ingestion/",
        max_upload_bytes=settings.max_upload_bytes,
    )
    if settings.security_headers_enabled:
        app.add_middleware(
            SecurityHeadersMiddleware,
            hsts=is_prod,
            max_bytes=settings.max_request_bytes,
            upload_prefix=f"{settings.api_prefix}/ingestion/",
            max_upload_bytes=settings.max_upload_bytes,
        )
    if settings.api_rate_limit_per_minute > 0:
        app.add_middleware(
            ApiRateLimitMiddleware, limit_per_minute=settings.api_rate_limit_per_minute, metrics=app.state.metrics
        )
    app.add_middleware(MetricsMiddleware, metrics=app.state.metrics)  # outermost: sees every response
    register_exception_handlers(app)
    app.include_router(v1_router, prefix=settings.api_prefix)
    routers = (
        catalog_router,
        ingestion_router,
        lineage_router,
        registry_router,
        graph_router,
        quality_router,
        risk_router,
        scenarios_router,
        impact_router,
        inventory_router,
        forecast_router,
        leadtime_router,
        modelops_router,
        routing_router,
        sourcing_router,
        replenishment_router,
        resilience_router,
        decision_router,
        analytics_router,
        alerting_router,
        integrations_router,
        keys_router,
        hooks_router,
        inbound_router,
        exports_router,
        system_router,
        auth_router,
        admin_router,
    )
    for extra in routers:
        app.include_router(extra, prefix=settings.api_prefix)
    from app.core.openapi import install_openapi

    install_openapi(app)
    return app
