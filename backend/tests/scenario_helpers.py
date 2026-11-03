"""Helpers for disruption-scenario tests (synthetic data only)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.scenarios import config as cfg
from app.scenarios.engine import EngineLimits, run_engine
from tests.graph_helpers import node, ok
from tests.quality_helpers import TABLE, TOK, h

SC = "/api/v1/scenarios"
__all__ = ["SC", "TABLE", "TOK", "h"]


def ev(event_type: str, uid: str = "EV-1", **kw: Any) -> dict[str, Any]:
    base = {
        "event_uid": uid, "event_type": event_type, "label": None, "target_type": None, "target_id": None, "relationship_ref": None,
        "targets": None, "region_label": None, "effect": None, "magnitude_pct": None, "added_lead_time_days": None,
        "start_offset_days": None, "duration_days": 14, "recovery_days": None, "position": 0,
    }  # fmt: skip
    return base | kw


def sup_out(node_: dict, uid: str = "EV-1", **kw: Any) -> dict[str, Any]:
    return ev("SUPPLIER_UNAVAILABLE", uid, target_type="SUPPLIER", target_id=node_["id"], **kw)


def definition(events: list[dict[str, Any]], **assumptions: Any) -> dict[str, Any]:
    return {
        "scenario": {"scenario_type": "CUSTOM", "assumptions": cfg.default_assumptions() | assumptions, "name": "t", "version": 1},
        "events": events,
    }  # fmt: skip


def simulate(client: TestClient, events: list[dict[str, Any]], limits: EngineLimits | None = None, **assumptions: Any):
    with Session(client.app.state.engine) as s:
        return run_engine(s, definition(events, **assumptions), limits or EngineLimits())


def by_name(result, name: str) -> dict[str, Any]:
    return next(i for i in result.impacts if i["entity_name"] == name)


def chain(client: TestClient, shares: tuple[float | None, float | None] = (60.0, 100.0)):
    """supplier A -> facility F1 -> facility F2 -> product P1(produced by F2); supplier B -> F1 too."""
    a = node(client, "suppliers", "Alder Parts")
    b = node(client, "suppliers", "Birch Parts")
    f1 = node(client, "facilities", "Cedar Plant")
    f2 = node(client, "facilities", "Dogwood Hub")
    p1 = node(client, "products", "Elm Widget")
    kw_a = {} if shares[0] is None else {"supply_share_pct": shares[0]}
    kw_b = {} if shares[0] is None else {"supply_share_pct": 100.0 - shares[0]}
    ok(client, "SUPPLIES", a, f1, **kw_a, capacity_value=100, capacity_unit="units/week")
    ok(client, "SUPPLIES", b, f1, **kw_b)
    kw_t = {} if shares[1] is None else {"supply_share_pct": shares[1]}
    ok(client, "TRANSFERS_TO", f1, f2, **kw_t)
    ok(client, "PRODUCES", f2, p1, supply_share_pct=100.0)
    return {"a": a, "b": b, "f1": f1, "f2": f2, "p1": p1}
