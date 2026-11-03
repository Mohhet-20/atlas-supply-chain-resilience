"""Write-side domain operations. Callers own the transaction (use ``with session.begin():``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError
from app.models.enums import RelationshipType
from app.models.facility import Facility
from app.models.observation import Observation
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource
from app.models.supplier import Supplier
from app.schemas.facility import FacilityCreate
from app.schemas.observation import ObservationCreate
from app.schemas.product import ProductCreate, ProductLinkCreate
from app.schemas.relationship import RelationshipCreate
from app.schemas.supplier import SupplierCreate


def _source_kwargs(source: DataSource | None, external_id: str | None = None) -> dict:
    """Source namespace scoping for records created from a known source (external id = its code)."""
    if source is None:
        return {}
    return {"source_id": source.id, "external_id": external_id}


def get_supplier_by_code(session: Session, code: str) -> Supplier | None:
    return session.scalar(select(Supplier).where(Supplier.supplier_code == code))


def get_facility_by_code(session: Session, code: str) -> Facility | None:
    return session.scalar(select(Facility).where(Facility.facility_code == code))


def create_supplier(session: Session, data: SupplierCreate, source: DataSource | None = None) -> Supplier:
    if get_supplier_by_code(session, data.supplier_code):
        raise ConflictError(f"Supplier code '{data.supplier_code}' already exists.")
    obj = Supplier(**data.model_dump(), **_source_kwargs(source, data.supplier_code))
    session.add(obj)
    session.flush()
    return obj


def create_facility(session: Session, data: FacilityCreate, source: DataSource | None = None) -> Facility:
    if get_facility_by_code(session, data.facility_code):
        raise ConflictError(f"Facility code '{data.facility_code}' already exists.")
    fields = data.model_dump(exclude={"owner_supplier_code"})
    owner_id = None
    if data.owner_supplier_code:
        owner = get_supplier_by_code(session, data.owner_supplier_code)
        if owner is None:
            raise DomainValidationError(f"Unknown owner supplier '{data.owner_supplier_code}'.")
        owner_id = owner.id
    obj = Facility(**fields, owner_supplier_id=owner_id, **_source_kwargs(source, data.facility_code))
    session.add(obj)
    session.flush()
    return obj


def create_relationship(
    session: Session, data: RelationshipCreate, source: DataSource | None = None
) -> SupplyRelationship:
    if session.scalar(
        select(SupplyRelationship.id).where(SupplyRelationship.relationship_code == data.relationship_code)
    ):
        raise ConflictError(f"Relationship code '{data.relationship_code}' already exists.")
    target = get_facility_by_code(session, data.target_facility_code)
    if target is None:
        raise DomainValidationError(f"Unknown target facility '{data.target_facility_code}'.")
    supplier_id = source_id = None
    if data.relationship_type is RelationshipType.SUPPLIES:
        supplier = get_supplier_by_code(session, data.supplier_code or "")
        if supplier is None:
            raise DomainValidationError(f"Unknown supplier '{data.supplier_code}'.")
        supplier_id = supplier.id
    else:
        origin = get_facility_by_code(session, data.source_facility_code or "")
        if origin is None:
            raise DomainValidationError(f"Unknown source facility '{data.source_facility_code}'.")
        source_id = origin.id
    obj = SupplyRelationship(
        relationship_code=data.relationship_code,
        relationship_type=data.relationship_type,
        supplier_id=supplier_id,
        source_facility_id=source_id,
        target_facility_id=target.id,
        capacity_value=data.capacity_value,
        capacity_unit=data.capacity_unit.strip() if data.capacity_unit else None,
        lead_time_days=data.lead_time_days,
        provenance=data.provenance,
        **_source_kwargs(source, data.relationship_code),
    )
    session.add(obj)
    session.flush()
    return obj


def create_product(session: Session, data: ProductCreate, source: DataSource | None = None) -> Product:
    if session.scalar(select(Product.id).where(Product.product_code == data.product_code)):
        raise ConflictError(f"Product code '{data.product_code}' already exists.")
    obj = Product(**data.model_dump(), **_source_kwargs(source, data.product_code))
    session.add(obj)
    session.flush()
    return obj


def create_product_link(session: Session, data: ProductLinkCreate, source: DataSource | None = None) -> ProductLink:
    if session.scalar(select(ProductLink.id).where(ProductLink.link_code == data.link_code)):
        raise ConflictError(f"Product link code '{data.link_code}' already exists.")
    product = session.scalar(select(Product).where(Product.product_code == data.product_code))
    if product is None:
        raise DomainValidationError(f"Unknown product '{data.product_code}'.")
    supplier_id = facility_id = None
    if data.supplier_code:
        supplier = get_supplier_by_code(session, data.supplier_code)
        if supplier is None:
            raise DomainValidationError(f"Unknown supplier '{data.supplier_code}'.")
        supplier_id = supplier.id
    else:
        facility = get_facility_by_code(session, data.facility_code or "")
        if facility is None:
            raise DomainValidationError(f"Unknown facility '{data.facility_code}'.")
        facility_id = facility.id
    obj = ProductLink(
        **data.model_dump(exclude={"product_code", "supplier_code", "facility_code"}),
        product_id=product.id,
        supplier_id=supplier_id,
        facility_id=facility_id,
        **_source_kwargs(source, data.link_code),
    )
    session.add(obj)
    session.flush()
    return obj


def create_observation(session: Session, data: ObservationCreate) -> Observation:
    if session.scalar(select(Observation.id).where(Observation.observation_code == data.observation_code)):
        raise ConflictError(f"Observation code '{data.observation_code}' already exists.")
    supplier_id = facility_id = None
    if data.supplier_code:
        supplier = get_supplier_by_code(session, data.supplier_code)
        if supplier is None:
            raise DomainValidationError(f"Unknown supplier '{data.supplier_code}'.")
        supplier_id = supplier.id
    else:
        facility = get_facility_by_code(session, data.facility_code or "")
        if facility is None:
            raise DomainValidationError(f"Unknown facility '{data.facility_code}'.")
        facility_id = facility.id
    obj = Observation(
        observation_code=data.observation_code,
        supplier_id=supplier_id,
        facility_id=facility_id,
        metric_name=data.metric_name,
        value=data.value,
        unit=data.unit,
        observed_at=data.observed_at,
        source_label=data.source_label,
        provenance=data.provenance,
        quality_notes=data.quality_notes,
    )
    session.add(obj)
    session.flush()
    return obj
