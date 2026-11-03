from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.facility import FacilityCreate
from app.schemas.observation import ObservationCreate
from app.schemas.relationship import RelationshipCreate
from app.schemas.supplier import SupplierCreate

BASE_REL = {"relationship_code": "R-1", "target_facility_code": "F-1", "provenance": "SYNTHETIC_DEMO"}
BASE_OBS = {
    "observation_code": "O-1",
    "supplier_code": "S-1",
    "metric_name": "m",
    "value": 1.0,
    "unit": "percent",
    "observed_at": "2026-01-01T00:00:00Z",
    "source_label": "src",
    "provenance": "SYNTHETIC_DEMO",
}


def test_supplier_create_trims_and_validates_code():
    s = SupplierCreate(
        supplier_code=" DEMO-SUP-001 ", name=" A ", country_or_region="X", category="C", provenance="SYNTHETIC_DEMO"
    )
    assert s.supplier_code == "DEMO-SUP-001" and s.name == "A"
    with pytest.raises(ValidationError):
        SupplierCreate(
            supplier_code="bad code!", name="A", country_or_region="X", category="C", provenance="USER_PROVIDED"
        )


def test_supplier_requires_known_provenance():
    with pytest.raises(ValidationError):
        SupplierCreate(supplier_code="S1", name="A", country_or_region="X", category="C", provenance="REAL")


def test_blank_name_rejected():
    with pytest.raises(ValidationError):
        FacilityCreate(
            facility_code="F1", name="   ", facility_type="WAREHOUSE", country_or_region="X", provenance="USER_PROVIDED"
        )


def test_supplies_requires_supplier_and_forbids_source_facility():
    RelationshipCreate(**BASE_REL, relationship_type="SUPPLIES", supplier_code="S-1")
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="SUPPLIES")
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="SUPPLIES", supplier_code="S-1", source_facility_code="F-2")


def test_transfer_rules():
    RelationshipCreate(**BASE_REL, relationship_type="TRANSFERS_TO", source_facility_code="F-2")
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="TRANSFERS_TO", supplier_code="S-1")
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="TRANSFERS_TO", source_facility_code="F-1")


def test_capacity_requires_unit_and_nonnegative():
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="SUPPLIES", supplier_code="S", capacity_value=10)
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="SUPPLIES", supplier_code="S", capacity_unit="u")
    with pytest.raises(ValidationError):
        RelationshipCreate(
            **BASE_REL, relationship_type="SUPPLIES", supplier_code="S", capacity_value=-1, capacity_unit="u"
        )
    with pytest.raises(ValidationError):
        RelationshipCreate(**BASE_REL, relationship_type="SUPPLIES", supplier_code="S", lead_time_days=-2)


def test_observation_valid_and_offset_preserved():
    o = ObservationCreate(**{**BASE_OBS, "observed_at": "2026-01-01T05:30:00+05:30"})
    assert o.observed_at.utcoffset() is not None


def test_observation_rejects_naive_timestamp():
    with pytest.raises(ValidationError):
        ObservationCreate(**{**BASE_OBS, "observed_at": datetime(2026, 1, 1)})
    assert ObservationCreate(**{**BASE_OBS, "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})


@pytest.mark.parametrize(
    "override", [{"unit": " "}, {"unit": None}, {"value": float("nan")}, {"value": float("inf")}, {"source_label": ""}]
)
def test_observation_rejects_missing_unit_or_bad_value(override):
    data = {**BASE_OBS, **override}
    if data.get("unit") is None:
        data.pop("unit")
    with pytest.raises(ValidationError):
        ObservationCreate(**data)


def test_observation_requires_exactly_one_subject():
    with pytest.raises(ValidationError):
        ObservationCreate(**{**BASE_OBS, "facility_code": "F-1"})
    data = {k: v for k, v in BASE_OBS.items() if k != "supplier_code"}
    with pytest.raises(ValidationError):
        ObservationCreate(**data)
