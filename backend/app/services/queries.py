"""Read-side services: bounded, paginated listing and detail lookups."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import NotFoundError
from app.models.enums import EntityStatus, FacilityType, Provenance
from app.models.facility import Facility
from app.models.observation import Observation
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.schemas.common import Page
from app.schemas.facility import FacilityDetail, FacilityRead
from app.schemas.observation import ObservationRead
from app.schemas.relationship import RelationshipRead
from app.schemas.supplier import SupplierDetail, SupplierRead
from app.services.mappers import observation_read, relationship_read

RECENT_OBSERVATION_LIMIT = 10
DETAIL_RELATIONSHIP_LIMIT = 200


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _rel_query():
    return select(SupplyRelationship).options(
        joinedload(SupplyRelationship.supplier),
        joinedload(SupplyRelationship.source_facility),
        joinedload(SupplyRelationship.target_facility),
    )


def _obs_query():
    return select(Observation).options(joinedload(Observation.supplier), joinedload(Observation.facility))


def list_suppliers(
    session: Session,
    *,
    limit: int,
    offset: int,
    q: str | None = None,
    status: EntityStatus | None = None,
    country_or_region: str | None = None,
    category: str | None = None,
    provenance: Provenance | None = None,
) -> Page[SupplierRead]:
    stmt = select(Supplier)
    if q:
        stmt = stmt.where(
            or_(Supplier.name.ilike(_like(q), escape="\\"), Supplier.supplier_code.ilike(_like(q), escape="\\"))
        )
    if status:
        stmt = stmt.where(Supplier.status == status)
    if country_or_region:
        stmt = stmt.where(Supplier.country_or_region == country_or_region)
    if category:
        stmt = stmt.where(Supplier.category == category)
    if provenance:
        stmt = stmt.where(Supplier.provenance == provenance)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(Supplier.supplier_code, Supplier.id).limit(limit).offset(offset)).all()
    return Page[SupplierRead](
        items=[SupplierRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


def get_supplier_detail(session: Session, supplier_id: int) -> SupplierDetail:
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {supplier_id} not found.")
    rels = (
        session.scalars(
            _rel_query()
            .where(SupplyRelationship.supplier_id == supplier_id)
            .order_by(SupplyRelationship.id)
            .limit(DETAIL_RELATIONSHIP_LIMIT)
        )
        .unique()
        .all()
    )
    obs = (
        session.scalars(
            _obs_query()
            .where(Observation.supplier_id == supplier_id)
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(RECENT_OBSERVATION_LIMIT)
        )
        .unique()
        .all()
    )
    return SupplierDetail(
        **SupplierRead.model_validate(supplier).model_dump(),
        relationships=[relationship_read(r) for r in rels],
        recent_observations=[observation_read(o) for o in obs],
    )


def list_facilities(
    session: Session,
    *,
    limit: int,
    offset: int,
    q: str | None = None,
    status: EntityStatus | None = None,
    facility_type: FacilityType | None = None,
    country_or_region: str | None = None,
    provenance: Provenance | None = None,
) -> Page[FacilityRead]:
    stmt = select(Facility)
    if q:
        stmt = stmt.where(
            or_(Facility.name.ilike(_like(q), escape="\\"), Facility.facility_code.ilike(_like(q), escape="\\"))
        )
    if status:
        stmt = stmt.where(Facility.status == status)
    if facility_type:
        stmt = stmt.where(Facility.facility_type == facility_type)
    if country_or_region:
        stmt = stmt.where(Facility.country_or_region == country_or_region)
    if provenance:
        stmt = stmt.where(Facility.provenance == provenance)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(Facility.facility_code, Facility.id).limit(limit).offset(offset)).all()
    return Page[FacilityRead](
        items=[FacilityRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


def get_facility_detail(session: Session, facility_id: int) -> FacilityDetail:
    facility = session.get(Facility, facility_id)
    if facility is None:
        raise NotFoundError(f"Facility {facility_id} not found.")
    inbound = (
        session.scalars(
            _rel_query()
            .where(SupplyRelationship.target_facility_id == facility_id)
            .order_by(SupplyRelationship.id)
            .limit(DETAIL_RELATIONSHIP_LIMIT)
        )
        .unique()
        .all()
    )
    outbound = (
        session.scalars(
            _rel_query()
            .where(SupplyRelationship.source_facility_id == facility_id)
            .order_by(SupplyRelationship.id)
            .limit(DETAIL_RELATIONSHIP_LIMIT)
        )
        .unique()
        .all()
    )
    obs = (
        session.scalars(
            _obs_query()
            .where(Observation.facility_id == facility_id)
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(RECENT_OBSERVATION_LIMIT)
        )
        .unique()
        .all()
    )
    return FacilityDetail(
        **FacilityRead.model_validate(facility).model_dump(),
        inbound_relationships=[relationship_read(r) for r in inbound],
        outbound_relationships=[relationship_read(r) for r in outbound],
        recent_observations=[observation_read(o) for o in obs],
    )


def list_observations(
    session: Session,
    *,
    limit: int,
    offset: int,
    supplier_id: int | None = None,
    facility_id: int | None = None,
    metric_name: str | None = None,
    provenance: Provenance | None = None,
) -> Page[ObservationRead]:
    stmt = select(Observation)
    if supplier_id is not None:
        stmt = stmt.where(Observation.supplier_id == supplier_id)
    if facility_id is not None:
        stmt = stmt.where(Observation.facility_id == facility_id)
    if metric_name:
        stmt = stmt.where(Observation.metric_name == metric_name)
    if provenance:
        stmt = stmt.where(Observation.provenance == provenance)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        session.scalars(
            stmt.options(joinedload(Observation.supplier), joinedload(Observation.facility))
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .unique()
        .all()
    )
    return Page[ObservationRead](items=[observation_read(o) for o in rows], total=total, limit=limit, offset=offset)


def recent_observations(session: Session, limit: int = 5) -> list[ObservationRead]:
    rows = (
        session.scalars(_obs_query().order_by(Observation.observed_at.desc(), Observation.id.desc()).limit(limit))
        .unique()
        .all()
    )
    return [observation_read(o) for o in rows]


__all__ = [
    "RelationshipRead",
    "get_facility_detail",
    "get_supplier_detail",
    "list_facilities",
    "list_observations",
    "list_suppliers",
    "recent_observations",
]
