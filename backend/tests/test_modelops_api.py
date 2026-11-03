"""Model-governance API: contract, roles, write gate, idempotency, pagination, validation, security, end-to-end. Synthetic data only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.modelops_helpers import issue_assessments, leadtime_env
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings

M = "/api/v1/modelops"


@pytest.fixture
def secured(settings, engine):
    with TestClient(create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False), engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    with TestClient(
        create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, write_api_enabled=False), engine)
    ) as c:
        yield c


def _lead_uid(c):
    items = c.get(f"{M}/models?limit=100").json()["items"]
    return next(i for i in items if i["domain"] == "LEAD_TIME")["model_uid"], items


def test_health_discloses_capabilities_and_limits(client):
    j = client.get(f"{M}/health").json()
    assert j["algorithm_version"] == "mo-1" and "shap" in j["explanation_methods"]["unsupported"]
    assert (
        not any("organisation" in x for x in j["not_supported"])
        and "separated by organisation" in j["statements"]["tenancy"]
        and "mae" in j["metric_definitions"]
    )


def test_empty_registry_then_sync_then_idempotent(client):
    assert client.get(f"{M}/models").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    leadtime_env(client)
    r = client.post(f"{M}/models/sync")
    assert r.status_code == 200 and r.json()["registered"] >= 3
    assert client.post(f"{M}/models/sync").json()["registered"] == 0
    page = client.get(f"{M}/models?limit=2&offset=0").json()
    assert page["total"] >= 3 and len(page["items"]) == 2
    only = client.get(f"{M}/models?domain=SUPPLIER_RISK").json()
    assert [i["model_key"] for i in only["items"]] == ["risk/rule_based"]
    assert client.get(f"{M}/models?q=risk").json()["total"] >= 1
    assert (
        client.get(f"{M}/models?limit=0").status_code == 422 and client.get(f"{M}/models?state=NOPE").status_code == 422
    )


def test_get_model_detail_versions_lifecycle_card(client):
    leadtime_env(client)
    client.post(f"{M}/models/sync")
    uid, _ = _lead_uid(client)
    d = client.get(f"{M}/models/{uid}").json()
    assert (
        d["lifecycle_state"] == "DEVELOPMENT"
        and d["intended_use"]
        and d["prohibited_use"]
        and d["artifact_checksum"]
        and d["statement"]
    )
    assert d["artifact_ref"].startswith("leadtime_run:") and "/" not in d["artifact_ref"]
    assert client.get(f"{M}/models/{uid}/versions").json()["total"] == 1
    lc = client.get(f"{M}/models/{uid}/lifecycle").json()
    assert lc["state"] == "DEVELOPMENT" and {"to": "EVALUATED", "minimum_role": "system"} in lc["allowed_transitions"]
    card = client.get(f"{M}/models/{uid}/card").json()
    assert card["evaluation"]["recorded_backtest"] == "not recorded" and card["statement"]
    assert client.get(f"{M}/models/MV-DOESNOTEXIST").status_code == 404
    assert client.get(f"{M}/models/bad%20id").status_code == 422


def test_end_to_end_evaluate_approve_explain_monitor(client):
    cm, lead, _risk = leadtime_env(client)
    issue_assessments(client, cm, lead, n=3)
    client.post(f"{M}/models/sync")
    uid, _ = _lead_uid(client)
    r = client.post(f"{M}/models/{uid}/evaluations", json={}, headers={"Idempotency-Key": "ev-key-0001"})
    assert r.status_code == 202
    ev = r.json()["eval_uid"]
    again = client.post(f"{M}/models/{uid}/evaluations", json={}, headers={"Idempotency-Key": "ev-key-0001"})
    assert again.headers["Idempotent-Replayed"] == "true" and again.json()["eval_uid"] == ev
    assert (
        client.post(
            f"{M}/models/{uid}/evaluations", json={"kind": "PROSPECTIVE"}, headers={"Idempotency-Key": "ev-key-0001"}
        ).status_code
        == 409
    )
    got = client.get(f"{M}/evaluations/{ev}").json()
    assert got["status"] == "COMPLETED" and got["result_status"] == "completed" and got["metrics"]["mae"]["status"] == "ok" and got["reproducibility"]["evidence_hash"]  # fmt: skip
    for part in ("baselines", "folds", "segments", "calibration"):
        assert client.get(f"{M}/evaluations/{ev}/{part}").status_code == 200
    assert client.get(f"{M}/models/{uid}/evaluations").json()["total"] == 1
    assert client.get(f"{M}/models/{uid}").json()["lifecycle_state"] == "EVALUATED"
    bad = client.post(f"{M}/models/{uid}/transition", json={"to_state": "APPROVED", "note": "short"})
    assert bad.status_code == 422
    ok = client.post(
        f"{M}/models/{uid}/transition",
        json={"to_state": "APPROVED", "note": "Backtest reviewed against baselines; planning use only."},
    )
    assert ok.status_code == 200 and ok.json()["lifecycle_state"] == "APPROVED"
    lc = client.get(f"{M}/models/{uid}/lifecycle").json()
    assert [i["to_state"] for i in lc["items"]] == ["DEVELOPMENT", "EVALUATED", "APPROVED"] and lc["items"][-1][
        "evidence"
    ]["evaluation"] == ev
    g = client.post(f"{M}/models/{uid}/explanations", json={"scope": "GLOBAL", "method": "stored_global"}).json()
    assert g["status"] == "ok" and g["model_version"] == uid
    assert (
        client.post(f"{M}/models/{uid}/explanations", json={"scope": "GLOBAL", "method": "shap"}).json()["status"]
        == "unsupported"
    )
    assert (
        client.post(f"{M}/models/{uid}/explanations", json={"scope": "LOCAL", "method": "stored_local"}).status_code
        == 422
    )
    assert client.get(f"{M}/models/{uid}/explanations").json()["total"] == 1
    mon = client.post(f"{M}/models/{uid}/monitoring/checks", json={}).json()
    assert mon["overall"] in ("ok", "warning", "alert") and "does not show" in mon["statement"]
    assert client.get(f"{M}/models/{uid}/monitoring").json()["total"] >= 3
    assert client.get(f"{M}/models/{uid}/card").json()["governance"]["state"] == "APPROVED"


def test_compare_flags_incompatible_models(client):
    leadtime_env(client)
    client.post(f"{M}/models/sync")
    _, items = _lead_uid(client)
    uids = [i["model_uid"] for i in items[:3]]
    j = client.get(f"{M}/compare?ids={uids[0]},{uids[1]}").json()
    assert j["comparable"] is False and j["warnings"] and all(i["evaluation"] is None for i in j["items"])
    assert client.get(f"{M}/compare?ids={uids[0]}").status_code == 422
    assert client.get(f"{M}/compare?ids={uids[0]},MV-NOPE").status_code == 404


def test_validation_and_unknown_fields(client):
    leadtime_env(client)
    client.post(f"{M}/models/sync")
    uid, _ = _lead_uid(client)
    for body in (
        {"organization_id": 1},
        {"kind": "FUTURE"},
        {"segment_by": ["ssn"]},
        {"min_segment_n": 1},
        {"path": "/etc/passwd"},
    ):
        assert client.post(f"{M}/models/{uid}/evaluations", json=body).status_code == 422, body
    assert (
        client.post(f"{M}/models/{uid}/transition", json={"to_state": "APPROVED", "organization_id": 2}).status_code
        == 422
    )
    assert client.post(f"{M}/models/{uid}/transition", json={"to_state": "FLYING"}).status_code == 422
    assert client.post(f"{M}/models/{uid}/explanations", json={"method": "Bad Method!"}).status_code == 422
    assert client.post(f"{M}/models/{uid}/evaluations", json={}, headers={"Idempotency-Key": "x"}).status_code == 422
    e = client.post(f"{M}/models/{uid}/transition", json={"to_state": "ARCHIVED"}).json()
    assert "Traceback" not in str(e) and "/home/" not in str(e)


def test_roles_and_write_gate(client, secured, readonly):
    leadtime_env(client)
    assert secured.get(f"{M}/models").status_code == 401
    assert secured.post(f"{M}/models/sync", headers=h("viewer")).status_code == 403
    assert secured.post(f"{M}/models/sync", headers=h("editor")).status_code == 200
    uid, _ = _lead_uid_h(secured)
    assert secured.get(f"{M}/models/{uid}", headers=h("viewer")).status_code == 200
    assert secured.post(f"{M}/models/{uid}/evaluations", json={}, headers=h("viewer")).status_code == 403
    ev = secured.post(f"{M}/models/{uid}/evaluations", json={}, headers=h("editor")).json()["eval_uid"]
    note = {"to_state": "APPROVED", "note": "A sufficiently long approval rationale."}
    assert (
        secured.post(f"{M}/models/{uid}/transition", json=note, headers=h("editor")).status_code == 403
    )  # approval is a reviewer act
    assert secured.post(f"{M}/models/{uid}/transition", json=note, headers=h("reviewer")).status_code == 200
    assert secured.get(f"{M}/evaluations/{ev}", headers=h("viewer")).status_code == 200
    # cancel: only requester or a reviewer
    done = secured.post(f"{M}/evaluations/{ev}/cancel", headers=h("editor"))
    assert done.status_code == 409  # already COMPLETED
    assert readonly.get(f"{M}/health", headers=h("viewer")).json()["write_api_enabled"] is False
    assert readonly.post(f"{M}/models/sync", headers=h("admin")).status_code in (403, 405, 409)


def _lead_uid_h(c):
    items = c.get(f"{M}/models?limit=100", headers=h("viewer")).json()["items"]
    return next(i for i in items if i["domain"] == "LEAD_TIME")["model_uid"], items


def test_audit_records_governance_actions(client, secured):
    leadtime_env(client)
    secured.post(f"{M}/models/sync", headers=h("editor"))
    uid, _ = _lead_uid_h(secured)
    secured.post(f"{M}/models/{uid}/evaluations", json={}, headers=h("editor"))
    secured.post(
        f"{M}/models/{uid}/transition",
        json={"to_state": "APPROVED", "note": "Documented approval rationale."},
        headers=h("reviewer"),
    )
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.registry import AuditEvent

    with Session(secured.app.state.engine) as s:
        acts = {a.action for a in s.scalars(select(AuditEvent))}
    assert {"modelops.sync", "modelops.evaluation.queued", "modelops.transition"} <= acts
