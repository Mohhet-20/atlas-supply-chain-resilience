from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from alembic import command
from app.db.migrations_check import alembic_config
from app.main import create_app
from tests.graph_helpers import G, node, ok, rel, world
from tests.registry_helpers import API, make_settings

TOK = {
    "viewer": "v" * 20 + "tokenAAAA",
    "editor": "e" * 20 + "tokenBBBB",
    "reviewer": "r" * 20 + "tokenCCCC",
    "admin": "a" * 20 + "tokenDDDD",
}
TABLE = ",".join(f"{t}:{r}-user:{r}" for r, t in TOK.items())


@pytest.fixture
def secured(settings, engine):
    with TestClient(create_app(make_settings(settings, api_tokens=TABLE), engine)) as c:
        yield c


def h(role):
    return {"Authorization": f"Bearer {TOK[role]}"}


def sql(engine, statement, **params):
    with Session(engine) as s, s.begin():
        s.execute(text(statement), params)


# ------------------------------------------------------------------------------------------------ statistics


def test_summary_counts_are_labelled_and_consistent(client):
    w = world(client)
    ok(client, "SUPPLIES", w["s3"], w["f4"], status="PROPOSED")
    s = client.get(f"{G}/summary").json()
    assert s["nodes"]["live"] == 9 and s["nodes"]["by_type"]["SUPPLIER"] == {"live": 3, "merged": 0}
    rels = s["relationships"]
    assert rels["total"] == 8 and rels["active"] == 7 and rels["by_status"]["PROPOSED"] == 1
    assert rels["by_type"]["SUPPLIES"]["CONFIRMED"] == 2 and rels["by_type"]["TRANSFERS_TO"]["CONFIRMED"] == 2
    assert rels["by_type"]["PRODUCES"]["CONFIRMED"] == 1 and rels["by_type"]["OWNS"]["CONFIRMED"] == 0
    # s3 and f4 only have a proposed edge, so they count as isolated in the *active* network
    assert s["isolated_nodes"]["by_type"] == {"SUPPLIER": 1, "FACILITY": 1, "PRODUCT": 0}
    assert s["components"] == {"computed": True, "count": 1, "largest_sizes": [7]}
    assert "active" in s["counting_rules"] and any("Proposed" in n for n in s["notes"])
    assert {t["type"] for t in s["types"]} >= {"SUPPLIES", "OWNS", "STORES"}


def test_summary_as_of_changes_active_counts(client):
    w = world(client)
    ok(client, "SUPPLIES", w["s3"], w["f4"], effective_to="2020-01-01")
    assert client.get(f"{G}/summary").json()["relationships"]["expired_or_future_confirmed"] == 1
    past = client.get(f"{G}/summary", params={"as_of": "2019-01-01"}).json()
    assert past["relationships"]["active"] == 8 and past["isolated_nodes"]["by_type"]["SUPPLIER"] == 0


def test_summary_on_seeded_demo_data(client, seeded):
    s = client.get(f"{G}/summary").json()
    assert s["relationships"]["total"] == s["relationships"]["by_status"]["CONFIRMED"] > 0
    assert s["nodes"]["live"] == sum(v["live"] for v in s["nodes"]["by_type"].values())


# ------------------------------------------------------------------------------------------------ integrity


def codes(client):
    r = client.get(f"{G}/integrity")
    assert r.status_code == 200, r.text
    return r.json()


def test_clean_graph_has_no_issues_and_integrity_is_read_only(client, engine):
    world(client)
    before = client.get(f"{G}/relationships").json()
    r = codes(client)
    assert r["issue_count"] == 0 and r["scanned_relationships"] == 7 and r["scan_truncated"] is False
    assert {c["code"] for c in r["checks"]} >= {"GRP001", "GRP005", "GRP009"} and "nothing is deleted" in r[
        "remediation"
    ]
    assert client.get(f"{G}/relationships").json() == before


def test_transfer_cycle_is_a_warning_not_an_error(client):
    w = world(client)
    ok(client, "TRANSFERS_TO", w["f3"], w["f1"])
    r = codes(client)
    cyc = [i for i in r["issues"] if i["code"] == "GRP005"]
    assert (
        len(cyc) == 1
        and cyc[0]["severity"] == "WARNING"
        and len(cyc[0]["nodes"]) == 3
        and len(cyc[0]["relationship_refs"]) == 3
    )
    assert r["by_severity"].get("ERROR", 0) == 0 and r["acyclic_policy"]["TRANSFERS_TO"] == "allowed"


def test_contradictions_inactive_endpoints_and_expired_edges_are_flagged(client, engine):
    w = world(client)
    expired = ok(client, "SUPPLIES", w["s3"], w["f4"], effective_to="2020-01-01")
    sql(engine, "UPDATE suppliers SET status = 'INACTIVE' WHERE id = :i", i=w["s2"]["id"])
    sql(engine, "UPDATE supply_relationships SET provenance = 'MODEL_ESTIMATE' WHERE supplier_id = :i", i=w["s1"]["id"])
    r = codes(client)
    by = {}
    for i in r["issues"]:
        by.setdefault(i["code"], []).append(i)
    assert any(expired["ref"] in i["relationship_refs"] for i in by["GRP009"])
    assert any("MODEL_ESTIMATE" in i["message"] for i in by["GRP009"])
    assert any(i["nodes"] == [{"type": "SUPPLIER", "id": w["s2"]["id"]}] for i in by["GRP007"])
    assert all(i["recommendation"] for i in r["issues"])


@pytest.mark.sqlite_only  # plants inconsistent rows through a raw sqlite3 connection
def test_merged_endpoint_and_duplicate_and_dangling_are_errors(client, engine):
    w = world(client)
    sql(engine, "UPDATE suppliers SET merged_into_id = :to, merged_at = '2026-01-01 00:00:00' WHERE id = :m",
        to=w["s1"]["id"], m=w["s2"]["id"])  # fmt: skip
    sql(engine, "DROP INDEX uq_supply_relationships_supplier_pair")
    sql(engine, "INSERT INTO supply_relationships (relationship_code, relationship_type, supplier_id, target_facility_id, "
                "provenance, created_at, status, version, updated_at) VALUES ('DUP-1','SUPPLIES',:s,:f,'USER_PROVIDED',"
                "'2026-01-01 00:00:00','CONFIRMED',1,'2026-01-01 00:00:00')", s=w["s1"]["id"], f=w["f1"]["id"])  # fmt: skip
    con = sqlite3.connect(engine.url.database)  # foreign keys are off by default on a raw connection
    con.execute(
        "INSERT INTO supply_relationships (relationship_code, relationship_type, supplier_id, target_facility_id, "
        "provenance, created_at, status, version, updated_at) VALUES ('DANGLE','SUPPLIES',777,?,'USER_PROVIDED',"
        "'2026-01-01 00:00:00','CONFIRMED',1,'2026-01-01 00:00:00')", (w["f2"]["id"],),
    )  # fmt: skip
    con.commit()
    con.close()
    r = codes(client)
    assert {"GRP001", "GRP003", "GRP006"} <= set(r["by_code"]) and r["by_severity"]["ERROR"] >= 3
    dup = next(i for i in r["issues"] if i["code"] == "GRP003")
    assert len(dup["relationship_refs"]) == 2


def test_database_constraints_block_invalid_rows_directly(client, engine):
    w = world(client)
    bad = [
        "UPDATE supply_relationships SET effective_from='2026-05-01', effective_to='2026-01-01' WHERE id=1",
        "UPDATE supply_relationships SET confidence = 3 WHERE id=1",
        "UPDATE supply_relationships SET supply_share_pct = -5 WHERE id=1",
        "UPDATE supply_relationships SET status = 'MAYBE' WHERE id=1",
        "UPDATE product_links SET confidence = -1 WHERE id=1",
    ]
    assert w
    for stmt in bad:
        with Session(engine) as s, pytest.raises(Exception):  # noqa: B017, PT011
            s.execute(text(stmt))
            s.commit()


# --------------------------------------------------------------------------------------------- auth/audit


def test_unauthenticated_and_viewer_permissions(secured):
    assert secured.get(f"{G}/summary").status_code == 401
    assert secured.get(f"{G}/summary", headers={"Authorization": "Bearer nope-nope-nope-nope"}).status_code == 401
    assert secured.get(f"{G}/summary", headers=h("viewer")).status_code == 200
    assert secured.get(f"{G}/expand", params={"start_type": "SUPPLIER", "start_id": 1}).status_code == 401
    body = {"type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": 1, "destination_type": "FACILITY",
            "destination_id": 1, "justification": "evidence"}  # fmt: skip
    r = secured.post(f"{G}/relationships", json=body, headers=h("viewer"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert secured.get(f"{G}/integrity", headers=h("viewer")).status_code == 403


def test_role_matrix_for_creating_confirming_and_reading_integrity(secured):
    def mkn(kind, name):
        extra = {"facility_type": "MANUFACTURING"} if kind == "facilities" else {"category": "x"}
        r = secured.post(f"{API}/{kind}", json={"name": name, "country_or_region": "DE"} | extra, headers=h("editor"))
        assert r.status_code == 201, r.text
        return r.json() | {"type": {"suppliers": "SUPPLIER", "facilities": "FACILITY"}[kind]}

    s, f = mkn("suppliers", "Alpha Co"), mkn("facilities", "Site A")
    body = {"type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": s["id"], "destination_type": "FACILITY",
            "destination_id": f["id"], "justification": "contract seen", "status": "CONFIRMED"}  # fmt: skip
    r = secured.post(f"{G}/relationships", json=body, headers=h("editor"))
    assert r.status_code == 403 and "reviewer" in r.json()["error"]["message"]  # editors may only propose
    r = secured.post(f"{G}/relationships", json=body | {"status": "PROPOSED"}, headers=h("editor"))
    assert r.status_code == 201 and r.json()["created_by"] == "editor-user"
    ref = r.json()["ref"]
    st = {"status": "CONFIRMED", "reason": "verified", "expected_version": 1}
    assert secured.post(f"{G}/relationships/{ref}/status", json=st, headers=h("editor")).status_code == 403
    done = secured.post(f"{G}/relationships/{ref}/status", json=st, headers=h("reviewer"))
    assert done.status_code == 200 and done.json()["updated_by"] == "reviewer-user"
    assert secured.get(f"{G}/integrity", headers=h("reviewer")).status_code == 200
    assert secured.patch(f"{G}/relationships/{ref}", json={"expected_version": 2, "reason": "tweak", "confidence": 0.9},
                         headers=h("viewer")).status_code == 403  # fmt: skip
    assert secured.patch(f"{G}/relationships/{ref}", json={"expected_version": 2, "reason": "tweak", "confidence": 0.9},
                         headers=h("editor")).status_code == 200  # fmt: skip


def test_mutations_are_audited_with_actor_and_denials_are_recorded(secured):
    def mkn(kind, name):
        extra = {"facility_type": "MANUFACTURING"} if kind == "facilities" else {"category": "x"}
        r = secured.post(f"{API}/{kind}", json={"name": name, "country_or_region": "DE"} | extra, headers=h("editor"))
        return r.json() | {"type": {"suppliers": "SUPPLIER", "facilities": "FACILITY"}[kind]}

    s, f = mkn("suppliers", "Alpha Co"), mkn("facilities", "Site A")
    body = {"type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": s["id"], "destination_type": "FACILITY",
            "destination_id": f["id"], "justification": "contract seen"}  # fmt: skip
    ref = secured.post(f"{G}/relationships", json=body, headers=h("editor")).json()["ref"]
    secured.post(f"{G}/relationships", json=body, headers=h("viewer"))  # denied
    events = secured.get(f"{API}/audit", params={"limit": 100}, headers=h("reviewer")).json()["items"]
    mine = [e for e in events if e["action"] == "graph.relationship.create"]
    assert mine and mine[0]["actor"] == "editor-user" and mine[0]["target_id"] == ref
    assert any(e["action"] == "authz.denied" and e["actor"] == "viewer-user" for e in events)
    assert secured.get(f"{API}/audit/verify", headers=h("admin")).json()["ok"] is True


def test_write_gate_blocks_graph_writes_when_disabled(settings, engine):
    s = make_settings(settings, write_api_enabled=False)
    with TestClient(create_app(s, engine)) as c:
        assert c.get(f"{G}/summary").status_code == 200
        w = {"type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": 1, "destination_type": "FACILITY",
             "destination_id": 1, "justification": "evidence"}  # fmt: skip
        r = c.post(f"{G}/relationships", json=w)
        assert r.status_code == 403 and r.json()["error"]["code"] == "write_api_disabled"


def test_error_envelope_never_leaks_internals(client):
    r = client.get(f"{G}/relationships/SUPPLY:abc")
    assert r.status_code == 422 and set(r.json()["error"]) >= {"code", "message", "request_id"}
    assert "Traceback" not in r.text and "sqlalchemy" not in r.text.lower()
    r = client.get(f"{G}/relationships", params={"statuses": "'; DROP TABLE suppliers; --"})
    assert r.status_code == 422
    assert client.get(f"{API}/overview").status_code == 200  # tables intact


# ------------------------------------------------------------------------------------------------ migration


def _raw(path: Path, statement: str, *params):
    con = sqlite3.connect(path)
    con.execute(statement, params)
    con.commit()
    con.close()


def _seed_0003(path: Path, duplicate: bool = False) -> None:
    _raw(path, "INSERT INTO suppliers (supplier_code,name,country_or_region,category,status,provenance,created_at,updated_at) "
               "VALUES ('SUP-1','Alpha','DE','x','ACTIVE','USER_PROVIDED','2026-01-01 00:00:00','2026-01-01 00:00:00')")  # fmt: skip
    _raw(path, "INSERT INTO facilities (facility_code,name,facility_type,country_or_region,status,provenance,created_at,updated_at) "
               "VALUES ('FAC-1','Site','MANUFACTURING','DE','ACTIVE','USER_PROVIDED','2026-01-01 00:00:00','2026-01-01 00:00:00')")  # fmt: skip
    for n in range(2 if duplicate else 1):
        _raw(path, "INSERT INTO supply_relationships (relationship_code,relationship_type,supplier_id,target_facility_id,"
                   "provenance,created_at) VALUES (?, 'SUPPLIES',1,1,'USER_PROVIDED','2026-01-01 00:00:00')", f"R-{n}")  # fmt: skip


def test_migration_0004_preserves_existing_rows_and_roundtrips(tmp_path):
    path = tmp_path / "m.db"
    cfg = alembic_config(f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "0003")
    _seed_0003(path)
    command.upgrade(cfg, "head")
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT status, version, confidence, effective_from, updated_at, created_at FROM supply_relationships"
    ).fetchone()
    assert row[0] == "CONFIRMED" and row[1] == 1 and row[2] is None and row[3] is None and row[4] == row[5]
    assert con.execute("SELECT count(*) FROM graph_edge_events").fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError):
        con.execute(
            "INSERT INTO graph_edge_events (occurred_at,edge_kind,edge_id,action,actor,changes) VALUES ('2026-01-01','SUPPLY',1,'X','a','{}')"
        )
        con.execute("UPDATE graph_edge_events SET actor='b'")
    con.close()
    command.downgrade(cfg, "0003")
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(supply_relationships)")}
    assert "status" not in cols and con.execute("SELECT count(*) FROM supply_relationships").fetchone()[0] == 1
    con.close()
    command.upgrade(cfg, "head")


def test_migration_0004_refuses_to_run_over_duplicates_without_touching_data(tmp_path):
    path = tmp_path / "d.db"
    cfg = alembic_config(f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "0003")
    _seed_0003(path, duplicate=True)
    with pytest.raises(Exception, match="duplicate supply_relationships"):
        command.upgrade(cfg, "head")
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM supply_relationships").fetchone()[0] == 2
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"
    con.close()


def test_fresh_database_initialisation_matches_models(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db.base import Base
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'f.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    eng = create_db_engine(url)
    with eng.connect() as c:
        assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
    eng.dispose()


def test_rel_helper_import_is_exercised():
    assert rel and node
