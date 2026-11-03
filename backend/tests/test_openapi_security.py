"""The published OpenAPI schema must document authentication and the access each operation enforces."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

PUBLIC = {"/api/v1/health", "/api/v1/readiness", "/api/v1/auth/login"}


def _schema(settings_factory=None):
    from app.core.config import Settings

    app = create_app(Settings(environment="development"))
    return app, app.openapi()


def test_bearer_scheme_declared_and_every_operation_has_security_decision():
    _app, oa = _schema()
    assert oa["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    for path, ops in oa["paths"].items():
        for method, op in ops.items():
            assert "security" in op, (method, path)
    public = {p for p, ops in oa["paths"].items() for o in ops.values() if o["security"] == []}
    assert PUBLIC <= public
    assert not any(p.endswith("/suppliers") for p in public)


def test_required_role_matches_enforcement():
    """Documented access is read from the real dependencies (role and permission factories)."""
    _app, oa = _schema()
    post = oa["paths"]["/api/v1/ingestion/sources"]["post"]
    assert "role:editor" in post["x-required-access"]
    get = oa["paths"]["/api/v1/suppliers"]["get"]
    assert "role:viewer" in get["x-required-access"]
    assert any(
        "permission:integration.admin" in o.get("x-required-access", [])
        for ops in oa["paths"].values()
        for o in ops.values()
    )


def test_description_no_longer_claims_missing_authentication():
    app, _ = _schema()
    assert "no authentication yet" not in app.description.lower()
    with TestClient(app) as c:
        assert c.get("/api/v1/health").status_code == 200
