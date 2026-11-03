"""ORM -> API schema conversion (kept separate so ORM models never leak into responses)."""

from __future__ import annotations

from app.models.enums import EntityType
from app.models.facility import Facility
from app.models.observation import Observation
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.schemas.observation import ObservationRead, SubjectRef
from app.schemas.relationship import EntityRef, RelationshipRead


def supplier_ref(s: Supplier) -> EntityRef:
    return EntityRef(type=EntityType.SUPPLIER, id=s.id, code=s.supplier_code, name=s.name)


def facility_ref(f: Facility) -> EntityRef:
    return EntityRef(type=EntityType.FACILITY, id=f.id, code=f.facility_code, name=f.name)


def relationship_read(r: SupplyRelationship) -> RelationshipRead:
    source = supplier_ref(r.supplier) if r.supplier is not None else facility_ref(r.source_facility)
    return RelationshipRead(
        id=r.id,
        relationship_code=r.relationship_code,
        relationship_type=r.relationship_type,
        source=source,
        target=facility_ref(r.target_facility),
        capacity_value=r.capacity_value,
        capacity_unit=r.capacity_unit,
        lead_time_days=r.lead_time_days,
        provenance=r.provenance,
        created_at=r.created_at,
    )


def observation_read(o: Observation) -> ObservationRead:
    ref = supplier_ref(o.supplier) if o.supplier is not None else facility_ref(o.facility)
    return ObservationRead(
        id=o.id,
        observation_code=o.observation_code,
        subject=SubjectRef(**ref.model_dump()),
        metric_name=o.metric_name,
        value=o.value,
        unit=o.unit,
        observed_at=o.observed_at,
        source_label=o.source_label,
        provenance=o.provenance,
        quality_notes=o.quality_notes,
    )


def product_link_read(link):
    from app.schemas.product import ProductLinkRead

    return ProductLinkRead(
        id=link.id,
        link_code=link.link_code,
        link_type=link.link_type,
        product_code=link.product.product_code,
        product_name=link.product.name,
        supplier_code=link.supplier.supplier_code if link.supplier else None,
        supplier_name=link.supplier.name if link.supplier else None,
        facility_code=link.facility.facility_code if link.facility else None,
        facility_name=link.facility.name if link.facility else None,
        quantity_value=link.quantity_value,
        quantity_unit=link.quantity_unit,
        lead_time_days=link.lead_time_days,
        effective_from=link.effective_from,
        effective_to=link.effective_to,
        provenance=link.provenance,
    )


def batch_read(b):
    from app.schemas.ingestion import BatchRead

    return BatchRead(
        id=b.id,
        batch_uid=b.batch_uid,
        status=b.status,
        policy=b.policy,
        source_id=b.source_id,
        source_namespace=b.source.namespace,
        entity_type=b.entity_type,
        source_format=b.source_format,
        mapping_profile_ref=b.mapping_profile_ref,
        mapping_profile_id=b.mapping_profile_id,
        file_name=b.file_name,
        content_sha256=b.content_sha256,
        content_bytes=b.content_bytes,
        fingerprint=b.fingerprint,
        normalization_version=b.normalization_version,
        total_rows=b.total_rows,
        created_count=b.created_count,
        updated_count=b.updated_count,
        unchanged_count=b.unchanged_count,
        rejected_count=b.rejected_count,
        skipped_count=b.skipped_count,
        duplicate_count=b.duplicate_count,
        finding_count=b.finding_count,
        failure_reason=b.failure_reason,
        started_at=b.started_at,
        finished_at=b.finished_at,
    )
