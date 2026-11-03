"""Failure handling, concurrency and security properties of the ingestion pipeline."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ingestion import commit as commit_mod
from app.ingestion import service
from app.ingestion.commit import sanitize_file_name
from app.models.enums import BatchStatus, EntityKind, IngestPolicy
from app.models.ingestion import DataQualityFinding, IngestionBatch, IngestionRowResult, RecordLineage
from app.models.supplier import Supplier
from tests.ingest_helpers import SUPPLIER_CSV, count, new_source, req

MB = 5000


def fail_on(monkeypatch, bad_id):
    real = commit_mod._create

    def flaky(session, plan, row):
        if row.external_id == bad_id:
            raise IntegrityError("INSERT", {}, Exception("simulated constraint failure"))
        return real(session, plan, row)

    monkeypatch.setattr(commit_mod, "_create", flaky)


def test_atomic_database_failure_rolls_back_everything_and_reports_failed(session, monkeypatch):
    src = new_source(session)
    fail_on(monkeypatch, "S-3")
    b = service.commit(
        session, req(src.id, SUPPLIER_CSV, policy=IngestPolicy.ATOMIC), max_rows=MB, expected_digest=None
    ).batch
    assert b.status is BatchStatus.FAILED
    assert (b.created_count, b.updated_count, b.skipped_count) == (0, 0, 3)  # nothing is reported as committed
    assert count(session, Supplier) == 0 and count(session, RecordLineage) == 0
    assert b.failure_reason and "rolled back" in b.failure_reason
    assert (
        session.scalar(
            select(DataQualityFinding.rule_id).where(
                DataQualityFinding.batch_id == b.id, DataQualityFinding.severity == "ERROR"
            )
        )
        == "DQ014"
    )
    monkeypatch.undo()
    retry = service.commit(
        session, req(src.id, SUPPLIER_CSV, policy=IngestPolicy.ATOMIC), max_rows=MB, expected_digest=None
    )
    assert retry.batch.status is BatchStatus.COMMITTED and not retry.replay  # a FAILED batch never blocks a retry
    assert count(session, Supplier) == 3


def test_partial_row_failure_rolls_back_only_that_row(session, monkeypatch):
    src = new_source(session)
    fail_on(monkeypatch, "S-2")
    b = service.commit(session, req(src.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None).batch
    assert b.status is BatchStatus.COMPLETED_WITH_ERRORS and (b.created_count, b.rejected_count) == (2, 1)
    assert sorted(session.scalars(select(Supplier.external_id))) == ["S-1", "S-3"]
    bad = session.scalar(select(IngestionRowResult).where(IngestionRowResult.external_id == "S-2"))
    assert bad.outcome.value == "REJECTED" and bad.entity_id is None


def test_losing_a_commit_race_returns_the_winning_batch(session, monkeypatch):
    src = new_source(session)
    winner = service.commit(session, req(src.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None).batch
    real = commit_mod.find_applied_batch
    calls = {"n": 0}

    def blind_first_call(sess, fp):  # simulates the window where the other writer has not committed yet
        calls["n"] += 1
        return None if calls["n"] == 1 else real(sess, fp)

    monkeypatch.setattr(commit_mod, "find_applied_batch", blind_first_call)
    res = service.commit(session, req(src.id, SUPPLIER_CSV), max_rows=MB, expected_digest=None)
    assert res.replay and res.batch.id == winner.id
    assert count(session, Supplier) == 3 and count(session, IngestionBatch) == 1  # loser left no trace


def test_unique_key_race_is_caught_per_row_not_a_crash(session):
    """A record inserted behind the planner's back (same external id) is rejected for that row only."""
    from app.ingestion.planner import build_plan

    src = new_source(session)
    plan = build_plan(
        session,
        source=src,
        entity=EntityKind.SUPPLIER,
        fmt="CSV",
        data=SUPPLIER_CSV.encode(),
        profile=service.builtin_profile(EntityKind.SUPPLIER),
        policy=IngestPolicy.PARTIAL,
        encoding="utf-8",
        delimiter=",",
        file_name=None,
        max_rows=MB,
    )
    session.rollback()
    with session.begin():
        session.add(
            Supplier(
                supplier_code="erp-a:S-2",
                name="Raced",
                country_or_region="X",
                category="Y",
                status="ACTIVE",
                provenance="IMPORTED_EXTERNAL",
                source_id=src.id,
                external_id="S-2",
            )
        )
    with session.begin():
        batch = commit_mod.make_batch(plan)
        session.add(batch)
        session.flush()
        commit_mod.apply_plan(session, plan, batch)
    assert batch.status is BatchStatus.COMPLETED_WITH_ERRORS and (batch.created_count, batch.rejected_count) == (2, 1)
    assert session.scalar(select(Supplier.name).where(Supplier.external_id == "S-2")) == "Raced"  # untouched


def test_every_input_row_is_accounted_for(session):
    src = new_source(session)
    messy = SUPPLIER_CSV + "S-1,Again,DE,X,\nS-4,,DE,X,\nbroken\nS-5,Ok,DE,X,\n"
    b = service.commit(session, req(src.id, messy), max_rows=MB, expected_digest=None).batch
    total = (
        b.created_count + b.updated_count + b.unchanged_count + b.rejected_count + b.skipped_count + b.duplicate_count
    )
    assert total == b.total_rows == 7
    assert count(session, IngestionRowResult) == 7


def test_history_is_preserved_and_findings_are_never_deleted(session):
    src = new_source(session)
    service.commit(session, req(src.id, SUPPLIER_CSV + "S-4,,DE,X,\n"), max_rows=MB, expected_digest=None)
    f = session.scalar(select(DataQualityFinding).where(DataQualityFinding.rule_id == "DQ001"))
    service.resolve_finding(session, f.id, service.ResolutionStatus.RESOLVED, "fixed upstream")
    service.commit(session, req(src.id, SUPPLIER_CSV + "S-4,Fixed,DE,X,\n"), max_rows=MB, expected_digest=None)
    f = session.scalar(select(DataQualityFinding).where(DataQualityFinding.id == f.id))
    assert (
        f.resolution_status.value == "RESOLVED" and f.resolved_at is not None and f.resolution_note == "fixed upstream"
    )
    assert count(session, IngestionBatch) == 2


# ---------------------------------------------------------------- security


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\..\\secret.csv", "secret.csv"),
        ("a\x00b.csv", "ab.csv"),
        ("<script>alert(1)</script>.csv", "script.csv"),
        ("....", None),
        ("", None),
        (None, None),
        ("x" * 300 + ".csv", "x" * 100),
        ("naïve file.csv", "naïve file.csv"),
    ],
)
def test_filenames_are_display_only_and_sanitized(raw, expected):
    assert sanitize_file_name(raw) == expected


def test_hostile_filename_never_touches_the_filesystem(session, tmp_path):
    src = new_source(session)
    b = service.commit(
        session, req(src.id, SUPPLIER_CSV, file_name="../../../../tmp/pwn.csv"), max_rows=MB, expected_digest=None
    ).batch
    assert b.file_name == "pwn.csv"
    assert not (tmp_path.parent / "pwn.csv").exists()


def test_sql_injection_and_xss_payloads_are_stored_as_inert_data(session):
    src = new_source(session)
    payloads = [
        "Robert'); DROP TABLE suppliers;--",
        "<img src=x onerror=alert(1)>",
        "' OR '1'='1",
        "{{7*7}}",
        "${jndi:ldap://x}",
    ]
    csv_rows = "".join(f'S-{i},"{p}",DE,X,\n' for i, p in enumerate(payloads))
    b = service.commit(
        session, req(src.id, SUPPLIER_CSV.split("\n")[0] + "\n" + csv_rows), max_rows=MB, expected_digest=None
    ).batch
    assert b.created_count == 5
    assert set(session.scalars(select(Supplier.name))) == set(payloads)
    assert count(session, Supplier) == 5  # table intact


def test_hostile_identifiers_are_rejected_not_executed(session):
    src = new_source(session)
    ids = ["'; DROP TABLE suppliers;--", "<script>", "../x", "a b", "x:y", "%00", "${x}"]
    rows = "".join(f'"{i}",N,DE,K,\n' for i in ids)
    b = service.commit(
        session, req(src.id, SUPPLIER_CSV.split("\n")[0] + "\n" + rows), max_rows=MB, expected_digest=None
    ).batch
    assert b.created_count == 0 and b.rejected_count == len(ids)


def test_findings_do_not_echo_unbounded_or_binary_values(session):
    src = new_source(session)
    huge = "9" * 5000
    data = SUPPLIER_CSV.split("\n")[0] + f"\nS-1,{huge},DE,K,\n"
    b = service.commit(session, req(src.id, data), max_rows=MB, expected_digest=None).batch
    for f in session.scalars(select(DataQualityFinding).where(DataQualityFinding.batch_id == b.id)):
        assert len(f.message) <= 400 and len(f.observed_value or "") <= 120


def test_cell_contents_are_never_logged(session, caplog):
    src = new_source(session)
    secret = "sk-live-SECRET-TOKEN-123"
    data = SUPPLIER_CSV.split("\n")[0] + f"\nS-1,{secret},DE,K,\n"
    with caplog.at_level("DEBUG"):
        service.commit(session, req(src.id, data + f"S-2,N,DE,K,{secret}\n"), max_rows=MB, expected_digest=None)
    assert secret not in caplog.text  # cell contents never reach logs
    # the offending cell may be kept (truncated) so a reviewer can fix it; it is never logged
    assert all(len(f.observed_value or "") <= 120 for f in session.scalars(select(DataQualityFinding)))


def test_utf8_bom_and_unicode_normalization_do_not_create_lookalike_duplicates(session):
    src = new_source(session)
    composed, decomposed = "Caf\u00e9", "Cafe\u0301"
    data = f"external_id,name,country_or_region,category\nS-1,{composed},DE,K\n"
    service.commit(session, req(src.id, data.encode("utf-8")), max_rows=MB, expected_digest=None)
    again = f"external_id,name,country_or_region,category\nS-1,{decomposed},DE,K\n"
    b = service.commit(session, req(src.id, again.encode("utf-8")), max_rows=MB, expected_digest=None).batch
    assert b.unchanged_count == 1 and count(session, Supplier) == 1  # NFC makes both spellings identical
