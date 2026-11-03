from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import Facility, Observation, Supplier, SupplyRelationship
from app.models.enums import EntityStatus, FacilityType, Provenance, RelationshipType

P = Provenance.USER_PROVIDED


def sup(code="S1"):
    return Supplier(
        supplier_code=code, name="n", country_or_region="r", category="c", status=EntityStatus.ACTIVE, provenance=P
    )


def fac(code="F1"):
    return Facility(
        facility_code=code,
        name="n",
        facility_type=FacilityType.WAREHOUSE,
        country_or_region="r",
        status=EntityStatus.ACTIVE,
        provenance=P,
    )


def commit_fails(session: Session, *objs):
    session.add_all(objs)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_duplicate_supplier_and_facility_codes_rejected(session):
    session.add_all([sup("S1"), fac("F1")])
    session.commit()
    commit_fails(session, sup("S1"))
    commit_fails(session, fac("F1"))


def test_blank_name_rejected_by_database(session):
    s = sup()
    s.name = "   "
    commit_fails(session, s)


def test_relationship_endpoint_rules_enforced_in_db(session):
    s, f1, f2 = sup(), fac("F1"), fac("F2")
    session.add_all([s, f1, f2])
    session.commit()
    base = {"provenance": P, "target_facility_id": f1.id}
    # SUPPLIES without supplier
    commit_fails(
        session, SupplyRelationship(relationship_code="R1", relationship_type=RelationshipType.SUPPLIES, **base)
    )
    # SUPPLIES with a source facility too
    commit_fails(
        session,
        SupplyRelationship(
            relationship_code="R2",
            relationship_type=RelationshipType.SUPPLIES,
            supplier_id=s.id,
            source_facility_id=f2.id,
            **base,
        ),
    )
    # TRANSFERS_TO from a supplier
    commit_fails(
        session,
        SupplyRelationship(
            relationship_code="R3", relationship_type=RelationshipType.TRANSFERS_TO, supplier_id=s.id, **base
        ),
    )
    # facility transferring to itself
    commit_fails(
        session,
        SupplyRelationship(
            relationship_code="R4", relationship_type=RelationshipType.TRANSFERS_TO, source_facility_id=f1.id, **base
        ),
    )
    # valid
    session.add(
        SupplyRelationship(
            relationship_code="R5", relationship_type=RelationshipType.TRANSFERS_TO, source_facility_id=f2.id, **base
        )
    )
    session.commit()


def test_relationship_requires_existing_endpoints_fk(session):
    commit_fails(
        session,
        SupplyRelationship(
            relationship_code="R1",
            relationship_type=RelationshipType.SUPPLIES,
            supplier_id=999,
            target_facility_id=998,
            provenance=P,
        ),
    )


def test_capacity_pair_and_lead_time_constraints(session):
    s, f = sup(), fac()
    session.add_all([s, f])
    session.commit()
    common = {
        "relationship_type": RelationshipType.SUPPLIES,
        "supplier_id": s.id,
        "target_facility_id": f.id,
        "provenance": P,
    }
    commit_fails(session, SupplyRelationship(relationship_code="A", capacity_value=5, **common))
    commit_fails(session, SupplyRelationship(relationship_code="B", capacity_value=-5, capacity_unit="u", **common))
    commit_fails(session, SupplyRelationship(relationship_code="C", lead_time_days=-1, **common))


def test_observation_subject_unit_and_timestamp_rules(session):
    s, f = sup(), fac()
    session.add_all([s, f])
    session.commit()
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ok = {"metric_name": "m", "value": 1.0, "unit": "u", "observed_at": ts, "source_label": "x", "provenance": P}
    commit_fails(session, Observation(observation_code="O1", **ok))  # no subject
    commit_fails(session, Observation(observation_code="O2", supplier_id=s.id, facility_id=f.id, **ok))  # two subjects
    commit_fails(session, Observation(observation_code="O3", supplier_id=s.id, **{**ok, "unit": " "}))  # blank unit
    session.add(Observation(observation_code="O4", supplier_id=s.id, **ok))
    session.commit()


def test_naive_datetime_rejected_and_roundtrip_is_aware_utc(session):
    s = sup()
    session.add(s)
    session.commit()
    base = {"metric_name": "m", "value": 1.0, "unit": "u", "source_label": "x", "provenance": P, "supplier_id": s.id}
    session.add(Observation(observation_code="N1", observed_at=datetime(2026, 1, 1), **base))
    with pytest.raises(StatementError):
        session.commit()
    session.rollback()
    ist = timezone(timedelta(hours=5, minutes=30))
    session.add(Observation(observation_code="N2", observed_at=datetime(2026, 1, 1, 5, 30, tzinfo=ist), **base))
    session.commit()
    session.expire_all()
    got = session.scalar(select(Observation).where(Observation.observation_code == "N2"))
    assert got.observed_at == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert got.observed_at.utcoffset() == timedelta(0)
    assert got.created_at.tzinfo is not None


def test_enum_values_constrained_in_db(session):
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO suppliers (supplier_code,name,country_or_region,category,status,"
                "provenance,created_at,updated_at) "
                "VALUES ('X','n','r','c','ACTIVE','NOT_A_PROVENANCE','2026-01-01','2026-01-01')"
            )
        )
    session.rollback()
