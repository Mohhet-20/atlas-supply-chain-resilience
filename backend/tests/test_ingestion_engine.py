from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import StalePreviewError
from app.ingestion import service
from app.models.enums import BatchStatus, EntityKind, IngestPolicy
from app.models.ingestion import DataQualityFinding, IngestionBatch, RecordLineage
from app.models.supplier import Supplier
from tests.ingest_helpers import SUPPLIER_CSV, count, new_source, req

MB = 5000


def test_preview_persists_nothing_and_matches_commit(session):
    src = new_source(session)
    r = req(src.id, SUPPLIER_CSV)
    pre = service.preview(session, r, max_rows=MB)
    assert pre.plan.counts()["to_create"] == 3 and not pre.plan.has_blocking
    assert count(session, Supplier) == 0 and count(session, IngestionBatch) == 0
    res = service.commit(session, r, max_rows=MB, expected_digest=pre.plan.plan_digest)
    assert res.batch.status is BatchStatus.COMMITTED and res.batch.created_count == 3 and not res.replay
    sup = session.scalar(select(Supplier).where(Supplier.external_id == "S-3"))
    assert sup.supplier_code == "erp-a:S-3" and sup.source.namespace == "erp-a"
    assert sup.status.value == "ACTIVE"  # blank status = unknown -> default only on create


def test_commit_is_idempotent_and_retry_safe(session):
    src = new_source(session)
    r = req(src.id, SUPPLIER_CSV)
    first = service.commit(session, r, max_rows=MB, expected_digest=None)
    again = service.commit(session, r, max_rows=MB, expected_digest=None)
    assert again.replay and again.batch.id == first.batch.id
    assert count(session, Supplier) == 3 and count(session, IngestionBatch) == 1


def test_same_external_id_in_two_namespaces_never_merges(session):
    a, b = new_source(session, "erp-a"), new_source(session, "erp-b")
    service.commit(session, req(a.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None)
    service.commit(session, req(b.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None)
    assert count(session, Supplier) == 6
    codes = set(session.scalars(select(Supplier.supplier_code)))
    assert {"erp-a:S-1", "erp-b:S-1"} <= codes


def test_update_changes_only_provided_fields_and_empty_means_unknown(session):
    src = new_source(session)
    service.commit(session, req(src.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None)
    upd = "external_id,name,country_or_region,category,status\nS-1,Acme Parts GmbH,,,\n"
    res = service.commit(session, req(src.id, upd), max_rows=MB, expected_digest=None)
    assert res.batch.updated_count == 1
    s1 = session.scalar(select(Supplier).where(Supplier.external_id == "S-1"))
    assert s1.name == "Acme Parts GmbH" and s1.country_or_region == "Germany"  # blank cells did not erase data
    lin = session.scalars(select(RecordLineage).where(RecordLineage.entity_id == s1.id)).all()
    assert len(lin) == 2 and lin[-1].changed_fields == ["name"]


def test_atomic_rejects_whole_batch_but_keeps_findings(session):
    src = new_source(session)
    bad = SUPPLIER_CSV + "S-4,,Chile,Metal,ACTIVE\n"
    res = service.commit(session, req(src.id, bad, policy=IngestPolicy.ATOMIC), max_rows=MB, expected_digest=None)
    b = res.batch
    assert b.status is BatchStatus.REJECTED and b.created_count == 0 and b.skipped_count == 3 and b.rejected_count == 1
    assert count(session, Supplier) == 0
    f = session.scalars(select(DataQualityFinding).where(DataQualityFinding.batch_id == b.id)).all()
    errors = [x for x in f if x.severity.value == "ERROR"]
    assert [x.rule_id for x in errors] == ["DQ001"] and errors[0].row_index == 4 and errors[0].source_line == 5
    assert {x.rule_id for x in f} == {"DQ001", "DQ020"}  # optional absent column is INFO, never an error


def test_partial_commits_valid_rows_and_counts_separately(session):
    src = new_source(session)
    bad = SUPPLIER_CSV + "S-4,,Chile,Metal,ACTIVE\nS-1,Dup,Germany,Electronics,ACTIVE\nS 5,Bad id,X,Y,ACTIVE\n"
    b = service.commit(session, req(src.id, bad), max_rows=MB, expected_digest=None).batch
    assert b.status is BatchStatus.COMPLETED_WITH_ERRORS
    assert (b.created_count, b.rejected_count, b.duplicate_count, b.skipped_count) == (3, 2, 1, 0)
    assert count(session, Supplier) == 3


def test_stale_preview_is_rejected(session):
    src = new_source(session)
    r = req(src.id, SUPPLIER_CSV)
    pre = service.preview(session, r, max_rows=MB)
    service.commit(
        session,
        req(src.id, "external_id,name,country_or_region,category\nS-9,N,C,K\n"),
        max_rows=MB,
        expected_digest=None,
    )
    # change registry so the plan differs: S-1 now exists
    service.commit(
        session,
        req(src.id, "external_id,name,country_or_region,category\nS-1,Other,C,K\n"),
        max_rows=MB,
        expected_digest=None,
    )
    with pytest.raises(StalePreviewError):
        service.commit(session, r, max_rows=MB, expected_digest=pre.plan.plan_digest)
    assert count(session, IngestionBatch) == 2


def test_duplicate_candidate_by_name_is_a_warning_not_a_merge(session):
    a, b = new_source(session, "erp-a"), new_source(session, "erp-b")
    service.commit(session, req(a.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None)
    pre = service.preview(
        session,
        req(b.id, "external_id,name,country_or_region,category\nX1,ACME PARTS,Germany,Electronics\n"),
        max_rows=MB,
    )
    assert pre.plan.rows[0].action == "CREATE"
    assert [f.rule_id for f in pre.plan.all_findings() if f.severity.value != "INFO"] == ["DQ011"]


def test_unknown_columns_policy_and_unit_conversion(session):
    src = new_source(session)
    prof = service.create_profile(
        session,
        name="fac profile",
        entity=EntityKind.FACILITY,
        fmt=service.SourceFormat.CSV,
        source_id=src.id,
        description=None,
        definition={
            "columns": {
                "external_id": {"source": "Site"},
                "name": {"source": "Site Name"},
                "facility_type": {"source": "Kind", "value_map": {"plant": "MANUFACTURING"}},
                "country_or_region": {"source": "Country"},
                "capacity_value": {"source": "Cap"},
                "capacity_unit": {"source": "Unit"},
            },
            "unknown_field_policy": "warn",
            "unit_rules": {
                "capacity_value": {
                    "unit_field": "capacity_unit",
                    "target_unit": "UNITS/WEEK",
                    "factors": {"UNITS/WEEK": 1, "UNITS/DAY": 7},
                }
            },
        },
    )
    service.set_profile_status(session, prof.id, service.ProfileStatus.ACTIVE)
    data = "Site,Site Name,Kind,Country,Cap,Unit,Extra\nF1,North Plant,plant,Spain,100,units/day,zzz\nF2,Bad,plant,Spain,5,boxes,\n"  # noqa: E501
    b = service.commit(
        session, req(src.id, data, entity=EntityKind.FACILITY, profile_id=prof.id), max_rows=MB, expected_digest=None
    ).batch
    assert b.created_count == 1 and b.rejected_count == 1
    from app.models.facility import Facility

    f1 = session.scalar(select(Facility).where(Facility.external_id == "F1"))
    assert f1.capacity_value == 700.0 and f1.capacity_unit == "UNITS/WEEK"
    rules = {f.rule_id for f in session.scalars(select(DataQualityFinding).where(DataQualityFinding.batch_id == b.id))}
    assert {"DQ008", "DQ016"} <= rules
