"""Supplier-risk HTTP contract: auth, validation, not-found, pagination, reviews and overrides, comparison, reporting."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.risk import RiskReview
from tests.graph_helpers import G, node, ok
from tests.registry_helpers import make_settings, mk
from tests.supplier_risk_helpers import TABLE, R, assess_all, current, h


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False)
    with TestClient(create_app(s, engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    s = make_settings(
        settings,
        write_api_enabled=False,
        environment="production",
        api_tokens=TABLE,
        cors_allowed_origins=["https://x.example"],
        risk_auto_reassess=False,
    )
    with TestClient(create_app(s, engine)) as c:
        yield c


@pytest.fixture
def world(secured):
    """Two assessed suppliers sharing a facility, built with the admin token."""
    ad = h("admin")
    a = mk(secured, "suppliers", headers=ad, name="Alder Parts", country_or_region="Germany")
    b = mk(secured, "suppliers", headers=ad, name="Birch Parts", country_or_region="Poland")
    f = mk(
        secured,
        "facilities",
        headers=ad,
        name="Cedar Plant",
        country_or_region="Germany",
        facility_type="MANUFACTURING",
    )
    for sup in (a, b):
        r = secured.post(
            f"{G}/relationships",
            json={
                "type": "SUPPLIES",
                "source_type": "SUPPLIER",
                "source_id": sup["id"],
                "destination_type": "FACILITY",
                "destination_id": f["id"],
                "status": "CONFIRMED",
                "justification": "synthetic test",
                "criticality": "HIGH",
            },
            headers=ad,
        )
        assert r.status_code == 201, r.text
    assess_all(secured)
    return a, b, f


# ------------------------------------------------------------------------------------------------ read model and auth


def test_endpoints_require_authentication_when_tokens_are_configured(secured, world):
    a, _b, _f = world
    for path in (
        "/methodology",
        "/overview",
        "/geography",
        "/suppliers",
        f"/suppliers/{a['id']}",
        "/alerts",
        "/runs",
        f"/compare?ids={a['id']}&ids={world[1]['id']}",
    ):
        assert secured.get(f"{R}{path}").status_code == 401, path
        assert secured.get(f"{R}{path}", headers=h("viewer")).status_code == 200, path
    assert secured.get(f"{R}/overview", headers={"Authorization": "Bearer wrong-token-value-1234"}).status_code == 401


def test_write_roles_are_enforced(secured, world):
    a, _b, _f = world
    cur = current(secured, a["id"], h("viewer"))["assessment"]
    review = {"assessment_id": cur["id"], "action": "ACKNOWLEDGE"}
    assert secured.post(f"{R}/suppliers/{a['id']}/reviews", json=review).status_code == 401
    assert secured.post(f"{R}/suppliers/{a['id']}/reviews", json=review, headers=h("viewer")).status_code == 403
    assert secured.post(f"{R}/suppliers/{a['id']}/reviews", json=review, headers=h("editor")).status_code == 201
    over = {
        "assessment_id": cur["id"],
        "action": "OVERRIDE",
        "override_classification": "HIGH",
        "note": "Supplier is moving to a sole-source contract.",
    }
    assert secured.post(f"{R}/suppliers/{a['id']}/reviews", json=over, headers=h("editor")).status_code == 403
    assert secured.post(f"{R}/suppliers/{a['id']}/reviews", json=over, headers=h("reviewer")).status_code == 201
    assert secured.post(f"{R}/runs", json={"scope": "NETWORK"}, headers=h("viewer")).status_code == 403
    assert (
        secured.post(f"{R}/runs", json={"scope": "NETWORK"}, headers=h("editor")).status_code == 403
    )  # network runs need a reviewer
    assert (
        secured.post(f"{R}/runs", json={"scope": "SUPPLIER", "supplier_id": a["id"]}, headers=h("editor")).status_code
        == 202
    )
    assert secured.post(f"{R}/runs", json={"scope": "NETWORK"}, headers=h("reviewer")).status_code == 202


def test_writes_can_be_disabled_without_disabling_reads(readonly):
    assert readonly.get(f"{R}/overview", headers=h("viewer")).status_code == 200
    for path, body in ((f"{R}/runs", {"scope": "NETWORK"}), (f"{R}/suppliers/1/assess", {})):
        r = readonly.post(path, json=body, headers=h("admin"))
        assert r.status_code == 403 and r.json()["error"]["code"] == "write_api_disabled", path


def test_not_found_and_bad_ids(secured, world):
    v = h("viewer")
    for path in (
        "/suppliers/9999",
        "/assessments/9999",
        "/runs/9999",
        "/suppliers/9999/dependencies",
        "/suppliers/9999/assessments",
    ):
        r = secured.get(f"{R}{path}", headers=v)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    for path in ("/suppliers/0", "/suppliers/-1", "/suppliers/abc", f"/suppliers/{2**40}", "/runs/0"):
        assert secured.get(f"{R}{path}", headers=v).status_code == 422, path


def test_invalid_filters_and_pagination_limits_are_rejected(secured, world):
    v = h("viewer")
    bad = [
        {"classification": "SEVERE"}, {"assessment_status": "WEIRD"}, {"sort": "random"}, {"limit": 101}, {"limit": 0}, {"offset": -1},
        {"min_coverage": 1.5}, {"review_state": "x"}, {"q": "x" * 81},
    ]  # fmt: skip
    for params in bad:
        assert secured.get(f"{R}/suppliers", params=params, headers=v).status_code == 422, params
    assert secured.get(f"{R}/alerts", params={"status": "NOPE"}, headers=v).status_code == 422
    assert secured.get(f"{R}/alerts", params={"limit": 1000}, headers=v).status_code == 422
    assert secured.get(f"{R}/compare", params={"ids": [1]}, headers=v).status_code == 422
    assert secured.get(f"{R}/compare", params=[("ids", i) for i in range(1, 9)], headers=v).status_code == 422


def test_search_is_literal_not_a_pattern_or_sql(secured, world):
    v = h("viewer")
    assert secured.get(f"{R}/suppliers", params={"q": "%"}, headers=v).json()["total"] == 0
    assert secured.get(f"{R}/suppliers", params={"q": "'; DROP TABLE suppliers;--"}, headers=v).json()["total"] == 0
    assert secured.get(f"{R}/suppliers", params={"q": "alder"}, headers=v).json()["total"] == 1
    assert secured.get(f"{R}/suppliers", headers=v).json()["total"] == 2


def test_list_filters_sort_and_unscored_ordering(secured, world):
    v = h("viewer")
    lone = mk(secured, "suppliers", headers=h("admin"), name="Zeta Lonely", country_or_region="Peru")
    assess_all(secured)
    asc = secured.get(f"{R}/suppliers", params={"sort": "score_asc"}, headers=v).json()
    desc = secured.get(f"{R}/suppliers", params={"sort": "score_desc"}, headers=v).json()
    assert (
        asc["items"][-1]["supplier_id"] == desc["items"][-1]["supplier_id"] == lone["id"]
    )  # unscored last in both directions
    assert (
        asc["items"][-1]["risk_score"] is None
        and asc["items"][-1]["assessment_status"] == "INSUFFICIENT_EVIDENCE"
        and asc["items"][-1]["rank_comparable"] is False
    )
    only = secured.get(f"{R}/suppliers", params={"assessment_status": "INSUFFICIENT_EVIDENCE"}, headers=v).json()
    assert [i["supplier_id"] for i in only["items"]] == [lone["id"]]
    page = secured.get(f"{R}/suppliers", params={"limit": 1, "offset": 1}, headers=v).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    region = secured.get(f"{R}/suppliers", params={"region": "Peru"}, headers=v).json()
    assert region["total"] == 1


def test_unassessed_supplier_is_reported_as_such_not_as_zero(secured):
    s = mk(secured, "suppliers", headers=h("admin"), name="Fresh One", country_or_region="Chile")
    d = secured.get(f"{R}/suppliers/{s['id']}", headers=h("viewer")).json()
    assert d["assessed"] is False and d["assessment"] is None and d["unassessed_note"]
    row = secured.get(f"{R}/suppliers", headers=h("viewer")).json()["items"][0]
    assert row["assessment_status"] == "NOT_ASSESSED" and row["risk_score"] is None and row["classification"] is None


def test_overview_counts_are_exact_and_separate_status_from_class(secured, world):
    o = secured.get(f"{R}/overview", headers=h("viewer")).json()
    assert o["suppliers_in_scope"] == 2 == o["assessed_suppliers"]
    assert sum(o["by_status"].values()) == 2
    assert o["classified"] == sum(o["by_classification"].values()) == o["by_status"]["ASSESSED"]
    assert o["max_possible_coverage"] == 0.65 and o["unavailable_categories"]
    assert {c["category"] for c in o["unavailable_categories"]} == {"OPERATIONAL", "FINANCIAL", "COMPLIANCE"}


def test_methodology_endpoint_documents_every_category_and_is_not_editable(secured):
    m = secured.get(f"{R}/methodology", headers=h("viewer")).json()
    assert m["version"] == "rm-1" and len(m["categories"]) == 10 and m["parameters_editable_via_api"] is False
    assert m["disclaimer"]["not_a_probability"] is True
    assert secured.put(f"{R}/methodology", json={}, headers=h("admin")).status_code in (404, 405)


def test_detail_has_all_factors_with_evidence_and_interpretation(secured, world):
    a, _b, _f = world
    d = secured.get(f"{R}/suppliers/{a['id']}", headers=h("viewer")).json()
    asm = d["assessment"]
    assert len(asm["factors"]) == 10 and asm["interpretation"]["not_a_probability"] is True
    scored = [f for f in asm["factors"] if f["state"] == "SCORED"]
    assert scored and all(f["evidence"] for f in scored)
    unavailable = [f for f in asm["factors"] if f["state"] == "UNAVAILABLE"]
    assert unavailable and all(f["score"] is None and f["unavailable_reason"] for f in unavailable)
    again = secured.get(f"{R}/assessments/{asm['id']}", headers=h("viewer")).json()
    assert again["id"] == asm["id"] and len(again["factors"]) == 10


def test_dependencies_endpoint_lists_nodes_shares_and_the_treatment_rules(secured, world):
    a, _b, f = world
    d = secured.get(f"{R}/suppliers/{a['id']}/dependencies", headers=h("viewer")).json()
    assert d["nodes"][0]["node_id"] == f["id"] and d["nodes"][0]["confirmed_supplier_count"] == 2
    assert "PROPOSED" in d["treatment"] and d["notes"]


def test_geography_reports_region_shares_with_caveats(secured, world):
    g = secured.get(f"{R}/geography", headers=h("viewer")).json()
    assert {r["region"] for r in g["regions"]} >= {"Germany", "Poland"} and g["caveats"]
    assert g["total_suppliers"] == 2


# ------------------------------------------------------------------------------------------------ comparison


def test_compare_flags_incomparable_assessments_and_drops_the_ranking(secured, world):
    a, b, _f = world
    lone = mk(secured, "suppliers", headers=h("admin"), name="Zeta Lonely", country_or_region="Peru")
    assess_all(secured)
    ok_ = secured.get(f"{R}/compare", params={"ids": [a["id"], b["id"]]}, headers=h("viewer")).json()
    assert ok_["comparable"] is True and len(ok_["ranking"]) == 2 and len(ok_["matrix"]) == 10
    bad = secured.get(f"{R}/compare", params={"ids": [a["id"], lone["id"]]}, headers=h("viewer")).json()
    assert bad["comparable"] is False and any("Insufficient evidence" in r for r in bad["incomparable_reasons"])
    assert secured.get(f"{R}/compare", params={"ids": [a["id"], a["id"]]}, headers=h("viewer")).status_code == 422
    assert secured.get(f"{R}/compare", params={"ids": [a["id"], 9999]}, headers=h("viewer")).status_code == 404


# ------------------------------------------------------------------------------------------------ reviews and overrides


def review(c, sid, role, **body):
    return c.post(f"{R}/suppliers/{sid}/reviews", json=body, headers=h(role))


def test_override_is_additive_audited_and_never_changes_the_automated_result(secured, world):
    a, _b, _f = world
    before = current(secured, a["id"], h("viewer"))["assessment"]
    r = review(
        secured,
        a["id"],
        "reviewer",
        assessment_id=before["id"],
        action="OVERRIDE",
        override_classification="CRITICAL",
        note="Known pending insolvency, not yet recorded in ATLAS.",
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert (
        row["original_status"] == before["status"]
        and row["original_classification"] == before["classification"]
        and row["original_score"] == before["risk_score"]
    )
    d = current(secured, a["id"], h("viewer"))
    assert (
        d["assessment"]["classification"] == before["classification"]
        and d["assessment"]["risk_score"] == before["risk_score"]
    )
    assert (
        d["override"]["override_classification"] == "CRITICAL"
        and d["override"]["effective"] is True
        and d["assessment"]["review_state"] == "OVERRIDDEN"
    )
    listed = secured.get(f"{R}/suppliers", headers=h("viewer")).json()["items"]
    mine = next(i for i in listed if i["supplier_id"] == a["id"])
    assert mine["classification"] == before["classification"] and mine["override"]["classification"] == "CRITICAL"


def test_override_validation_rules(secured, world):
    a, _b, _f = world
    aid = current(secured, a["id"], h("viewer"))["assessment"]["id"]
    base = {"assessment_id": aid, "action": "OVERRIDE", "override_classification": "HIGH"}
    assert review(secured, a["id"], "reviewer", **base).status_code == 422  # note required
    assert review(secured, a["id"], "reviewer", **base, note="too short").status_code == 422
    assert (
        review(secured, a["id"], "reviewer", assessment_id=aid, action="OVERRIDE", note="x" * 30).status_code == 422
    )  # class required
    assert (
        review(
            secured, a["id"], "editor", assessment_id=aid, action="ACKNOWLEDGE", override_classification="HIGH"
        ).status_code
        == 422
    )
    assert review(secured, a["id"], "editor", assessment_id=aid, action="NOTE").status_code == 422
    assert (
        review(secured, a["id"], "editor", assessment_id=aid, action="FOLLOW_UP", note="check").status_code == 422
    )  # category required
    assert (
        review(
            secured, a["id"], "editor", assessment_id=aid, action="FOLLOW_UP", note="check", factor_category="NOPE"
        ).status_code
        == 422
    )
    assert (
        review(
            secured,
            a["id"],
            "editor",
            assessment_id=aid,
            action="FOLLOW_UP",
            note="check shares",
            factor_category="CONCENTRATION",
        ).status_code
        == 201
    )
    far = "2999-01-01T00:00:00Z"
    assert (
        review(secured, a["id"], "reviewer", **base, note="x" * 30, override_expires_at=far).status_code == 422
    )  # beyond the maximum duration
    assert (
        review(
            secured, a["id"], "reviewer", **base, note="x" * 30, override_expires_at="2000-01-01T00:00:00Z"
        ).status_code
        == 422
    )  # in the past
    assert (
        review(secured, a["id"], "reviewer", assessment_id=aid, action="ACKNOWLEDGE", extra="field").status_code == 422
    )  # unknown fields rejected


def test_a_stale_assessment_cannot_be_reviewed_and_nothing_is_written(secured, world):
    a, _b, f = world
    old = current(secured, a["id"], h("viewer"))["assessment"]
    third = mk(secured, "suppliers", headers=h("admin"), name="Cypress Parts", country_or_region="Spain")
    r = secured.post(
        f"{G}/relationships",
        json={
            "type": "SUPPLIES",
            "source_type": "SUPPLIER",
            "source_id": third["id"],
            "destination_type": "FACILITY",
            "destination_id": f["id"],
            "status": "CONFIRMED",
            "justification": "synthetic test",
            "criticality": "HIGH",
        },
        headers=h("admin"),
    )
    assert r.status_code == 201
    assess_all(secured)
    new = current(secured, a["id"], h("viewer"))["assessment"]
    assert new["id"] != old["id"]
    with Session(secured.app.state.engine) as s:
        count = s.scalar(select(func.count()).select_from(RiskReview))
    r = review(secured, a["id"], "editor", assessment_id=old["id"], action="ACKNOWLEDGE")
    assert r.status_code == 409
    with Session(secured.app.state.engine) as s:
        assert s.scalar(select(func.count()).select_from(RiskReview)) == count


def test_override_becomes_stale_when_a_new_assessment_supersedes_its_basis_and_can_be_revoked(secured, world):
    a, _b, f = world
    cur = current(secured, a["id"], h("viewer"))["assessment"]
    ov = review(
        secured,
        a["id"],
        "reviewer",
        assessment_id=cur["id"],
        action="OVERRIDE",
        override_classification="LOW",
        note="Reviewed on site; risk accepted for this quarter.",
    ).json()
    third = mk(secured, "suppliers", headers=h("admin"), name="Cypress Parts", country_or_region="Spain")
    secured.post(
        f"{G}/relationships",
        json={
            "type": "SUPPLIES",
            "source_type": "SUPPLIER",
            "source_id": third["id"],
            "destination_type": "FACILITY",
            "destination_id": f["id"],
            "status": "CONFIRMED",
            "justification": "synthetic test",
            "criticality": "HIGH",
        },
        headers=h("admin"),
    )
    assess_all(secured)
    d = current(secured, a["id"], h("viewer"))
    assert d["assessment"]["id"] != cur["id"]
    new_id = d["assessment"]["id"]
    # an override never silently applies to a different assessment: it is shown as stale and not in effect
    assert d["override"]["id"] == ov["id"] and d["override"]["stale"] is True and d["override"]["effective"] is False
    assert d["override"]["note_on_staleness"] and d["assessment"]["review_state"] == "UNREVIEWED"
    revoke = review(
        secured,
        a["id"],
        "reviewer",
        assessment_id=new_id,
        action="REVOKE_OVERRIDE",
        target_review_id=ov["id"],
        note="superseded by new data",
    )
    assert revoke.status_code == 201
    assert (
        review(
            secured,
            a["id"],
            "reviewer",
            assessment_id=new_id,
            action="REVOKE_OVERRIDE",
            target_review_id=ov["id"],
            note="again",
        ).status_code
        == 409
    )


def test_reviews_are_append_only_at_the_database_level(secured, world):
    a, _b, _f = world
    aid = current(secured, a["id"], h("viewer"))["assessment"]["id"]
    rid = review(secured, a["id"], "editor", assessment_id=aid, action="NOTE", note="looked at it").json()["id"]
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    with Session(secured.app.state.engine) as s:
        with pytest.raises(DatabaseError, match="append-only|no_update|abort|cannot"):
            s.execute(text("UPDATE risk_reviews SET note = 'edited' WHERE id = :i"), {"i": rid})
        s.rollback()
        with pytest.raises(DatabaseError):
            s.execute(text("DELETE FROM risk_reviews WHERE id = :i"), {"i": rid})
        s.rollback()
        assert s.scalar(select(RiskReview.note).where(RiskReview.id == rid)) == "looked at it"
        row = s.get(RiskReview, rid)
        row.note = "edited"
        with pytest.raises(ValueError, match="append-only"):
            s.flush()
        s.rollback()


def test_assessing_one_supplier_now_requires_editor_and_returns_the_result(secured, world):
    a, _b, _f = world
    assert secured.post(f"{R}/suppliers/{a['id']}/assess", headers=h("viewer")).status_code == 403
    r = secured.post(f"{R}/suppliers/{a['id']}/assess", headers=h("editor"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["status"] == "COMPLETED" and body["run"]["unchanged"] == 1 and body["result"]["assessed"] is True
    assert secured.post(f"{R}/suppliers/9999/assess", headers=h("editor")).status_code == 404


def test_run_request_validation(secured, world):
    a, _b, _f = world
    ed = h("reviewer")
    assert secured.post(f"{R}/runs", json={"scope": "SUPPLIER"}, headers=ed).status_code == 422
    assert secured.post(f"{R}/runs", json={"scope": "NETWORK", "supplier_id": a["id"]}, headers=ed).status_code == 422
    assert secured.post(f"{R}/runs", json={"scope": "SUPPLIERS", "supplier_ids": []}, headers=ed).status_code == 422
    assert (
        secured.post(
            f"{R}/runs", json={"scope": "SUPPLIERS", "supplier_ids": list(range(1, 102))}, headers=ed
        ).status_code
        == 422
    )
    assert secured.post(f"{R}/runs", json={"scope": "SUPPLIER", "supplier_id": 9999}, headers=ed).status_code == 404
    assert secured.post(f"{R}/runs", json={"scope": "NETWORK", "bogus": 1}, headers=ed).status_code == 422
    assert (
        secured.post(f"{R}/runs", json={"scope": "NETWORK"}, headers={**ed, "Idempotency-Key": "short"}).status_code
        == 422
    )
    assert (
        secured.post(
            f"{R}/runs", json={"scope": "NETWORK"}, headers={**ed, "Idempotency-Key": "bad key with spaces!"}
        ).status_code
        == 422
    )


def test_run_listing_and_detail(secured, world):
    runs_ = secured.get(f"{R}/runs", headers=h("viewer")).json()
    assert runs_["total"] >= 1
    one = secured.get(f"{R}/runs/{runs_['items'][0]['id']}", headers=h("viewer")).json()
    assert one["status"] == "COMPLETED" and one["methodology_version"] == "rm-1" and one["failures"] == []
    assert node and ok  # helpers imported for symmetry with the other modules
    assert secured.get(f"{R}/runs", params={"status": "NOPE"}, headers=h("viewer")).status_code == 422
