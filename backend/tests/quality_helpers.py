"""Helpers for data-quality tests (synthetic data only)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.models.enums import EvalScope
from app.quality import engine
from app.registry.config import quality_config

Q = "/api/v1/quality"
TOK = {
    "viewer": "v" * 20 + "tokenAAAA",
    "editor": "e" * 20 + "tokenBBBB",
    "reviewer": "r" * 20 + "tokenCCCC",
    "admin": "a" * 20 + "tokenDDDD",
}
TABLE = ",".join(f"{t}:{r}-user:{r}" for r, t in TOK.items())


def h(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOK[role]}"}


def evaluate(client: TestClient, scope: EvalScope = EvalScope.NETWORK, ref: dict[str, Any] | None = None, **kw: Any):
    s = client.app.state.settings
    return engine.evaluate_now(
        client.app.state.session_factory, scope, ref, "test", quality_config(s.quality_config_path), **kw
    )


def findings(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    r = client.get(f"{Q}/findings", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def facility_with_coords(
    client: TestClient, name: str, lat: float | None, lon: float | None, **kw: Any
) -> dict[str, Any]:
    from tests.registry_helpers import mk

    body = {"name": name, "facility_type": "MANUFACTURING"} | kw
    if lat is not None:
        body |= {"latitude": lat, "longitude": lon}
    return mk(client, "facilities", **body)


def db_supplier(client: TestClient, name: str, **kw: Any) -> int:
    """Insert a supplier straight into the database (bypasses API cleaning and the re-evaluation hook)."""
    from sqlalchemy.orm import Session

    from app.models.enums import EntityStatus, Provenance
    from app.models.supplier import Supplier

    code = f"SUP-T-{abs(hash(name)) % 10**8}"
    with Session(client.app.state.engine) as s, s.begin():
        row = Supplier(
            supplier_code=code,
            name=name,
            country_or_region="Germany",
            category="Electronics",
            status=EntityStatus.ACTIVE,
            provenance=Provenance.USER_PROVIDED,
            name_normalized=name.strip().lower(),
            **kw,
        )
        s.add(row)
        s.flush()
        return row.id
