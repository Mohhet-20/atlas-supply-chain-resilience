"""Reusable scenario templates. They pre-fill *assumptions and event shapes only*.

No template names an entity: the analyst must select real targets and review every assumption before a run. Nothing here
is seed data and no scenario is created from a template without an explicit request.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import EventType, ScenarioType

DISCLAIMER = (
    "Template defaults are placeholders for review, not estimates. Choose real entities from the registry and set "
    "magnitudes and durations you can justify."
)

TEMPLATES: dict[str, dict[str, Any]] = {
    "supplier_outage": {
        "name": "Complete supplier outage",
        "scenario_type": ScenarioType.SUPPLIER,
        "description": "A selected supplier stops shipping entirely for an analyst-defined period.",
        "events": [{"event_type": EventType.SUPPLIER_UNAVAILABLE, "needs": "one supplier", "duration_days": None}],
        "notes": "Duration is intentionally blank: unknown until you set it.",
    },
    "supplier_capacity_reduction": {
        "name": "Partial supplier capacity reduction",
        "scenario_type": ScenarioType.SUPPLIER,
        "description": "A selected supplier loses part of its output.",
        "events": [
            {"event_type": EventType.SUPPLIER_CAPACITY_REDUCTION, "needs": "one supplier", "magnitude_pct": None}
        ],
        "notes": "The reduction percentage must be entered; no default is assumed.",
    },
    "facility_shutdown": {
        "name": "Facility shutdown",
        "scenario_type": ScenarioType.FACILITY,
        "description": "A selected facility stops operating.",
        "events": [{"event_type": EventType.FACILITY_SHUTDOWN, "needs": "one facility"}],
        "notes": "",
    },
    "lead_time_increase": {
        "name": "Lead-time increase",
        "scenario_type": ScenarioType.RELATIONSHIP,
        "description": "Deliveries on a selected relationship (or all outgoing relationships of a selected entity) take longer.",
        "events": [
            {
                "event_type": EventType.LEAD_TIME_INCREASE,
                "needs": "one relationship or one entity",
                "added_lead_time_days": None,
            }
        ],
        "notes": "Added days must be entered. Inventory buffers are not modelled.",
    },
    "critical_supplier_loss": {
        "name": "Loss of a critical supplier",
        "scenario_type": ScenarioType.SUPPLIER,
        "description": "Pick a supplier you consider critical (for example from the Supplier Risk page) and remove it.",
        "events": [{"event_type": EventType.SUPPLIER_UNAVAILABLE, "needs": "one supplier you selected as critical"}],
        "notes": "ATLAS does not choose the supplier for you.",
    },
    "multi_entity": {
        "name": "Simultaneous disruption of selected entities",
        "scenario_type": ScenarioType.MULTI_ENTITY,
        "description": "Several selected suppliers and/or facilities are disrupted at the same time.",
        "events": [{"event_type": EventType.CUSTOM, "needs": "up to 50 suppliers/facilities", "effect": "UNAVAILABLE"}],
        "notes": "Overlapping events on one target use the larger reduction; they do not stack.",
    },
    "regional_event": {
        "name": "Regional event over selected facilities",
        "scenario_type": ScenarioType.REGIONAL,
        "description": "An event affects an explicitly selected set of facilities that you associate with a region label.",
        "events": [
            {
                "event_type": EventType.REGION_DISRUPTION,
                "needs": "the facilities/suppliers you select",
                "effect": "UNAVAILABLE",
            }
        ],
        "notes": "The region label is descriptive only; ATLAS does not select entities by geography.",
    },
}


def list_templates() -> list[dict[str, Any]]:
    out = []
    for key, t in TEMPLATES.items():
        out.append({
            "key": key, "name": t["name"], "scenario_type": t["scenario_type"].value, "description": t["description"],
            "events": [{**e, "event_type": e["event_type"].value} for e in t["events"]], "notes": t["notes"],
            "requires_target_selection": True, "disclaimer": DISCLAIMER,
        })  # fmt: skip
    return out
