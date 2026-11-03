import pytest

from app.core.errors import ConflictError, DomainValidationError
from app.schemas.facility import FacilityCreate
from app.schemas.observation import ObservationCreate
from app.schemas.relationship import RelationshipCreate
from app.schemas.supplier import SupplierCreate
from app.services import registry

S = SupplierCreate(supplier_code="S1", name="s", country_or_region="r", category="c", provenance="USER_PROVIDED")
F = FacilityCreate(
    facility_code="F1", name="f", facility_type="WAREHOUSE", country_or_region="r", provenance="USER_PROVIDED"
)


def test_duplicate_codes_raise_conflict(session):
    registry.create_supplier(session, S)
    registry.create_facility(session, F)
    with pytest.raises(ConflictError):
        registry.create_supplier(session, S)
    with pytest.raises(ConflictError):
        registry.create_facility(session, F)


def test_relationship_requires_known_entities(session):
    registry.create_facility(session, F)
    rel = RelationshipCreate(
        relationship_code="R1",
        relationship_type="SUPPLIES",
        supplier_code="NOPE",
        target_facility_code="F1",
        provenance="USER_PROVIDED",
    )
    with pytest.raises(DomainValidationError):
        registry.create_relationship(session, rel)
    bad_target = RelationshipCreate(
        relationship_code="R2",
        relationship_type="SUPPLIES",
        supplier_code="S1",
        target_facility_code="NOPE",
        provenance="USER_PROVIDED",
    )
    registry.create_supplier(session, S)
    with pytest.raises(DomainValidationError):
        registry.create_relationship(session, bad_target)


def test_valid_relationship_and_duplicate_code(session):
    registry.create_supplier(session, S)
    registry.create_facility(session, F)
    rel = RelationshipCreate(
        relationship_code="R1",
        relationship_type="SUPPLIES",
        supplier_code="S1",
        target_facility_code="F1",
        capacity_value=10,
        capacity_unit="units/week",
        provenance="USER_PROVIDED",
    )
    created = registry.create_relationship(session, rel)
    assert created.supplier_id and created.capacity_unit == "units/week"
    with pytest.raises(ConflictError):
        registry.create_relationship(session, rel)


def test_observation_requires_known_subject(session):
    obs = ObservationCreate(
        observation_code="O1",
        supplier_code="NOPE",
        metric_name="m",
        value=1,
        unit="u",
        observed_at="2026-01-01T00:00:00Z",
        source_label="x",
        provenance="USER_PROVIDED",
    )
    with pytest.raises(DomainValidationError):
        registry.create_observation(session, obs)
