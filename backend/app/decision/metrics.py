"""The metric registry: one definition per metric, shared by every plan and condition so values are only ever compared like for like.

``scope_dependent`` metrics are only comparable between plans for the same product, destination, quantity, unit and currency. ``scenario_only``
metrics (exposure counts) exist only under a disruption and have no baseline value; they are never compared with a zero.
"""

from __future__ import annotations

from typing import Any

REGISTRY: list[dict[str, Any]] = [
    {"id": "MODELLED_COST", "name": "Modelled cost", "category": "Cost", "unit": "currency", "direction": "min", "time_basis": "one planning period (the run's demand)",
     "definition": "Purchase cost of the plan, plus freight only where every used offer states it. The cost basis is recorded with each value; values with different bases are not compared.",
     "requires": "unit prices on the offers", "missing": "unavailable; never zero", "cost_sensitive": True, "scope_dependent": True, "scenario_only": False,
     "sources": ["RESILIENCE_RUN", "SOURCING_RUN"]},
    {"id": "UNMET_UNITS", "name": "Unmet demand", "category": "Service", "unit": "units", "direction": "min", "time_basis": "one planning period",
     "definition": "Units of the run's demand not delivered inside the horizon after opening stock. Under a disruption it is the shortfall of that plan under that condition.",
     "requires": "capacities and lead times", "missing": "unavailable", "cost_sensitive": False, "scope_dependent": True, "scenario_only": False,
     "sources": ["RESILIENCE_RUN", "SOURCING_RUN"]},
    {"id": "FULFILMENT", "name": "Demand fulfilled", "category": "Service", "unit": "fraction", "direction": "max", "time_basis": "one planning period",
     "definition": "1 - unmet units / demand for that plan under that condition. Only comparable between runs with the same demand.",
     "requires": "demand and delivered units", "missing": "unavailable", "cost_sensitive": False, "scope_dependent": True, "scenario_only": False,
     "sources": ["RESILIENCE_RUN", "SOURCING_RUN"]},
    {"id": "AVG_LEAD_TIME_DAYS", "name": "Average lead time", "category": "Service", "unit": "days", "direction": "min", "time_basis": "quantity-weighted over the plan",
     "definition": "Quantity-weighted lead time of the offers used.", "requires": "lead times on the offers", "missing": "unavailable", "cost_sensitive": False,
     "scope_dependent": False, "scenario_only": False, "sources": ["RESILIENCE_RUN", "SOURCING_RUN"]},
    {"id": "CONCENTRATION_HHI", "name": "Supplier concentration (HHI)", "category": "Network resilience", "unit": "index 0-1", "direction": "min", "time_basis": "this requirement only",
     "definition": "Herfindahl index of supplier shares within this requirement. It says nothing about the supplier's share of other products.", "requires": "an allocation",
     "missing": "unavailable", "cost_sensitive": False, "scope_dependent": False, "scenario_only": False, "sources": ["RESILIENCE_RUN", "SOURCING_RUN"]},
    {"id": "SUPPLIER_COUNT", "name": "Suppliers used", "category": "Network resilience", "unit": "count", "direction": "neutral", "time_basis": "this requirement only",
     "definition": "Number of suppliers with a positive allocation. Neither higher nor lower is better by itself.", "requires": "an allocation", "missing": "unavailable",
     "cost_sensitive": False, "scope_dependent": False, "scenario_only": False, "sources": ["SOURCING_RUN"]},
    {"id": "OUTAGE_EXPOSURE_UNITS", "name": "Single-supplier outage exposure", "category": "Network resilience", "unit": "units", "direction": "min", "time_basis": "one planning period",
     "definition": "Largest unmet quantity over the outage of any one used supplier.", "requires": "an allocation and capacities", "missing": "unavailable",
     "cost_sensitive": False, "scope_dependent": True, "scenario_only": False, "sources": ["RESILIENCE_RUN"]},
    {"id": "WORST_CASE_FULFILMENT", "name": "Worst-case fulfilment", "category": "Network resilience", "unit": "fraction", "direction": "max", "time_basis": "one planning period",
     "definition": "Lowest fulfilment of the plan across the stress conditions of its run. Unavailable when the run had no stress conditions.", "requires": "stress conditions",
     "missing": "unavailable", "cost_sensitive": False, "scope_dependent": True, "scenario_only": False, "sources": ["RESILIENCE_RUN"]},
    {"id": "AFFECTED_ENTITIES", "name": "Affected entities", "category": "Network resilience", "unit": "count", "direction": "min", "time_basis": "while the disruption lasts",
     "definition": "Registry entities the scenario reaches through the dependency graph (direct and downstream). Reachability is exposure, not a quantified loss.",
     "requires": "a finished scenario simulation", "missing": "unavailable", "cost_sensitive": False, "scope_dependent": False, "scenario_only": True, "sources": ["SIMULATION_RUN"]},
    {"id": "DIRECT_IMPACTS", "name": "Directly affected entities", "category": "Network resilience", "unit": "count", "direction": "min", "time_basis": "while the disruption lasts",
     "definition": "Entities named by the scenario's events.", "requires": "a finished scenario simulation", "missing": "unavailable", "cost_sensitive": False,
     "scope_dependent": False, "scenario_only": True, "sources": ["SIMULATION_RUN"]},
    {"id": "INDIRECT_IMPACTS", "name": "Indirectly affected entities", "category": "Network resilience", "unit": "count", "direction": "min", "time_basis": "while the disruption lasts",
     "definition": "Entities reached only through downstream (or upstream) dependencies.", "requires": "a finished scenario simulation", "missing": "unavailable", "cost_sensitive": False,
     "scope_dependent": False, "scenario_only": True, "sources": ["SIMULATION_RUN"]},
    {"id": "AFFECTED_PRODUCTS", "name": "Products at risk", "category": "Network resilience", "unit": "count", "direction": "min", "time_basis": "while the disruption lasts",
     "definition": "Products among the affected entities.", "requires": "a finished scenario simulation", "missing": "unavailable", "cost_sensitive": False,
     "scope_dependent": False, "scenario_only": True, "sources": ["SIMULATION_RUN"]},
    {"id": "AFFECTED_FACILITIES", "name": "Facilities affected", "category": "Network resilience", "unit": "count", "direction": "min", "time_basis": "while the disruption lasts",
     "definition": "Facilities among the affected entities.", "requires": "a finished scenario simulation", "missing": "unavailable", "cost_sensitive": False,
     "scope_dependent": False, "scenario_only": True, "sources": ["SIMULATION_RUN"]},
]  # fmt: skip

UNAVAILABLE: list[dict[str, str]] = [
    {
        "id": "TRANSPORT_COST",
        "name": "Transportation cost",
        "reason": "No attached evidence source reports transport cost on a comparable basis (freight appears only inside modelled cost when stated).",
    },
    {
        "id": "HOLDING_COST",
        "name": "Inventory holding cost",
        "reason": "Reported by replenishment runs, which this module does not read.",
    },
    {
        "id": "SHORTAGE_COST",
        "name": "Shortage cost",
        "reason": "Reported by replenishment runs, which this module does not read.",
    },
    {
        "id": "ON_TIME_DELIVERY",
        "name": "On-time delivery",
        "reason": "Lead-time models are not evaluated per scenario run.",
    },
    {
        "id": "STOCKOUT_FREQUENCY",
        "name": "Stockout frequency and duration",
        "reason": "Reported by simulations, which this module does not read.",
    },
    {
        "id": "INVENTORY_LEVELS",
        "name": "Average inventory, depletion, excess",
        "reason": "Time-phased inventory results come from projections, which this module does not read.",
    },
    {
        "id": "LANE_CONCENTRATION",
        "name": "Lane concentration",
        "reason": "No evidence source reports lane shares for a sourcing plan.",
    },
    {
        "id": "RECOVERY_TIME",
        "name": "Recovery time",
        "reason": "No recovery model exists in ATLAS (the resilience supply model is one period).",
    },
    {
        "id": "CAPACITY_UTILISATION",
        "name": "Capacity utilisation",
        "reason": "Per-offer utilisation exists inside runs but is not aggregated into a comparable metric.",
    },
]

BY_ID = {m["id"]: m for m in REGISTRY}
ORDER = [m["id"] for m in REGISTRY]
COST_IDS = frozenset(m["id"] for m in REGISTRY if m["cost_sensitive"])


def verdict(metric_id: str, baseline: float, value: float) -> str:
    """better | worse | same | changed (neutral metrics). Equality uses a tiny tolerance so float noise is not reported as a change."""
    tol = 1e-9 * max(1.0, abs(baseline), abs(value))
    if abs(value - baseline) <= tol:
        return "same"
    d = BY_ID[metric_id]["direction"]
    if d == "neutral":
        return "changed"
    lower_is_better = d == "min"
    return "better" if (value < baseline) == lower_is_better else "worse"
