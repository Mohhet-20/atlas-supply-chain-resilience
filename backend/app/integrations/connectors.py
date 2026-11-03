"""Connector framework: a fixed, in-code allow-list of connector implementations.

A connector is selected by a *key* stored on the integration row and looked up in ``CONNECTORS``; the database never supplies a
module path, class name or code. Adding a connector means adding code and registering it here in a reviewed change.

Status of what ships (see docs/api.md):
* ``sandbox_inventory`` - SANDBOX. Deterministic synthetic records generated in-process. Not a live system.
* ``rest_json``         - generic REST/JSON pull connector. Implemented and tested against a fake transport only; no live
                          provider has been contacted.
* SAP, Oracle, Dynamics 365, Coupa, Ariba, warehouse, carrier and BI systems are EXTENSION POINTS: listed in the catalogue so the
  gap is visible, not creatable, and not implemented.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.alerting.notify import UnsafeDestination, _NoRedirect, validate_webhook_url
from app.core.errors import DomainValidationError

MAX_RESPONSE_BYTES = 1_048_576
CATEGORIES = ("erp", "procurement", "inventory", "logistics", "supplier", "analytics", "generic")


class ConnectorError(Exception):
    """kind: transient (retry), permanent (give up), auth (credentials rejected - give up, do not retry)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class Page:
    records: list[dict[str, Any]]
    next_cursor: str | None = None


@dataclass
class TestResult:
    ok: bool
    detail: str
    latency_ms: int | None = None


@dataclass(frozen=True)
class ConnectorInfo:
    key: str
    name: str
    category: str
    provider: str
    description: str
    status: str  # sandbox | implemented | extension_point
    version: str
    capabilities: tuple[str, ...]
    needs_credential: bool = False
    config_fields: dict[str, str] = field(default_factory=dict)


class Connector(Protocol):
    info: ConnectorInfo

    def validate_config(self, config: dict[str, Any], hosts: set[str]) -> list[str]: ...
    def test_connection(self, config: dict[str, Any], credential: str | None, hosts: set[str]) -> TestResult: ...
    def fetch(
        self,
        config: dict[str, Any],
        credential: str | None,
        cursor: str | None,
        since: str | None,
        limit: int,
        hosts: set[str],
    ) -> Page: ...


# ----------------------------------------------------------------- sandbox


class SandboxInventory:
    info = ConnectorInfo(
        key="sandbox_inventory", name="Sandbox inventory feed", category="inventory", provider="ATLAS sandbox",
        description="SANDBOX ONLY: deterministic synthetic stock-level records for developing and testing integrations. Not a live system.",
        status="sandbox", version="1.0", capabilities=("test_connection", "fetch", "paginate", "incremental"),
        config_fields={"records": "integer 1-5000", "facility": "text"},
    )  # fmt: skip
    BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def validate_config(self, config: dict[str, Any], hosts: set[str]) -> list[str]:
        errs = []
        n = config.get("records", 50)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 5000:
            errs.append("records: must be an integer from 1 to 5000")
        if not isinstance(config.get("facility", "F-SBX-1"), str):
            errs.append("facility: must be text")
        unknown = set(config) - {"records", "facility"}
        if unknown:
            errs.append(f"unknown settings: {', '.join(sorted(unknown))}")
        return errs

    def test_connection(self, config: dict[str, Any], credential: str | None, hosts: set[str]) -> TestResult:
        return TestResult(True, "sandbox adapter reachable (in-process; no external system)", 0)

    def fetch(
        self,
        config: dict[str, Any],
        credential: str | None,
        cursor: str | None,
        since: str | None,
        limit: int,
        hosts: set[str],
    ) -> Page:
        total = int(config.get("records", 50))
        start = int(cursor or 0)
        facility = config.get("facility", "F-SBX-1")
        out = []
        for i in range(start, min(start + limit, total)):
            stamp = (self.BASE + timedelta(hours=i)).isoformat()
            if since and stamp <= since:
                continue
            out.append(
                {
                    "external_id": f"SBX-{i:05d}",
                    "updated_at": stamp,
                    "facility": facility,
                    "metric": "stock_level",
                    "quantity": 100 + (i * 7) % 90,
                    "unit": "units",
                }
            )
        nxt = start + limit
        return Page(out, str(nxt) if nxt < total else None)


# ----------------------------------------------------------------- generic REST/JSON


class Getter(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> tuple[int, bytes]: ...


class UrllibGetter:
    def get(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> tuple[int, bytes]:
        req = urllib.request.Request(  # noqa: S310
            url, headers={"Accept": "application/json", "User-Agent": "ATLAS-connector/1", **headers}
        )  # noqa: S310  (https + allow-list validated by the caller)
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:  # noqa: S310
                data = r.read(max_bytes + 1)
                return r.status, data
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ConnectorError("transient", "network error or timeout") from None


def _path(obj: Any, path: str | None) -> Any:
    for part in (path or "").split("."):
        if not part:
            continue
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


class RestJson:
    info = ConnectorInfo(
        key="rest_json", name="Generic REST/JSON pull", category="generic", provider="generic",
        description="Pulls JSON records from an allow-listed https endpoint with bearer-token auth and cursor pagination. Implemented and tested against a fake transport only; no live provider has been contacted.",
        status="implemented", version="1.0", capabilities=("test_connection", "fetch", "paginate", "incremental"), needs_credential=True,
        config_fields={"url": "https URL on the host allow-list", "records_path": "dotted path to the list", "cursor_param": "query parameter", "next_cursor_path": "dotted path", "since_param": "query parameter", "limit_param": "query parameter"},
    )  # fmt: skip
    ALLOWED = {
        "url",
        "records_path",
        "cursor_param",
        "next_cursor_path",
        "since_param",
        "limit_param",
        "timeout_seconds",
    }

    def __init__(self, getter: Getter | None = None) -> None:
        self.getter = getter or UrllibGetter()

    def validate_config(self, config: dict[str, Any], hosts: set[str]) -> list[str]:
        errs = (
            [f"unknown settings: {', '.join(sorted(set(config) - self.ALLOWED))}"] if set(config) - self.ALLOWED else []
        )
        try:
            validate_webhook_url(str(config.get("url", "")), hosts, resolve=False)
        except UnsafeDestination as e:
            errs.append(f"url: {e}")
        return errs

    def _call(self, config: dict[str, Any], credential: str | None, params: dict[str, str], hosts: set[str]) -> Any:
        from urllib.parse import urlencode

        url = str(config["url"])
        validate_webhook_url(url, hosts, resolve=True)  # re-checked at call time: DNS may have changed
        qs = urlencode({k: v for k, v in params.items() if v})
        full = f"{url}{'&' if '?' in url else '?'}{qs}" if qs else url
        headers = {"Authorization": f"Bearer {credential}"} if credential else {}
        status, body = self.getter.get(full, headers, float(config.get("timeout_seconds", 10)), MAX_RESPONSE_BYTES)
        if status in (401, 403):
            raise ConnectorError("auth", f"provider rejected the credentials (HTTP {status})")
        if status in (408, 425, 429) or status >= 500:
            raise ConnectorError("transient", f"provider error (HTTP {status})")
        if status >= 400:
            raise ConnectorError("permanent", f"provider refused the request (HTTP {status})")
        if len(body) > MAX_RESPONSE_BYTES:
            raise ConnectorError("permanent", "response larger than the size limit")
        try:
            return json.loads(body or b"null")
        except ValueError:
            raise ConnectorError("permanent", "provider returned invalid JSON") from None

    def test_connection(self, config: dict[str, Any], credential: str | None, hosts: set[str]) -> TestResult:
        import time

        t0 = time.monotonic()
        try:
            self._call(config, credential, {config.get("limit_param", "limit"): "1"}, hosts)
        except UnsafeDestination as e:
            return TestResult(False, f"blocked: {e}")
        except ConnectorError as e:
            return TestResult(False, e.message)
        return TestResult(True, "connection succeeded", int((time.monotonic() - t0) * 1000))

    def fetch(
        self,
        config: dict[str, Any],
        credential: str | None,
        cursor: str | None,
        since: str | None,
        limit: int,
        hosts: set[str],
    ) -> Page:
        params = {config.get("limit_param", "limit"): str(limit)}
        if cursor:
            params[config.get("cursor_param", "cursor")] = cursor
        if since and config.get("since_param"):
            params[config["since_param"]] = since
        try:
            data = self._call(config, credential, params, hosts)
        except UnsafeDestination as e:
            raise ConnectorError("permanent", f"blocked: {e}") from None
        records = _path(data, config.get("records_path")) if config.get("records_path") else data
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise ConnectorError("permanent", "records_path did not point to a list of objects")
        nxt = _path(data, config.get("next_cursor_path")) if config.get("next_cursor_path") else None
        return Page(records, str(nxt) if nxt else None)


# ----------------------------------------------------------------- registry

CONNECTORS: dict[str, Connector] = {"sandbox_inventory": SandboxInventory(), "rest_json": RestJson()}  # type: ignore[dict-item]

EXTENSION_POINTS: tuple[ConnectorInfo, ...] = tuple(
    ConnectorInfo(key=k, name=n, category=c, provider=p, description=f"EXTENSION POINT: {n} is not implemented. It needs a reviewed connector class, real credentials and a successful connection test before it can be offered.", status="extension_point", version="-", capabilities=())
    for k, n, c, p in (
        ("sap", "SAP ERP", "erp", "SAP"), ("oracle_erp", "Oracle ERP", "erp", "Oracle"), ("dynamics365", "Microsoft Dynamics 365", "erp", "Microsoft"),
        ("coupa", "Coupa", "procurement", "Coupa"), ("ariba", "SAP Ariba", "procurement", "SAP"),
        ("wms", "Warehouse management system", "inventory", "(any)"), ("tms", "Transportation / carrier system", "logistics", "(any)"),
        ("supplier_portal", "Supplier information system", "supplier", "(any)"), ("bi_export", "BI / analytics push", "analytics", "(any)"),
    )
)  # fmt: skip


def catalogue() -> list[ConnectorInfo]:
    return [c.info for c in CONNECTORS.values()] + list(EXTENSION_POINTS)


def get_connector(key: str) -> Connector:
    c = CONNECTORS.get(key)
    if c is None:
        ext = next((e for e in EXTENSION_POINTS if e.key == key), None)
        raise DomainValidationError(
            f"{ext.name} is an extension point and is not implemented." if ext else f"Unknown connector '{key}'."
        )
    return c
