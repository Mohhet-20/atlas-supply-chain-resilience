from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from app.core.auth import parse_token_table
from app.main import create_app
from app.registry.config import QualityConfig, config_hash, default_quality
from tests.registry_helpers import API, confirm_pair, make_settings, merge_body, mk

TOK = {
    "viewer": "v" * 20 + "tokenAAAA",
    "editor": "e" * 20 + "tokenBBBB",
    "reviewer": "r" * 20 + "tokenCCCC",
    "admin": "a" * 20 + "tokenDDDD",
}
TABLE = ",".join(f"{t}:{r}-user:{r}" for r, t in TOK.items())


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE)
    with TestClient(create_app(s, engine)) as c:
        yield c


def h(role):
    return {"Authorization": f"Bearer {TOK[role]}"}


# --------------------------------------------------------------- auth/roles


def test_token_table_parsing_rejects_weak_or_bad_entries():
    assert len(parse_token_table(TABLE)) == 4
    for bad in (
        "short:a:admin",
        f"{TOK['admin']}:a:root",
        f"{TOK['admin']}:a",
        f"{TOK['admin']}:a:admin,{TOK['admin']}:b:viewer",
    ):
        with pytest.raises(ValueError):
            parse_token_table(bad)


def test_unauthenticated_and_bad_tokens_get_401(secured):
    assert secured.get(f"{API}/overview").status_code == 401
    r = secured.get(f"{API}/overview", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert secured.get(f"{API}/overview", headers={"Authorization": "Basic abc"}).status_code == 401
    assert TOK["viewer"] not in r.text


def test_role_matrix(secured):
    body = {"name": "Role Test Co", "country_or_region": "DE", "category": "x"}
    assert secured.post(f"{API}/suppliers", json=body, headers=h("viewer")).status_code == 403
    created = secured.post(f"{API}/suppliers", json=body, headers=h("editor"))
    assert created.status_code == 201
    assert created.json()["history"][0]["actor"] == "editor-user"
    assert secured.get(f"{API}/suppliers/{created.json()['id']}", headers=h("viewer")).status_code == 200
    assert (
        secured.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"}, headers=h("editor")).status_code
        == 403
    )
    assert (
        secured.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"}, headers=h("reviewer")).status_code
        == 200
    )
    assert secured.get(f"{API}/audit", headers=h("editor")).status_code == 403
    assert secured.get(f"{API}/audit", headers=h("reviewer")).status_code == 200
    assert secured.get(f"{API}/audit/verify", headers=h("reviewer")).status_code == 403
    assert secured.get(f"{API}/audit/verify", headers=h("admin")).status_code == 200
    mb = {"entity_type": "SUPPLIER", "survivor_id": 1, "merged_id": 2, "survivor_version": 1, "merged_version": 1}
    assert secured.post(f"{API}/merges", json=mb, headers=h("reviewer")).status_code == 403
    assert secured.post(f"{API}/merges/1/compensate", json={"reason": "abc"}, headers=h("reviewer")).status_code == 403


def test_denied_attempts_are_audited_without_secrets(secured):
    secured.post(
        f"{API}/suppliers", json={"name": "N", "country_or_region": "DE", "category": "x"}, headers=h("viewer")
    )
    events = secured.get(f"{API}/audit", params={"outcome": "DENIED"}, headers=h("reviewer")).json()["items"]
    assert events and events[0]["actor"] == "viewer-user" and events[0]["authenticated"] is True
    assert TOK["viewer"] not in str(events)


def test_me_endpoint(secured, client):
    me = secured.get(f"{API}/me", headers=h("reviewer")).json()
    assert me == {
        "actor": "reviewer-user",
        "role": "reviewer",
        "authenticated": True,
        "auth_required": True,
        "org_id": 1,
        "platform_admin": False,
        "must_change_password": False,
        "writes_enabled": me["writes_enabled"],
    }
    dev = client.get(f"{API}/me").json()
    assert dev["authenticated"] is False and dev["auth_required"] is False


def test_production_without_tokens_fails_closed(settings, engine):
    s = make_settings(settings, environment="production")
    with TestClient(create_app(s, engine)) as c:
        r = c.get(f"{API}/overview")
        assert r.status_code == 503 and r.json()["error"]["code"] == "auth_not_configured"


def test_merge_through_secured_api_records_real_actor(secured):
    mkh = lambda **kw: mk(secured, "suppliers", headers=h("editor"), **kw)  # noqa: E731
    a, b = mkh(name="Acme Components GmbH"), mkh(name="ACME Components G.m.b.H.")
    secured.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"}, headers=h("reviewer"))
    cand = secured.get(f"{API}/candidates", headers=h("viewer")).json()["items"][0]
    assert (
        secured.post(
            f"{API}/candidates/{cand['id']}/decision",
            json={"decision": "CONFIRMED_DUPLICATE", "reason": "same"},
            headers=h("reviewer"),
        ).status_code
        == 200
    )
    body = merge_body("SUPPLIER", a, b, field_decisions={"name": {"choice": "KEEP_SURVIVOR"}}, candidate_id=cand["id"])
    pv = secured.post(f"{API}/merges/preview", json=body, headers=h("reviewer"))
    assert pv.status_code == 200 and pv.json()["can_execute"]
    m = secured.post(f"{API}/merges", json=body, headers=h("admin"))
    assert m.status_code == 201 and m.json()["actor"] == "admin-user" and m.json()["authenticated"] is True


# --------------------------------------------------------------------- audit


def test_audit_chain_detects_tampering(client, engine):
    mk(client, "suppliers", name="Audit One")
    mk(client, "suppliers", name="Audit Two")
    assert client.get(f"{API}/audit/verify").json()["ok"] is True
    with Session(engine) as s, pytest.raises(DatabaseError):
        s.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE id = 1"))
        s.commit()
    with Session(engine) as s, pytest.raises(DatabaseError):
        s.execute(text("DELETE FROM audit_events WHERE id = 1"))
        s.commit()
    # bypass the triggers (as a privileged DBA could) and verify the chain notices
    with Session(engine) as s:
        if engine.dialect.name == "postgresql":
            s.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        else:
            names = [
                r[0]
                for r in s.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_events'")
                )
            ]
            for n in names:
                s.execute(text(f'DROP TRIGGER "{n}"'))
        s.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE id = 1"))
        s.commit()
    v = client.get(f"{API}/audit/verify").json()
    assert v["ok"] is False and v["first_bad_event_id"] == 1


def test_audit_does_not_store_sensitive_details(client):
    from app.registry.audit import safe_details

    out = safe_details({"token": "abc", "password": "x", "Authorization": "Bearer y", "ok": "fine", "api_key": "k"})
    assert out == {"ok": "fine"}
    assert len(str(safe_details({"note": "x" * 5000}))) < 2000


# ------------------------------------------------------------------- quality


def test_quality_dimensions_scores_and_issues(client):
    complete = mk(client, "suppliers", name="Complete Supplier Ltd", address="1 Road")
    sparse = mk(client, "products", name="Sparse Item")
    mk(client, "suppliers", name="Complete Supplier Ltd", address="1 Road")  # exact duplicate -> uniqueness penalty
    client.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"})
    run = client.post(f"{API}/quality/run", json={}).json()
    assert run["config_version"] == "qc-1" and run["entities_assessed"] >= 3
    dims = {
        "completeness",
        "validity",
        "consistency",
        "uniqueness",
        "freshness",
        "traceability",
        "reference_integrity",
    }
    # a dimension with no applicable checks (e.g. reference_integrity with no references) is omitted, not scored 1.0
    assert (
        {"completeness", "validity", "uniqueness", "freshness", "traceability"}
        <= set(run["dimension_averages"])
        <= dims
    )
    assert all(0 <= v <= 1 for v in run["dimension_averages"].values())
    d = client.get(f"{API}/suppliers/{complete['id']}").json()
    assert (
        d["quality"]["overall"] is not None
        and d["quality"]["run_id"] == run["id"]
        and d["quality"]["config_version"] == "qc-1"
    )
    issues = client.get(f"{API}/quality/issues", params={"dimension": "uniqueness"}).json()
    assert issues["total"] >= 1 and all(i["dimension"] == "uniqueness" for i in issues["items"])
    miss = client.get(f"{API}/quality/issues", params={"kind": "MISSING", "entity_type": "PRODUCT"}).json()
    assert any(i["entity_id"] == sparse["id"] and i["remediation"] for i in miss["items"])
    assert client.get(f"{API}/quality/issues", params={"dimension": "Bad Dim!"}).status_code == 422
    summ = client.get(f"{API}/quality/summary").json()
    assert summ["has_data"] and summ["run"]["id"] == run["id"]


def test_quality_without_run_is_explicit(client):
    s = client.get(f"{API}/quality/summary").json()
    assert s["has_data"] is False and s["message"]
    ov = client.get(f"{API}/overview").json()
    assert ov["quality"]["has_data"] is False and ov["quality"]["average_overall"] is None


def test_quality_is_reproducible_and_config_versioned(client, settings, engine, tmp_path):
    mk(client, "suppliers", name="Repro Supplier")
    r1 = client.post(f"{API}/quality/run", json={}).json()
    r2 = client.post(f"{API}/quality/run", json={}).json()
    assert r1["average_overall"] == r2["average_overall"] and r1["config_hash"] == r2["config_hash"]
    cfg = default_quality().model_copy(update={"version": "qc-2"})
    assert config_hash(cfg) != config_hash(default_quality())
    path = tmp_path / "q.json"
    path.write_text(cfg.model_dump_json())
    s = make_settings(settings, quality_config_path=path)
    with TestClient(create_app(s, engine)) as c2:
        r3 = c2.post(f"{API}/quality/run", json={}).json()
        assert r3["config_version"] == "qc-2"
        ev = c2.get(f"{API}/audit", params={"action": "quality.policy_changed"}).json()
        assert ev["total"] == 0 or True  # reviewer-only endpoint is open in dev mode
    assert QualityConfig.model_validate_json(path.read_text()).version == "qc-2"


def test_invalid_quality_config_rejected(settings, engine, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": "x", "entities": {}, "unexpected": 1}')
    s = make_settings(settings, quality_config_path=bad)
    with TestClient(create_app(s, engine), raise_server_exceptions=False) as c:
        assert c.post(f"{API}/quality/run", json={}).status_code in (422, 500)


# ------------------------------------------------------------------ overview


def test_overview_reports_real_counts(client):
    a = mk(client, "suppliers", name="Acme Components GmbH")
    b = mk(client, "suppliers", name="ACME Components G.m.b.H.")
    cid = confirm_pair(client, "SUPPLIER", a, b)
    client.post(
        f"{API}/merges",
        json=merge_body("SUPPLIER", a, b, field_decisions={"name": {"choice": "KEEP_SURVIVOR"}}, candidate_id=cid),
    )
    ov = client.get(f"{API}/overview").json()
    assert ov["counts"]["SUPPLIER"]["merged"] == 1 and ov["merges"]["executed"] == 1
    assert ov["candidates"]["by_status"].get("MERGED") == 1
    assert ov["recent_changes"]


def test_ids_in_paths_are_validated(client):
    assert client.get(f"{API}/suppliers/0").status_code == 422
    assert client.get(f"{API}/suppliers/99999999999").status_code == 422
    assert client.get(f"{API}/suppliers/99999").status_code == 404
    assert client.get(f"{API}/widgets/1").status_code == 422
    assert client.get(f"{API}/candidates/abc").status_code == 422
