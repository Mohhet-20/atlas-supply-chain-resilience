from __future__ import annotations

import json

from sqlalchemy import event

from tests.quality_helpers import Q, db_supplier, evaluate, facility_with_coords, findings


def _count_queries(client, fn):
    n = {"q": 0}

    def bump(_conn, _cur, statement, *_a, **_k):
        if statement.lstrip().upper().startswith("SELECT"):
            n["q"] += 1

    eng = client.app.state.engine
    event.listen(eng, "before_cursor_execute", bump)
    try:
        fn()
    finally:
        event.remove(eng, "before_cursor_execute", bump)
    return n["q"]


def test_findings_list_query_count_does_not_grow_with_rows(client):
    for i in range(3):
        facility_with_coords(client, f"Zero Site {i}", 0.0, 0.0)
    evaluate(client)
    small = _count_queries(client, lambda: client.get(f"{Q}/findings", params={"limit": 100}))
    for i in range(3, 15):
        facility_with_coords(client, f"Zero Site {i}", 0.0, 0.0)
    evaluate(client)
    big = _count_queries(client, lambda: client.get(f"{Q}/findings", params={"limit": 100}))
    assert big <= small + 2, (small, big)


def test_network_evaluation_read_statements_are_chunked_not_per_row(client):
    for i in range(10):
        db_supplier(client, f"Perf Supplier {i}")
    small = _count_queries(client, lambda: evaluate(client))
    for i in range(10, 50):
        db_supplier(client, f"Perf Supplier {i}")
    big = _count_queries(client, lambda: evaluate(client))
    assert big <= small + 4, (small, big)  # reads are chunked; only inserts grow with the output


def test_evidence_and_messages_never_carry_location_or_secrets(client):
    fac = facility_with_coords(client, "Zero Site", 0.0, 0.0, address="12 Hidden Lane Password=hunter2")
    evaluate(client)
    blob = json.dumps(findings(client, entity_type="FACILITY", entity_id=fac["id"]))
    assert "hunter2" not in blob and "Hidden Lane" not in blob
    detail = client.get(f"{Q}/entities/FACILITY/{fac['id']}").text
    assert "hunter2" not in detail


def test_listing_has_no_unbounded_page_and_sort_is_whitelisted(client):
    assert client.get(f"{Q}/findings", params={"sort": "id; DROP TABLE x"}).status_code == 422
    assert client.get(f"{Q}/findings", params={"limit": 0}).status_code == 422
    assert client.get(f"{Q}/findings", params={"offset": -1}).status_code == 422
