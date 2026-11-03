"""Thin HTTP layer: validation + delegation to services."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.deps import VIEW, DbSession, Limit, Offset, SearchText
from app.db.migrations_check import assert_schema_current
from app.models.enums import EntityStatus, FacilityType, Provenance
from app.schemas.common import ErrorResponse, Page
from app.schemas.dashboard import DashboardSummary
from app.schemas.facility import FacilityDetail, FacilityRead
from app.schemas.health import HealthResponse, ReadinessResponse
from app.schemas.network import NetworkSummary
from app.schemas.observation import ObservationRead
from app.schemas.supplier import SupplierDetail, SupplierRead
from app.services import dashboard as dashboard_service
from app.services import network as network_service
from app.services import queries
from app.services.dataversion import network_data_version

router = APIRouter()
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Resource not found"}}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1, description="Numeric record id.")]


@router.get("/health", response_model=HealthResponse, tags=["system"], summary="Liveness probe")
def health(request: Request) -> HealthResponse:
    """Reports only that the process is alive. Does not touch the database."""
    return HealthResponse(status="ok", app=request.app.state.settings.app_name)


def _org(db) -> object:  # noqa: ANN001
    """Tenant component of every cache key: the organisation the request session is scoped to."""
    from app.db.tenancy import session_org

    return session_org(db)


def _schema_check(request: Request) -> None:
    """Schema revision check, remembered briefly when OK (revision only changes on deploy). Failures are never cached."""
    import time

    app, ttl = request.app, request.app.state.settings.readiness_schema_cache_seconds
    ok_until = getattr(app.state, "schema_ok_until", 0.0)
    if ttl > 0 and time.monotonic() < ok_until:
        return
    assert_schema_current(app.state.engine)
    app.state.schema_ok_until = time.monotonic() + ttl


@router.get(
    "/readiness",
    response_model=ReadinessResponse,
    tags=["system"],
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse}},
)
def readiness(request: Request):
    """Verifies the database is reachable and migrated to the expected revision. No internals are exposed."""
    checks = {"database": "ok", "schema": "ok"}
    engine = request.app.state.engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        checks = {"database": "unavailable", "schema": "unknown"}
    else:
        try:
            _schema_check(request)
        except Exception:
            checks["schema"] = "not_migrated"
    ready = all(v == "ok" for v in checks.values())
    body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
    if ready:
        return body
    return JSONResponse(status_code=503, content=body.model_dump())


@router.get(
    "/dashboard/summary",
    dependencies=VIEW,
    response_model=DashboardSummary,
    tags=["dashboard"],
    summary="Dashboard summary",
)
def dashboard_summary(db: DbSession, request: Request) -> DashboardSummary:
    """Database-derived counts, provenance breakdown, coverage indicator and baseline metrics."""
    cache = request.app.state.cache
    return cache.get_or_compute(
        ("dashboard", _org(db)), network_data_version(db), lambda: dashboard_service.build_dashboard_summary(db)
    )


@router.get(
    "/suppliers", dependencies=VIEW, response_model=Page[SupplierRead], tags=["suppliers"], summary="List suppliers"
)
def list_suppliers(
    db: DbSession,
    limit: Limit = 25,
    offset: Offset = 0,
    q: SearchText = None,
    status: EntityStatus | None = None,
    country_or_region: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=100)] = None,
    provenance: Provenance | None = None,
) -> Page[SupplierRead]:
    return queries.list_suppliers(
        db,
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        country_or_region=country_or_region,
        category=category,
        provenance=provenance,
    )


@router.get(
    "/suppliers/{supplier_id}",
    dependencies=VIEW,
    response_model=SupplierDetail,
    tags=["suppliers"],
    responses=NOT_FOUND,
    summary="Supplier detail",
)
def get_supplier(supplier_id: IdPath, db: DbSession) -> SupplierDetail:
    return queries.get_supplier_detail(db, supplier_id)


@router.get(
    "/facilities", dependencies=VIEW, response_model=Page[FacilityRead], tags=["facilities"], summary="List facilities"
)
def list_facilities(
    db: DbSession,
    limit: Limit = 25,
    offset: Offset = 0,
    q: SearchText = None,
    status: EntityStatus | None = None,
    facility_type: FacilityType | None = None,
    country_or_region: Annotated[str | None, Query(max_length=100)] = None,
    provenance: Provenance | None = None,
) -> Page[FacilityRead]:
    return queries.list_facilities(
        db,
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        facility_type=facility_type,
        country_or_region=country_or_region,
        provenance=provenance,
    )


@router.get(
    "/facilities/{facility_id}",
    dependencies=VIEW,
    response_model=FacilityDetail,
    tags=["facilities"],
    responses=NOT_FOUND,
    summary="Facility detail with relationships",
)
def get_facility(facility_id: IdPath, db: DbSession) -> FacilityDetail:
    return queries.get_facility_detail(db, facility_id)


@router.get(
    "/network/summary",
    dependencies=VIEW,
    response_model=NetworkSummary,
    tags=["network"],
    summary="Supply-chain network overview",
)
def network_summary(
    db: DbSession,
    request: Request,
    max_edges: Annotated[int, Query(ge=1, le=network_service.MAX_EDGE_LIMIT)] = network_service.DEFAULT_EDGE_LIMIT,
) -> NetworkSummary:
    return request.app.state.cache.get_or_compute(
        ("network", _org(db), max_edges),
        network_data_version(db),
        lambda: network_service.build_network_summary(db, max_edges),
    )


@router.get(
    "/observations",
    dependencies=VIEW,
    response_model=Page[ObservationRead],
    tags=["observations"],
    summary="List observations (newest first)",
)
def list_observations(
    db: DbSession,
    limit: Limit = 25,
    offset: Offset = 0,
    supplier_id: Annotated[int | None, Query(ge=1)] = None,
    facility_id: Annotated[int | None, Query(ge=1)] = None,
    metric_name: Annotated[str | None, Query(max_length=100)] = None,
    provenance: Provenance | None = None,
) -> Page[ObservationRead]:
    return queries.list_observations(
        db,
        limit=limit,
        offset=offset,
        supplier_id=supplier_id,
        facility_id=facility_id,
        metric_name=metric_name,
        provenance=provenance,
    )
