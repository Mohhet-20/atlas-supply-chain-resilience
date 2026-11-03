"""Synthetic fixtures for registry tests (no real company data)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import DataSource, SupplyRelationship
from app.models.enums import Provenance, RelationshipType

API = "/api/v1/registry"


def mk(client: TestClient, kind: str, headers: dict[str, str] | None = None, **fields: Any) -> dict[str, Any]:
    base: dict[str, dict[str, Any]] = {
        "suppliers": {"country_or_region": "Germany", "category": "Electronics"},
        "facilities": {"country_or_region": "Germany", "facility_type": "FACTORY"},
        "products": {"category": "Components"},
    }
    r = client.post(f"{API}/{kind}", json=base[kind] | fields, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def source_id(engine: Engine) -> int:
    with Session(engine) as s:
        return s.scalars(select(DataSource.id).order_by(DataSource.id)).first()


def link(engine: Engine, supplier_id: int, facility_id: int, code: str) -> int:
    with Session(engine) as s, s.begin():
        r = SupplyRelationship(
            relationship_code=code, relationship_type=RelationshipType.SUPPLIES, supplier_id=supplier_id,
            target_facility_id=facility_id, provenance=Provenance.USER_PROVIDED,
        )  # fmt: skip
        s.add(r)
        s.flush()
        return r.id


def merge_body(rtype: str, survivor: dict, merged: dict, **extra: Any) -> dict[str, Any]:
    return {
        "entity_type": rtype, "survivor_id": survivor["id"], "merged_id": merged["id"],
        "survivor_version": survivor["version"], "merged_version": merged["version"], "reason": "test merge",
    } | extra  # fmt: skip


def confirm_pair(client: TestClient, rtype: str, left: dict, right: dict, **kw: Any) -> int:
    r = client.post(f"{API}/candidates/generate", json={"entity_type": rtype})
    assert r.status_code == 200, r.text
    cands = client.get(
        f"{API}/candidates", params={"entity_type": rtype, "tier": ["STRONG", "POSSIBLE"], "limit": 100}
    ).json()["items"]
    ids = {left["id"], right["id"]}
    cand = next(c for c in cands if {c["left_id"], c["right_id"]} == ids)
    d = client.post(
        f"{API}/candidates/{cand['id']}/decision",
        json={"decision": "CONFIRMED_DUPLICATE", "reason": "same entity"} | kw,
    )
    assert d.status_code == 200, d.text
    return cand["id"]


def make_settings(settings, **overrides: Any):
    """Validated copy of test settings (model_copy would skip validators such as SecretStr coercion)."""
    from app.core.config import Settings

    base = {
        "environment": "test",
        "database_url": settings.database_url,
        "data_dir": settings.data_dir,
        "log_level": "WARNING",
    }
    return Settings(_env_file=None, **(base | overrides))
