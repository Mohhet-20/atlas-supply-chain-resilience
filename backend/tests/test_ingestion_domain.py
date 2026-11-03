"""Relationship / product-link ingestion, reference resolution and domain constraints."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ingestion import service
from app.models.enums import BatchStatus, EntityKind, IngestPolicy
from app.models.facility import Facility
from app.models.ingestion import DataQualityFinding
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from tests.ingest_helpers import count, new_source, req

MB = 5000
H = "external_id,name,country_or_region,category\n"


def load(session, src, data, entity, **kw):
    return service.commit(session, req(src.id, data, entity=entity, **kw), max_rows=MB, expected_digest=None).batch


def base_network(session):
    src = new_source(session)
    load(session, src, H + "S-1,Acme,DE,Parts\nS-2,Bolt,MX,Metal\n", EntityKind.SUPPLIER)
    load(
        session,
        src,
        "external_id,name,facility_type,country_or_region,latitude,longitude\n"
        "F-1,Plant 1,manufacturing,DE,52.5,13.4\nF-2,DC 1,distribution,DE,,\n",
        EntityKind.FACILITY,
    )
    load(session, src, "external_id,name,category,unit_of_measure\nP-1,Widget,Parts,EA\n", EntityKind.PRODUCT)
    return src


def test_facility_coordinates_are_never_fabricated(session):
    base_network(session)
    f1, f2 = (session.scalar(select(Facility).where(Facility.external_id == e)) for e in ("F-1", "F-2"))
    assert (f1.latitude, f1.longitude, f1.location_status.value) == (52.5, 13.4, "UNVERIFIED")
    assert (f2.latitude, f2.longitude, f2.location_status.value) == (None, None, "UNKNOWN")


def test_facility_rejects_half_a_coordinate_and_out_of_range(session):
    src = new_source(session)
    d = "external_id,name,facility_type,country_or_region,latitude,longitude\nF-1,A,WAREHOUSE,DE,10,\nF-2,B,WAREHOUSE,DE,91,0\n"  # noqa: E501
    b = load(session, src, d, EntityKind.FACILITY)
    assert b.status is BatchStatus.REJECTED
    rules = sorted(
        f.rule_id
        for f in session.scalars(select(DataQualityFinding).where(DataQualityFinding.batch_id == b.id))
        if f.severity.value == "ERROR"
    )
    assert rules == ["DQ007", "DQ017"]


def test_relationships_resolve_refs_and_enforce_endpoint_rules(session):
    src = base_network(session)
    data = (
        "external_id,relationship_type,supplier_ref,source_facility_ref,target_facility_ref,lead_time_days\n"
        "R-1,SUPPLIES,S-1,,F-1,12\nR-2,TRANSFERS_TO,,F-1,F-2,0\n"
        "R-3,SUPPLIES,S-9,,F-1,1\nR-4,SUPPLIES,S-1,F-2,F-1,1\nR-5,TRANSFERS_TO,,F-1,F-1,1\nR-6,SUPPLIES,S-1,,F-1,-1\n"
    )
    b = load(session, src, data, EntityKind.RELATIONSHIP)
    assert (b.created_count, b.rejected_count) == (2, 4) and b.status is BatchStatus.COMPLETED_WITH_ERRORS
    r2 = session.scalar(select(SupplyRelationship).where(SupplyRelationship.external_id == "R-2"))
    assert r2.lead_time_days == 0.0  # zero is a real value, not unknown
    by_row = {
        f.row_index: f.rule_id
        for f in session.scalars(
            select(DataQualityFinding).where(
                DataQualityFinding.batch_id == b.id, DataQualityFinding.severity == "ERROR"
            )
        )
    }
    assert by_row[3] == "DQ009" and by_row[4] == "DQ012" and by_row[5] == "DQ017" and by_row[6] in ("DQ006", "DQ015")


def test_duplicate_relationship_with_new_external_id_is_flagged(session):
    src = base_network(session)
    h = "external_id,relationship_type,supplier_ref,target_facility_ref\n"
    load(session, src, h + "R-1,SUPPLIES,S-1,F-1\n", EntityKind.RELATIONSHIP)
    b = load(
        session,
        src,
        h + "R-OTHER,SUPPLIES,S-1,F-1\nR-2,SUPPLIES,S-2,F-1\nR-3,SUPPLIES,S-2,F-1\n",
        EntityKind.RELATIONSHIP,
    )
    assert (b.created_count, b.rejected_count) == (1, 2)
    assert count(session, SupplyRelationship) == 2


def test_relationship_endpoints_are_immutable_but_attributes_update(session):
    src = base_network(session)
    h = "external_id,relationship_type,supplier_ref,target_facility_ref,lead_time_days\n"
    load(session, src, h + "R-1,SUPPLIES,S-1,F-1,5\n", EntityKind.RELATIONSHIP)
    moved = load(session, src, h + "R-1,SUPPLIES,S-2,F-1,5\n", EntityKind.RELATIONSHIP)
    assert moved.rejected_count == 1
    upd = load(session, src, h + "R-1,SUPPLIES,S-1,F-1,9\n", EntityKind.RELATIONSHIP)
    assert upd.updated_count == 1
    assert session.scalar(select(SupplyRelationship.lead_time_days)) == 9.0


def test_product_links_and_cross_namespace_refs(session):
    a = base_network(session)
    b = new_source(session, "wms-b")
    h = "external_id,link_type,product_ref,supplier_ref,facility_ref,quantity_value,quantity_unit,effective_from,effective_to\n"  # noqa: E501
    data = (
        h + "L-1,SUPPLIES_PRODUCT,P-1,S-1,,100,EA,2026-01-01,2026-12-31\n"
        "L-2,PRODUCES,P-1,,F-1,,,,\nL-3,STORES,erp-a:P-1,,erp-a:F-2,0,EA,,\n"
        "L-4,PRODUCES,P-1,S-1,F-1,,,,\nL-5,STORES,P-1,,F-1,5,,,\nL-6,STORES,P-1,,F-2,,,2026-05-01,2026-04-01\n"
    )
    batch = load(session, b, data, EntityKind.PRODUCT_LINK)
    # bare refs resolve inside 'wms-b' (nothing there); only L-3 carries explicit 'erp-a:' refs
    assert batch.created_count == 1
    cross = (
        data.replace(",P-1,", ",erp-a:P-1,")
        .replace(",S-1,", ",erp-a:S-1,")
        .replace(",,F-", ",,erp-a:F-")
        .replace(",F-2,", ",erp-a:F-2,")
    )
    batch = load(session, b, cross, EntityKind.PRODUCT_LINK)
    assert (batch.created_count, batch.unchanged_count, batch.rejected_count) == (2, 1, 3)
    link = session.scalar(select(ProductLink).where(ProductLink.external_id == "L-3"))
    assert link.quantity_value == 0.0 and link.source.namespace == "wms-b" and link.product.source.namespace == "erp-a"
    assert a.id != b.id


def test_external_ids_are_case_sensitive_with_a_candidate_warning(session):
    src = new_source(session)
    b = load(session, src, H + "abc,Lower,DE,X\nABC,Upper,DE,X\n", EntityKind.SUPPLIER)
    assert b.created_count == 2
    assert any(f.rule_id == "DQ011" and "letter case" in f.message for f in session.scalars(select(DataQualityFinding)))


def test_unmapped_unknown_policy_reject_blocks_batch(session):
    src = new_source(session)
    d = {
        "columns": {k: {"source": k} for k in ("external_id", "name", "country_or_region", "category")},
        "unknown_field_policy": "reject",
    }
    p = service.create_profile(
        session,
        name="strict",
        entity=EntityKind.SUPPLIER,
        fmt=service.SourceFormat.ANY,
        source_id=None,
        definition=d,
        description=None,
    )
    service.set_profile_status(session, p.id, service.ProfileStatus.ACTIVE)
    b = load(
        session,
        src,
        H.strip() + ",extra\nS-1,A,DE,X,1\n",
        EntityKind.SUPPLIER,
        profile_id=p.id,
        policy=IngestPolicy.ATOMIC,
    )
    assert b.status is BatchStatus.REJECTED and count(session, Supplier) == 0


def test_schema_constraints_reject_invalid_rows_directly(session):
    src = new_source(session)
    with pytest.raises(IntegrityError):
        with session.begin():
            session.add(
                ProductLink(
                    link_code="X",
                    link_type="PRODUCES",
                    product_id=1,
                    supplier_id=1,
                    facility_id=1,
                    provenance="USER_PROVIDED",
                )
            )
            session.flush()
    session.rollback()
    with pytest.raises(IntegrityError):  # half a source pair
        with session.begin():
            session.add(
                Supplier(
                    supplier_code="Q",
                    name="n",
                    country_or_region="c",
                    category="k",
                    status="ACTIVE",
                    provenance="USER_PROVIDED",
                    source_id=src.id,
                )
            )
            session.flush()
    session.rollback()
    with session.begin():
        session.add_all(
            [
                Product(
                    product_code="a:1",
                    name="n",
                    status="ACTIVE",
                    provenance="IMPORTED_EXTERNAL",
                    source_id=src.id,
                    external_id="1",
                )
            ]
        )
    with pytest.raises(IntegrityError):  # (source, external id) unique
        with session.begin():
            session.add(
                Product(
                    product_code="a:2",
                    name="n",
                    status="ACTIVE",
                    provenance="IMPORTED_EXTERNAL",
                    source_id=src.id,
                    external_id="1",
                )
            )
            session.flush()
    session.rollback()


def test_upgrade_from_0001_preserves_and_backfills_existing_rows(tmp_path):
    from alembic import command
    from app.db.migrations_check import alembic_config

    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0001")
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute(
        "INSERT INTO suppliers (supplier_code,name,country_or_region,category,status,provenance,created_at,updated_at) "
        "VALUES ('DEMO-SUP-001','S','X','C','ACTIVE','SYNTHETIC_DEMO','2026-01-01','2026-01-01'),"
        "('MY-1','Mine','X','C','ACTIVE','USER_PROVIDED','2026-01-01','2026-01-01')"
    )
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(tmp_path / "old.db")
    rows = dict((r[0], r[1:]) for r in con.execute("SELECT supplier_code, source_id, external_id FROM suppliers"))
    assert rows["DEMO-SUP-001"][1] == "DEMO-SUP-001" and rows["DEMO-SUP-001"][0] is not None
    assert rows["MY-1"] == (None, None)  # user records are not assigned to a source
    assert con.execute("SELECT namespace FROM data_sources").fetchall() == [("atlas-demo",)]
    con.close()
