"""Disruption overlay for one product-facility pair: a scenario's events become hypothetical inputs applied to COPIES.

Stored scenarios, inventory, policies and offers are never changed. Only what the inventory model can express is mapped; every other event
is reported as not applied, with the reason - never silently ignored.

Mapping (events that overlap are treated as concurrent and combined into ONE what-if for the scenario):
* SUPPLIER_UNAVAILABLE / FACILITY_SHUTDOWN / REGION_DISRUPTION / CUSTOM(UNAVAILABLE) on a supplier that serves this pair, on a facility owned by
  that supplier, or on the destination facility itself -> no order can be placed (and open receipts slip) for ``duration + recovery`` days from the
  event's start offset. A destination shutdown is approximated the same way (conservative: ordering is blocked, not just receiving).
* RELATIONSHIP_UNAVAILABLE on a relationship (``SUPPLY:<id>``) of a serving supplier into this facility -> the same blocked window.
* LEAD_TIME_INCREASE on a serving supplier or its relationship -> extra lead-time days (summed).
* Not mapped: capacity reductions (no per-order capacity is stored for the pair), facility capacity events, events with no duration, events
  that do not touch a supplier serving this pair, and the timing detail of overlapping events.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.enums import ScenarioStatus
from app.models.facility import Facility
from app.models.relationship import SupplyRelationship
from app.scenarios import service as scen

MAX_WINDOW = 365


def _targets(ev: Any, kind: str) -> set[int]:
    ids: set[int] = set()
    if ev.target_type is not None and ev.target_type.value == kind and ev.target_id:
        ids.add(int(ev.target_id))
    for t in ev.targets or []:
        if isinstance(t, dict) and str(t.get("type", "")).upper() == kind and isinstance(t.get("id"), int):
            ids.add(t["id"])
    return ids


def _rel(ref: str | None) -> int | None:
    return int(ref[7:]) if ref and ref.startswith("SUPPLY:") and ref[7:].isdigit() else None


def from_scenario(session: Session, scenario_id: int, facility_id: int, suppliers: set[int]) -> dict[str, Any]:
    sc = scen.get_scenario(session, scenario_id)
    if sc.status is not ScenarioStatus.READY:
        raise ConflictError(f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used.")
    defn = scen.definition_of(session, sc)
    info = {
        "scenario_id": sc.id,
        "scenario_uid": sc.scenario_uid,
        "name": sc.name,
        "version": sc.version,
        "definition_hash": scen.definition_hash(defn),
    }
    events = scen.events_of(session, sc)
    fac_ids = {fid for ev in events for fid in _targets(ev, "FACILITY")}
    owner: dict[int, int] = {}
    if fac_ids:
        for f in session.scalars(select(Facility).where(Facility.id.in_(fac_ids))):
            if f.owner_supplier_id:
                owner[f.id] = f.owner_supplier_id
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    windows: list[tuple[int, int]] = []
    extra = 0.0
    for ev in events:
        src = f"scenario:{sc.scenario_uid}:{ev.event_uid}"
        et = ev.event_type.value
        sup, fac, rel_id = _targets(ev, "SUPPLIER"), _targets(ev, "FACILITY"), _rel(ev.relationship_ref)
        start = int(ev.start_offset_days or 0)
        span = (ev.duration_days or 0) + (ev.recovery_days or 0)
        reason = None
        if et in ("SUPPLIER_UNAVAILABLE", "FACILITY_SHUTDOWN", "REGION_DISRUPTION") or (
            et == "CUSTOM" and (ev.effect or "").upper() == "UNAVAILABLE"
        ):
            hit_dest = facility_id in fac
            victims = (set(sup) | {owner[f] for f in fac if f in owner}) & suppliers
            if not (hit_dest or victims):
                reason = "The event does not touch the destination facility or a supplier known to serve this product at this facility."
            elif span <= 0:
                reason = "The event has no duration, so its blocked window cannot be sized."
            else:
                windows.append((start, start + int(span)))
                applied.append({"source": src, "effect": f"orders blocked and open receipts slipped for {int(span)} days from day {start}", "supplier_ids": sorted(victims), "destination": hit_dest})  # fmt: skip
        elif et == "RELATIONSHIP_UNAVAILABLE" and rel_id is not None:
            r = session.get(SupplyRelationship, rel_id)
            if r is None or r.target_facility_id != facility_id or (r.supplier_id and r.supplier_id not in suppliers):
                reason = "The relationship does not supply this facility from a supplier known to serve this product."
            elif span <= 0:
                reason = "The event has no duration, so its blocked window cannot be sized."
            else:
                windows.append((start, start + int(span)))
                applied.append({"source": src, "effect": f"relationship unavailable: orders blocked for {int(span)} days from day {start}", "supplier_ids": [r.supplier_id]})  # fmt: skip
        elif et == "LEAD_TIME_INCREASE" and ev.added_lead_time_days:
            who = set(sup)
            if rel_id is not None:
                r = session.get(SupplyRelationship, rel_id)
                if r is not None and r.supplier_id and r.target_facility_id == facility_id:
                    who.add(r.supplier_id)
            if who & suppliers:
                extra += float(ev.added_lead_time_days)
                applied.append(
                    {
                        "source": src,
                        "effect": f"lead time +{ev.added_lead_time_days:g} days",
                        "supplier_ids": sorted(who & suppliers),
                    }
                )
            else:
                reason = "The event does not touch a supplier known to serve this product at this facility."
        elif et == "SUPPLIER_CAPACITY_REDUCTION":
            reason = "A capacity reduction needs a per-order or per-period capacity for this pair, which is not stored; it is not applied."
        elif et == "FACILITY_CAPACITY_REDUCTION":
            reason = "Facility capacity events do not state supplier output."
        if reason:
            skipped.append({"source": src, "event_type": et, "reason": reason})
    what_ifs: list[dict[str, Any]] = []
    if windows or extra:
        lo = min((a for a, _ in windows), default=0)
        hi = max((b for _, b in windows), default=0)
        wi = {
            "supplier_blocked_days": min(MAX_WINDOW, max(0, hi - lo)),
            "supplier_blocked_start_day": min(MAX_WINDOW, lo),
            "extra_lead_time_days": min(365.0, extra),
        }
        what_ifs.append({"name": f"Scenario: {sc.name}", "what_if": wi, "events": [a["source"] for a in applied]})
    if len(windows) > 1:
        skipped.append({"source": f"scenario:{sc.scenario_uid}", "event_type": "TIMING", "reason": "Several blocked windows were merged into one continuous window from the earliest start to the latest end (conservative)."})  # fmt: skip
    return {"scenario": info, "what_ifs": what_ifs, "applied": applied, "not_applied": skipped}
