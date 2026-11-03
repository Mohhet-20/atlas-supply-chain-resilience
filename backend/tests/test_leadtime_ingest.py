"""Delivery import: validation, provenance, idempotency, chronology, write-once original promise. SYNTHETIC data only."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.leadtime.resolve import load_deliveries
from app.models.delivery import DeliveryEvent, DeliveryImportKind, DeliveryImportRow, DeliverySnapshot
from tests.leadtime_helpers import (  # noqa: F401
    NOW,
    code_map,
    csv_bytes,
    do_import,
    gen_rows,
    iso,
    load_world_deliveries,
    remap,
    snapshots,
    special_rows,
    world,
)

D, E = DeliveryImportKind.DELIVERIES, DeliveryImportKind.EVENTS


def one(client, **over):
    w = world(client)
    cm = code_map(w)
    r = remap(gen_rows(1, 1), cm)[0]
    r.update(over)
    return w, cm, r


def test_valid_import_preserves_provenance_and_counts(client):
    _w, _cm, rows = load_world_deliveries(client, 30, special=False)
    snaps = snapshots(client)
    assert len(snaps) == 30
    assert all(s.provenance.value == "SYNTHETIC_DEMO" and s.source_label == "synthetic" and s.batch_id for s in snaps)


def test_reimport_is_idempotent_by_fingerprint_and_by_record(client):
    w = world(client)
    cm = code_map(w)
    data = csv_bytes(remap(gen_rows(10, 2), cm))
    b1, c1 = do_import(client, D, data)
    b2, c2 = do_import(client, D, data)
    assert c1 and not c2 and b1.id == b2.id
    data2 = csv_bytes(remap(gen_rows(10, 2), cm) + remap(gen_rows(2, 3, start_ref=50), cm))
    b3, c3 = do_import(client, D, data2)  # new file, ten identical rows, two new
    assert c3 and b3.rows_duplicate == 10 and b3.rows_accepted == 2
    assert len(snapshots(client)) == 12


def test_row_level_errors_never_coerce(client):
    w = world(client)
    cm = code_map(w)
    good = remap(gen_rows(1, 1), cm)[0]
    bad_ts = {**good, "delivery_ref": "X1", "arrived_at": "not-a-date"}
    unk_sup = {**good, "delivery_ref": "X2", "supplier_code": "NOPE"}
    unk_fac = {**good, "delivery_ref": "X3", "destination_facility_code": "NOPE"}
    chrono = {**good, "delivery_ref": "X4", "arrived_at": iso(NOW - timedelta(days=500)), "received_at": None}
    cancelled = {**good, "delivery_ref": "X5", "status": "CANCELLED"}
    neg = {**good, "delivery_ref": "X6", "quantity": -5}
    bad_status = {**good, "delivery_ref": "X7", "status": "TELEPORTED"}
    batch, _ = do_import(client, D, csv_bytes([good, bad_ts, unk_sup, unk_fac, chrono, cancelled, neg, bad_status]))
    codes = batch.summary["error_codes"]
    assert batch.rows_accepted == 1 and batch.rows_rejected == 7
    for c in (
        "INVALID_TIMESTAMP",
        "UNRESOLVED_SUPPLIER",
        "UNRESOLVED_FACILITY",
        "CHRONOLOGY_VIOLATION",
        "CANCELLED_WITH_ARRIVAL",
        "NON_POSITIVE_QUANTITY",
        "INVALID_STATUS",
    ):
        assert c in codes, c
    with Session(client.app.state.engine) as s:
        assert s.scalar(select(func.count()).select_from(DeliverySnapshot)) == 1
        assert (
            s.scalar(select(func.count()).select_from(DeliveryImportRow)) == 8
        )  # every row, including rejects, is inspectable


def test_documented_exception_keeps_row_but_flags_it(client):
    _w, _cm, r = one(
        client,
        delivery_ref="EXC",
        arrived_at=iso(NOW - timedelta(days=500)),
        received_at=None,
        exception_note="source clock skew corrected by planner",
    )
    batch, _ = do_import(client, D, csv_bytes([r], cols=[*ingest_cols(), "exception_note"]))
    assert batch.rows_accepted == 1 and "CHRONOLOGY_EXCEPTION" in batch.summary["warning_codes"]


def ingest_cols():
    from tests.leadtime_helpers import COLS

    return COLS


def test_original_promise_is_write_once(client):
    _w, cm, r = one(
        client,
        delivery_ref="OP1",
        arrived_at=None,
        received_at=None,
        status="CONFIRMED",
        observed_at=iso(NOW - timedelta(days=200)),
    )
    do_import(client, D, csv_bytes([r]))
    changed = {
        **r,
        "original_promised_at": iso(NOW + timedelta(days=30)),
        "observed_at": iso(NOW - timedelta(days=100)),
    }
    batch, _ = do_import(client, D, csv_bytes([changed]))
    assert batch.rows_rejected == 1 and "ORIGINAL_PROMISE_CHANGED" in batch.summary["error_codes"]
    revised = {**r, "revised_promised_at": iso(NOW + timedelta(days=30)), "observed_at": iso(NOW - timedelta(days=100))}
    b2, _ = do_import(client, D, csv_bytes([revised]))
    assert b2.rows_accepted == 1
    with Session(client.app.state.engine) as s:
        d = load_deliveries(s)[0]
    assert d.original_promised_at.isoformat().startswith(r["original_promised_at"][:10])  # original kept
    assert d.latest_promised_at is not None and d.latest_promised_at != d.original_promised_at


def test_identity_conflict_not_silently_reattached(client):
    _w, cm, r = one(client, delivery_ref="ID1", arrived_at=None, received_at=None, status="CONFIRMED")
    do_import(client, D, csv_bytes([r]))
    other = {**r, "supplier_code": cm["S2"], "observed_at": iso(NOW)}
    batch, _ = do_import(client, D, csv_bytes([other]))
    assert batch.rows_rejected == 1 and "IDENTITY_CONFLICT" in batch.summary["error_codes"]


def test_naive_timestamps_use_batch_timezone_and_are_flagged(client):
    w = world(client)
    cm = code_map(w)
    r = remap(gen_rows(1, 1), cm)[0]
    r.update(order_at="2026-03-01T09:00:00", confirmed_at=None, dispatched_at=None, promised_dispatch_at=None, original_promised_at="2026-03-10T09:00:00", arrived_at="2026-03-09T09:00:00", received_at="2026-03-09T10:00:00", observed_at="2026-03-09T11:00:00")  # fmt: skip
    batch, _ = do_import(client, D, csv_bytes([r]), tz="Asia/Kolkata")
    assert "TIMESTAMP_ASSUMED_TZ" in batch.summary["warning_codes"]
    s = snapshots(client)[0]
    assert s.order_at.isoformat().startswith("2026-03-01T03:30")  # 09:00 IST == 03:30 UTC


def test_unknown_timezone_is_a_clear_error(client):
    import pytest

    from app.core.errors import DomainValidationError

    w = world(client)
    cm = code_map(w)
    with pytest.raises(DomainValidationError, match="time zone"):
        do_import(client, D, csv_bytes(remap(gen_rows(2, 1), cm)), tz="Mars/Olympus")


def test_events_are_deduplicated_and_never_fabricated(client):
    w, cm, r = one(
        client, delivery_ref="EV1", arrived_at=None, received_at=None, dispatched_at=None, status="CONFIRMED"
    )
    do_import(client, D, csv_bytes([r]))
    ev = [
        {"delivery_ref": "EV1", "event_type": "DISPATCHED", "occurred_at": iso(NOW - timedelta(days=3))},
        {"delivery_ref": "EV1", "event_type": "DISPATCHED", "occurred_at": iso(NOW - timedelta(days=3))},
        {
            "delivery_ref": "EV1",
            "event_type": "PROMISE_REVISED",
            "occurred_at": iso(NOW - timedelta(days=2)),
            "value_at": iso(NOW + timedelta(days=4)),
        },
        {"delivery_ref": "GHOST", "event_type": "ARRIVED", "occurred_at": iso(NOW - timedelta(days=1))},
        {"delivery_ref": "EV1", "event_type": "WARP", "occurred_at": iso(NOW)},
    ]
    batch, _ = do_import(client, E, csv_bytes(ev, ["delivery_ref", "event_type", "occurred_at", "value_at"]))
    assert batch.rows_accepted == 3 and batch.rows_duplicate == 1 and batch.rows_rejected == 1
    assert "ORPHAN_EVENT" in batch.summary["warning_codes"]
    with Session(client.app.state.engine) as s:
        assert s.scalar(select(func.count()).select_from(DeliveryEvent)) == 3
        d = next(x for x in load_deliveries(s) if x.ref == "EV1")
    assert d.dispatched_at is not None and d.latest_promised_at is not None
    assert d.arrived_at is None  # nothing was invented from the other facts


def test_records_are_insert_only(client):
    import pytest

    load_world_deliveries(client, 5, special=False)
    with Session(client.app.state.engine) as s:
        row = s.scalar(select(DeliverySnapshot))
        row.quantity = 1.0
        with pytest.raises(Exception):  # noqa: B017
            s.commit()
