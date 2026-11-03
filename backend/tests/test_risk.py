import pytest

from app.models.enums import RelationshipType as T
from app.services.risk import (
    FacilityRec,
    NetworkData,
    ObsRec,
    RelRec,
    SupplierRec,
    compute_risk_summary,
    connectivity_coverage,
    observation_coverage,
    relationship_attribute_completeness,
    single_source_facilities,
    supplier_dependency_concentration,
)

D = "SYNTHETIC_DEMO"


def sup(i):
    return SupplierRec(i, f"S{i}", D)


def fac(i):
    return FacilityRec(i, f"F{i}", D)


def supplies(s, f, cap=None, unit=None, lead=None):
    return RelRec(T.SUPPLIES, s, None, f, cap, unit, lead, D)


def transfer(a, b, cap=None, unit=None, lead=None):
    return RelRec(T.TRANSFERS_TO, None, a, b, cap, unit, lead, D)


def metric(summary, key):
    return next(m for m in summary.metrics if m.key == key)


def test_empty_database_yields_insufficient_data_everywhere():
    summary = compute_risk_summary(NetworkData())
    assert {m.state for m in summary.metrics} == {"insufficient_data"}
    assert all(m.value is None and m.message for m in summary.metrics)
    assert "not" in summary.disclaimer.lower()


def test_entities_without_relationships():
    data = NetworkData(suppliers=[sup(1)], facilities=[fac(1)])
    assert single_source_facilities(data).state == "insufficient_data"
    assert supplier_dependency_concentration(data).state == "insufficient_data"
    assert relationship_attribute_completeness(data).state == "insufficient_data"
    assert connectivity_coverage(data).value == 0.0
    assert observation_coverage(data).value == 0.0
    assert metric(compute_risk_summary(data), "data_coverage_indicator").state == "insufficient_data"


def test_hhi_capacity_basis_hand_computed():
    # shares 0.75 / 0.25 -> 0.5625 + 0.0625 = 0.625
    data = NetworkData(
        suppliers=[sup(1), sup(2)],
        facilities=[fac(1)],
        relationships=[supplies(1, 1, 3000, "u/w"), supplies(2, 1, 1000, "u/w")],
    )
    m = supplier_dependency_concentration(data)
    assert m.value == pytest.approx(0.625)
    assert m.details[0]["basis"] == "capacity"


def test_hhi_falls_back_to_count_when_capacity_missing_or_units_differ():
    missing = NetworkData(
        suppliers=[sup(1), sup(2)], facilities=[fac(1)], relationships=[supplies(1, 1, 3000, "u/w"), supplies(2, 1)]
    )
    assert supplier_dependency_concentration(missing).value == pytest.approx(0.5)
    mixed = NetworkData(
        suppliers=[sup(1), sup(2)],
        facilities=[fac(1)],
        relationships=[supplies(1, 1, 3000, "u/w"), supplies(2, 1, 1000, "u/d")],
    )
    m = supplier_dependency_concentration(mixed)
    assert m.value == pytest.approx(0.5) and m.details[0]["basis"] == "relationship_count"


def test_hhi_zero_capacity_does_not_divide_by_zero():
    data = NetworkData(
        suppliers=[sup(1), sup(2)], facilities=[fac(1)], relationships=[supplies(1, 1, 0, "u"), supplies(2, 1, 0, "u")]
    )
    assert supplier_dependency_concentration(data).value == pytest.approx(0.5)


def test_same_supplier_two_edges_is_still_single_source():
    data = NetworkData(
        suppliers=[sup(1)],
        facilities=[fac(1), fac(2)],
        relationships=[supplies(1, 1, 5, "u"), supplies(1, 1, 6, "u"), supplies(1, 2)],
    )
    m = single_source_facilities(data)
    assert m.value == 2.0 and m.coverage == 1.0
    assert supplier_dependency_concentration(data).value == pytest.approx(1.0)


def test_single_source_counts_only_one_supplier_facilities():
    data = NetworkData(
        suppliers=[sup(1), sup(2)],
        facilities=[fac(1), fac(2), fac(3)],
        relationships=[supplies(1, 1), supplies(2, 1), supplies(1, 2)],
    )
    m = single_source_facilities(data)
    assert m.value == 1.0
    assert m.inputs["single_source_facility_codes"] == "F2"
    assert m.coverage == pytest.approx(2 / 3)


def test_completeness_connectivity_observation_and_overall():
    data = NetworkData(
        suppliers=[sup(1), sup(2)],
        facilities=[fac(1), fac(2)],
        relationships=[supplies(1, 1, 10, "u", 5), transfer(1, 2, None, None, None)],
        observations=[ObsRec(1, None, D), ObsRec(None, 2, D)],
    )
    assert relationship_attribute_completeness(data).value == pytest.approx(0.5)  # (1/2 + 1/2)/2
    assert connectivity_coverage(data).value == pytest.approx(3 / 4)  # supplier 2 unconnected
    assert observation_coverage(data).value == pytest.approx(2 / 4)
    overall = metric(compute_risk_summary(data), "data_coverage_indicator")
    assert overall.value == pytest.approx((0.5 + 0.75 + 0.5) / 3)
    assert overall.provenance == {D: 2 + 2 + 2 + 2}


def test_provenance_is_carried_through_and_not_merged():
    rels = [RelRec(T.SUPPLIES, 1, None, 1, None, None, None, "USER_PROVIDED"), supplies(2, 1)]
    data = NetworkData(suppliers=[sup(1), sup(2)], facilities=[fac(1)], relationships=rels)
    assert supplier_dependency_concentration(data).provenance == {"USER_PROVIDED": 1, D: 1}


def test_deterministic():
    data = NetworkData(
        suppliers=[sup(1), sup(2)], facilities=[fac(1)], relationships=[supplies(1, 1, 3, "u"), supplies(2, 1, 1, "u")]
    )
    assert compute_risk_summary(data) == compute_risk_summary(data)
