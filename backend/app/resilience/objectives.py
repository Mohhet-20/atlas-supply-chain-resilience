"""Objective registry: every objective declares id, definition, direction, unit, inputs, normalisation, missing-data behaviour and
whether it is available for the problem at hand. An unavailable objective is never replaced by zero."""

from __future__ import annotations

from typing import Any

# direction: "min" or "max" (as the user reads it). Internally everything is converted to minimisation (see sign()).
REGISTRY: list[dict[str, Any]] = [
    {
        "id": "COST",
        "name": "Total modelled cost",
        "direction": "min",
        "unit": "currency",
        "definition": "Purchase price x quantity, plus stated freight when every eligible offer states it, plus shortage cost x baseline unmet units when a shortage cost is supplied.",
        "inputs": ["unit price of every eligible offer"],
        "normalisation": "min-max over the feasible candidates of the run",
        "missing": "Unavailable when an eligible offer has no price. Omitted components (freight, shortage cost) are listed, never costed at zero.",
        "explanation": "What the plan is expected to cost under normal conditions, using only the cost inputs that exist.",
    },
    {
        "id": "UNMET_DEMAND",
        "name": "Unmet demand (normal conditions)",
        "direction": "min",
        "unit": "units",
        "definition": "max(0, demand - units delivered inside the horizon - usable opening stock).",
        "inputs": ["demand", "capacities", "lead times"],
        "normalisation": "min-max over the feasible candidates",
        "missing": "Always available. Constant across candidates when full fulfilment is required and lead times fit the horizon (then it does not discriminate).",
        "explanation": "Demand the plan fails to meet even when nothing goes wrong. Unit-based, not an order fill rate.",
    },
    {
        "id": "DELIVERY_DELAY",
        "name": "Quantity-weighted lead time",
        "direction": "min",
        "unit": "days",
        "definition": "sum(quantity x offer lead time) / sum(quantity).",
        "inputs": ["lead time of every eligible offer"],
        "normalisation": "min-max over the feasible candidates",
        "missing": "Unavailable when an eligible offer has no lead time.",
        "explanation": "Average stated lead time of the plan. These are stated offer terms, not predicted delays or observed lateness.",
    },
    {
        "id": "CONCENTRATION",
        "name": "Supplier concentration (HHI)",
        "direction": "min",
        "unit": "index 0-1",
        "definition": "Herfindahl-Hirschman index of supplier shares of this requirement: sum(share^2).",
        "inputs": ["allocation shares"],
        "normalisation": "min-max over the feasible candidates",
        "missing": "Always available.",
        "explanation": "How dependent the plan is on few suppliers. Suppliers sharing an upstream dependency or region are not detected.",
    },
    {
        "id": "DISRUPTION_EXPOSURE",
        "name": "Single-supplier outage exposure",
        "direction": "min",
        "unit": "units",
        "definition": "Largest unmet demand over every outage of one supplier used by the plan (fixed or adaptive per the run's mode).",
        "inputs": ["allocation", "capacities of the other eligible suppliers", "opening stock"],
        "normalisation": "min-max over the feasible candidates",
        "missing": "Always available. Needs response_days for the adaptive mode.",
        "explanation": "How much demand goes unmet if the single worst-placed supplier of this plan fails.",
    },
    {
        "id": "WORST_CASE_SERVICE",
        "name": "Worst-case fulfilment across scenarios",
        "direction": "max",
        "unit": "fraction of demand",
        "definition": "min over the evaluated scenarios of 1 - unmet / scenario demand.",
        "inputs": ["at least one stress scenario"],
        "normalisation": "min-max over the feasible candidates",
        "missing": "Unavailable without stress scenarios.",
        "explanation": "Lowest share of demand met across the scenarios you chose. It describes only those scenarios.",
    },
]
BY_ID = {o["id"]: o for o in REGISTRY}


def sign(oid: str) -> int:
    """+1 when smaller is better, -1 when larger is better (internal minimisation)."""
    return 1 if BY_ID[oid]["direction"] == "min" else -1


def availability(problem: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """problem: {'eligible': [supplier dicts], 'has_stress': bool}. Returns id -> {available, reason}."""
    el = problem["eligible"]
    out: dict[str, dict[str, Any]] = {}
    no_price = [e["code"] for e in el if e.get("price") is None]
    no_lead = [e["code"] for e in el if e.get("lead_days") is None]
    out["COST"] = {"available": bool(el) and not no_price, "reason": None if el and not no_price else ("no eligible offers" if not el else f"no unit price for: {', '.join(no_price)}")}  # fmt: skip
    out["UNMET_DEMAND"] = {"available": bool(el), "reason": None if el else "no eligible offers"}
    out["DELIVERY_DELAY"] = {"available": bool(el) and not no_lead, "reason": None if el and not no_lead else ("no eligible offers" if not el else f"no lead time for: {', '.join(no_lead)}")}  # fmt: skip
    out["CONCENTRATION"] = {"available": bool(el), "reason": None if el else "no eligible offers"}
    out["DISRUPTION_EXPOSURE"] = {"available": bool(el), "reason": None if el else "no eligible offers"}
    out["WORST_CASE_SERVICE"] = {"available": bool(el) and problem["has_stress"], "reason": None if (el and problem["has_stress"]) else ("no stress scenario selected" if el else "no eligible offers")}  # fmt: skip
    return out
