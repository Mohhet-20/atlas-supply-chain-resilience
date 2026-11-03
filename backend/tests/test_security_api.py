"""Identity API-hardening regression tests: headers, request-size limits (incl. chunked), CORS, safe errors, uploads, CSV export."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ingestion.export import neutralize, to_csv
from tests.quality_helpers import h
from tests.security_helpers import make_app

API = "/api/v1"


@pytest.fixture
def client(settings, engine):
    with TestClient(make_app(settings, engine)) as c:
        yield c


def test_security_headers_on_success_error_and_unauthenticated_responses(client):
    for r in (client.get(f"{API}/health"), client.get(f"{API}/registry/me"), client.get(f"{API}/no/such/route")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in r.headers  # only behind TLS in production


def test_hsts_is_sent_in_production(settings, engine):
    from tests.registry_helpers import make_settings

    s = make_settings(
        settings,
        environment="production",
        api_tokens=h("admin")["Authorization"][7:] + ":svc:admin",
        cors_allowed_origins=["https://atlas.example.com"],
        auth_scrypt_n=1024,
    )
    with TestClient(__import__("app.main", fromlist=["create_app"]).create_app(s, engine)) as c:
        assert "max-age=" in c.get(f"{API}/health").headers.get("strict-transport-security", "")


def test_oversized_json_body_is_rejected_with_413(client):
    big = b'{"x":"' + b"a" * (client.app.state.settings.max_request_bytes + 10) + b'"}'
    r = client.post(f"{API}/auth/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_chunked_body_cannot_bypass_the_size_limit(client):
    limit = client.app.state.settings.max_request_bytes

    def chunks():  # no Content-Length header: this is the V2 bypass
        for _ in range(limit // 1024 + 5):
            yield b"a" * 1024

    r = client.post(f"{API}/auth/login", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_upload_limit_applies_to_ingestion_endpoints(client):
    n = client.app.state.settings.max_upload_bytes + 1024
    r = client.post(
        f"{API}/ingestion/inspect?entity_type=SUPPLIER&format=CSV",
        content=b"a," * (n // 2),
        headers=h("editor") | {"content-type": "text/csv"},
    )
    assert r.status_code == 413


def test_upload_requires_a_supported_content_type_and_editor_role(client):
    base = f"{API}/ingestion/inspect?entity_type=SUPPLIER"
    assert (
        client.post(base, content=b"a,b\n1,2\n", headers=h("viewer") | {"content-type": "text/csv"}).status_code == 403
    )
    assert (
        client.post(
            base, content=b"MZ\x90\x00", headers=h("editor") | {"content-type": "application/x-msdownload"}
        ).status_code
        == 415
    )
    assert client.post(base, content=b"a,b\n1,2\n").status_code == 401


def test_upload_filename_is_display_only_and_traversal_is_harmless(client):
    r = client.post(
        f"{API}/ingestion/inspect?entity_type=SUPPLIER&filename=../../etc/passwd.csv",
        content=b"name\nAcme\n",
        headers=h("editor") | {"content-type": "text/csv"},
    )
    assert r.status_code in (200, 422)  # accepted as a label only; nothing is written to disk by name
    assert "etc/passwd" not in r.text or r.status_code == 422


@pytest.mark.parametrize("value", ["=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\t=1", "\r=1"])
def test_csv_export_neutralises_formula_injection(value):
    assert neutralize(value).startswith("'")
    out = to_csv(["col"], [[value]])
    cell = next(__import__("csv").reader(out.splitlines(keepends=True)[1:]))[0]
    assert cell.startswith("'") and cell.lstrip("'")[0:1] in ("=", "+", "-", "@", "\t", "\r")


def test_csv_export_keeps_normal_values_unchanged():
    assert neutralize("Acme GmbH") == "Acme GmbH" and neutralize(12) == "12" and neutralize(None) == ""


def test_cors_rejects_unlisted_origins_and_never_allows_credentials_wildcard(client):
    r = client.options(
        f"{API}/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"
    assert r.headers.get("access-control-allow-origin") != "*"


def test_production_refuses_wildcard_cors_and_missing_tokens(settings):
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            database_url=settings.database_url,
            data_dir=settings.data_dir,
            cors_allowed_origins=["*"],
        )


def test_errors_do_not_leak_internals(client):
    for r in (
        client.get(f"{API}/no/such/route", headers=h("viewer")),
        client.get(f"{API}/registry/audit?limit=abc", headers=h("reviewer")),
    ):
        text = r.text
        assert (
            "Traceback" not in text
            and "sqlalchemy" not in text.lower()
            and "/sessions/" not in text
            and 'File "' not in text
        )
    assert "request_id" in client.get(f"{API}/registry/audit?limit=abc", headers=h("reviewer")).text


def test_malformed_authorization_headers_are_unauthenticated_not_errors(client):
    for hv in ("Bearer", "Bearer ", "Basic abc", "bearer\x00x", "Bearer " + "A" * 5000, "Token abc"):
        try:
            r = client.get(f"{API}/registry/me", headers={"Authorization": hv})
        except Exception:  # noqa: S112 - an invalid header value rejected client-side is also acceptable
            continue
        assert r.status_code == 401, hv


def test_only_documented_auth_endpoints_are_public(client):
    public = []
    for path, ops in client.get("/openapi.json").json()["paths"].items():
        for m in ops:
            if m in ("get", "post", "put", "patch", "delete") and client.request(
                m.upper(), path.replace("{", "").replace("}", ""), json={}
            ).status_code not in (401, 404, 405):
                public.append((m, path))
    allowed = {
        "/api/v1/health",
        "/api/v1/readiness",
        "/api/v1/auth/login",
        "/api/v1/auth/password-reset/request",
        "/api/v1/auth/password-reset/confirm",
        "/api/v1/webhooks/inbound/{uid}",  # authenticated by HMAC signature; unsigned calls get 401 (test_integrations_webhooks)
    }
    assert {p for _, p in public} <= allowed


def test_findings_export_is_audited_with_row_count_and_actor(client):
    from sqlalchemy import select

    from app.models.registry import AuditEvent

    r = client.get(f"{API}/ingestion/findings/export.csv?severity=ERROR", headers=h("viewer"))
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    with client.app.state.session_factory() as db:
        ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "export.created")).all()
    assert len(ev) == 1 and ev[0].actor == "viewer-user" and ev[0].details["rows"] == 0
    assert ev[0].details["filters"] == {"severity": "ERROR"}
