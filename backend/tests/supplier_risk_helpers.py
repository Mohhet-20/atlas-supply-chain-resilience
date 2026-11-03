"""Helpers for supplier-risk tests (synthetic data only)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import RiskRunScope
from app.risk import inputs, runs
from app.risk.methodology import RiskConfig, risk_config
from app.risk.scoring import assess
from tests.graph_helpers import node, ok
from tests.quality_helpers import TABLE, TOK, h

R = "/api/v1/risk"
__all__ = ["R", "TABLE", "TOK", "h"]

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 22)


def cfg_of(client: TestClient) -> RiskConfig:
    return risk_config(client.app.state.settings.risk_config_path)


def assess_all(
    client: TestClient, scope: RiskRunScope = RiskRunScope.NETWORK, ref: dict[str, Any] | None = None, **kw: Any
):
    return runs.assess_now(client.app.state.session_factory, scope, ref, "test", cfg_of(client), **kw)


def engine_result(client: TestClient, supplier_id: int, cfg: RiskConfig | None = None, now: datetime = NOW):
    """Run the pure engine over the current database state for one supplier."""
    with Session(client.app.state.engine) as s:
        facts, net = inputs.load_facts(s, [supplier_id], now.date())
        return assess(facts[supplier_id], net, now.date(), now, cfg or cfg_of(client))


def factor(result, category: str):
    return next(f for f in result.factors if f.category.value == category)


def current(client: TestClient, supplier_id: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = client.get(f"{R}/suppliers/{supplier_id}", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def two_supplier_site(
    client: TestClient, *, share_a: float | None = None, share_b: float | None = None, crit: str | None = None
):
    """Two suppliers feeding one facility; returns (a, b, facility)."""
    a = node(client, "suppliers", "Alder Parts")
    b = node(client, "suppliers", "Birch Parts")
    f = node(client, "facilities", "Cedar Plant")
    ka = {k: v for k, v in {"supply_share_pct": share_a, "criticality": crit}.items() if v is not None}
    kb = {k: v for k, v in {"supply_share_pct": share_b, "criticality": crit}.items() if v is not None}
    ok(client, "SUPPLIES", a, f, **ka)
    ok(client, "SUPPLIES", b, f, **kb)
    return a, b, f
