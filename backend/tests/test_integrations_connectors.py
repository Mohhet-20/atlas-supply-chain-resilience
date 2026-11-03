"""Integrations: connector framework - allow-list, generic REST connector against a fake transport, SSRF, error classification."""

from __future__ import annotations

import json

import pytest

from app.core.errors import DomainValidationError
from app.integrations import connectors as C
from app.integrations.connectors import ConnectorError, RestJson
from tests.integrations_helpers import HOST

HOSTS = {HOST}
URL = f"https://{HOST}/api/stock"


class FakeGetter:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers, timeout, max_bytes):
        self.calls.append({"url": url, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body = r
        return status, (body if isinstance(body, bytes) else json.dumps(body).encode())


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    real = C.validate_webhook_url
    monkeypatch.setattr(C, "validate_webhook_url", lambda url, hosts, resolve=True: real(url, hosts, resolve=False))


def conn(*responses):
    return RestJson(FakeGetter(*responses))


CFG = {"url": URL, "records_path": "data", "next_cursor_path": "next", "cursor_param": "after", "since_param": "since"}


def test_allow_list_is_fixed_and_unknown_or_extension_keys_are_refused():
    assert set(C.CONNECTORS) == {"sandbox_inventory", "rest_json"}
    for key in ("sap", "oracle_erp", "wms", "os.system", "__import__('os')", "app.integrations.connectors.RestJson"):
        with pytest.raises(DomainValidationError):
            C.get_connector(key)
    assert {e.key for e in C.EXTENSION_POINTS} >= {"sap", "oracle_erp", "dynamics365", "coupa", "ariba", "wms", "tms"}
    assert all(e.status == "extension_point" and "not implemented" in e.description for e in C.EXTENSION_POINTS)
    assert C.CONNECTORS["sandbox_inventory"].info.status == "sandbox"


def test_valid_connection_sends_bearer_credential_and_pages_with_cursor_and_watermark():
    c = conn((200, {"data": [{"id": 1}], "next": "abc"}), (200, {"data": [{"id": 2}], "next": None}))
    p1 = c.fetch(CFG, "tok-123", None, "2026-01-01T00:00:00+00:00", 100, HOSTS)
    p2 = c.fetch(CFG, "tok-123", p1.next_cursor, None, 100, HOSTS)
    assert [r["id"] for r in p1.records + p2.records] == [1, 2] and p2.next_cursor is None
    first, second = c.getter.calls
    assert (
        first["headers"]["Authorization"] == "Bearer tok-123"
        and "since=2026-01-01" in first["url"]
        and "limit=100" in first["url"]
    )
    assert "after=abc" in second["url"]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "auth"),
        (429, "transient"),
        (503, "transient"),
        (408, "transient"),
        (404, "permanent"),
        (400, "permanent"),
    ],
)
def test_provider_errors_are_classified_so_the_engine_knows_whether_to_retry(status, kind):
    with pytest.raises(ConnectorError) as e:
        conn((status, b"")).fetch(CFG, "tok-secret-9", None, None, 10, HOSTS)
    assert e.value.kind == kind and "tok-secret-9" not in e.value.message


def test_timeouts_malformed_and_oversized_responses():
    with pytest.raises(ConnectorError) as e:
        conn(ConnectorError("transient", "network error or timeout")).fetch(CFG, "t", None, None, 10, HOSTS)
    assert e.value.kind == "transient"
    with pytest.raises(ConnectorError) as e:
        conn((200, b"<html>not json")).fetch(CFG, "t", None, None, 10, HOSTS)
    assert e.value.kind == "permanent"
    with pytest.raises(ConnectorError):
        conn((200, {"data": "not a list"})).fetch(CFG, "t", None, None, 10, HOSTS)
    with pytest.raises(ConnectorError) as e:
        conn((200, b"x" * (C.MAX_RESPONSE_BYTES + 5))).fetch(CFG, "t", None, None, 10, HOSTS)
    assert "size" in e.value.message


def test_the_credential_is_never_included_in_error_messages_or_test_results():
    res = conn((401, b"")).test_connection(CFG, "SUPER-SECRET-TOKEN", HOSTS)
    assert res.ok is False and "SUPER-SECRET-TOKEN" not in res.detail
    ok = conn((200, {"data": []})).test_connection(CFG, "SUPER-SECRET-TOKEN", HOSTS)
    assert ok.ok and "SUPER-SECRET-TOKEN" not in ok.detail


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/x",
        "https://other.example.org/x",
        "https://u:p@hooks.example.com/x",
    ],
)
def test_rest_connector_refuses_unsafe_destinations_in_config_and_at_call_time(url):
    c = conn()
    assert c.validate_config({"url": url}, HOSTS)
    with pytest.raises(ConnectorError) as e:
        c.fetch({**CFG, "url": url}, "t", None, None, 10, HOSTS)
    assert e.value.kind == "permanent" and c.getter.calls == []  # nothing was sent


def test_unknown_settings_are_rejected():
    assert any("unknown" in e for e in RestJson().validate_config({"url": URL, "exec": "rm -rf /"}, HOSTS))
    assert RestJson().validate_config(CFG, HOSTS) == []
