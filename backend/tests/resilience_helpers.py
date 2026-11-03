"""SYNTHETIC fixtures for resilience-optimisation tests. Every supplier, price and capacity is invented; results verify the software only."""

from __future__ import annotations

from typing import Any

from app.resilience.config import Limits, ResilienceRequest, Stress, stable_hash

LIM = Limits()
R = "/api/v1/resilience"


def sup(i: int, price: float | None = 10.0, cap: float = 100.0, lead: float | None = 10.0, freight: float | None = None, status: str = "ELIGIBLE", **kw: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "uid": f"OF-{i}", "supplier_id": i, "code": f"S{i}", "name": f"Supplier {i}", "country": kw.pop("country", f"C{i}"), "status": status, "reasons": [],
        "price": price, "freight": freight, "capacity": cap, "moq": kw.pop("moq", None), "lot": kw.pop("lot", None), "lead_days": lead, "lead_std_days": None,
        "risk": None, "reliability": {"value": None, "n": 0}, "provenance": "SYNTHETIC_DEMO", "offer_status": "ACTIVE", **kw,
    }  # fmt: skip


def outage(name: str, *uids: str, prob: float | None = None) -> dict[str, Any]:
    return {"name": name, "kind": "SUPPLIER_OUTAGE", "probability": prob, "demand_multiplier": 1.0, "outage": None, "applied": [], "not_applied": [],
            "effects": {u: {"available": False, "capacity": None, "lead_days": None} for u in uids}}  # fmt: skip


def snap(suppliers: list[dict[str, Any]], demand: float = 60.0, scenarios: list[dict[str, Any]] | None = None, stock: float | None = None, anchors: list[Any] | None = None) -> dict[str, Any]:  # fmt: skip
    s = {"as_of": "2026-11-01T00:00:00+00:00", "product_id": 1, "destination_facility_id": 1, "unit": "kg", "currency": "USD", "demand": demand, "suppliers": suppliers,
         "scenarios": scenarios or [], "opening_stock": {"value": stock, "source": None if stock is None else "USER_SUPPLIED", "observed_at": None}, "anchors": anchors or [], "warnings": []}  # fmt: skip
    s["hash"] = stable_hash(s)
    return s


def req(demand: float = 60.0, **kw: Any) -> ResilienceRequest:
    base: dict[str, Any] = dict(
        product_id=1, destination_facility_id=1, quantity=demand, unit="kg", currency="USD", grid_steps=10
    )
    return ResilienceRequest(**(base | kw))


def stress(name: str = "A out", kind: str = "SUPPLIER_OUTAGE", **kw: Any) -> Stress:
    return Stress(name=name, kind=kind, **kw)


def objs(**weights: float) -> list[dict[str, Any]]:
    return [{"id": k, "weight": v} for k, v in weights.items()]
