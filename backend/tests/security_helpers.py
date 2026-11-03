"""SYNTHETIC helpers for the identity security tests: an app with users, organisations and sessions."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.auth import service as svc
from app.core.auth import Actor, Role
from app.main import create_app
from tests.alerting_helpers import FakeProvider
from tests.quality_helpers import TABLE
from tests.registry_helpers import make_settings

A = "/api/v1/auth"
ADM = "/api/v1/admin"
AL = "/api/v1/alerting"
PW = "Synthetic-Pass-Phrase-41!"
OPERATOR = Actor("test-operator", Role.ADMIN, True, platform_admin=True)


def make_app(settings, engine, **kw: Any):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, auth_scrypt_n=1024, **kw)
    app = create_app(s, engine)
    app.state.alert_provider = FakeProvider()
    return app


def mk_user(client: TestClient, username: str, role: str = "viewer", org_id: int = 1, password: str = PW, **kw: Any):
    with client.app.state.session_factory() as db:
        u = svc.create_user(
            db, client.app.state.settings, None, username=username, password=password, role=role, org_id=org_id, **kw
        )
        db.commit()
        return u.user_uid


def mk_org(client: TestClient, slug: str) -> int:
    with client.app.state.session_factory() as db:
        org = svc.create_org(db, OPERATOR, slug, slug.title() + " Org")
        db.commit()
        return org.id


def login(client: TestClient, username: str, password: str = PW):
    return client.post(f"{A}/login", json={"username": username, "password": password})


def bearer(client: TestClient, username: str, password: str = PW) -> dict[str, str]:
    r = login(client, username, password)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}
