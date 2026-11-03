"""SYNTHETIC helpers for the integration tests: an app with a master key, a recording webhook sender and org helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from fastapi.testclient import TestClient

from app.alerting.notify import SendResult
from tests.quality_helpers import h
from tests.security_helpers import make_app as _make_app
from tests.security_helpers import mk_org, mk_user

API = "/api/v1"
MASTER = "synthetic-master-key-for-tests-only-0123456789"
HOST = "hooks.example.com"
URL = f"https://{HOST}/atlas"


class FakeSender:
    """Records every outbound delivery; `script` is a list of SendResult consumed in order (default: accepted)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[SendResult] = []

    def send(self, url: str, body: bytes, timeout: float, headers: dict[str, str]) -> SendResult:
        self.calls.append({"url": url, "body": body, "headers": headers})
        return self.script.pop(0) if self.script else SendResult("accepted", "ok", None)


def make_app(settings, engine, **kw: Any):
    kw.setdefault("integrations_master_key", MASTER)
    kw.setdefault("integration_hosts", HOST)
    app = _make_app(settings, engine, **kw)
    app.state.webhook_sender = FakeSender()
    app.state.webhook_resolve = False
    return app


def client_for(settings, engine, **kw: Any) -> TestClient:
    return TestClient(make_app(settings, engine, **kw))


def admin() -> dict[str, str]:
    return h("admin")


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def post_inbound(
    c: TestClient, uid: str, secret: str, event: dict[str, Any], ts: int | None = None, sig: str | None = None
):
    body = json.dumps(event).encode()
    return c.post(
        f"{API}/webhooks/inbound/{uid}",
        content=body,
        headers={"X-Atlas-Signature": sig or sign(secret, body, ts), "content-type": "application/json"},
    )


def mk_integration(c: TestClient, headers=None, **over: Any) -> dict[str, Any]:
    body = {"name": "Sandbox feed", "provider": "sandbox_inventory", "config": {"records": 12}} | over
    r = c.post(f"{API}/integrations", json=body, headers=headers or admin())
    assert r.status_code == 201, r.text
    return r.json()


def connect(c: TestClient, uid: str, headers=None) -> None:
    hd = headers or admin()
    assert c.post(f"{API}/integrations/{uid}/validate", headers=hd).json()["valid"]
    assert c.post(f"{API}/integrations/{uid}/test", headers=hd).json()["ok"]


def enable(c: TestClient, uid: str, headers=None) -> None:
    connect(c, uid, headers)
    assert c.post(f"{API}/integrations/{uid}/enable", headers=headers or admin()).status_code == 200


__all__ = [
    "API",
    "FakeSender",
    "admin",
    "client_for",
    "connect",
    "enable",
    "h",
    "make_app",
    "mk_integration",
    "mk_org",
    "mk_user",
    "post_inbound",
    "sign",
    "URL",
]
