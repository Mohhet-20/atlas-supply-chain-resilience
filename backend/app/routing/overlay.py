"""Disruption overlay: scenario events and request-level changes applied to an in-memory COPY of the network.

The registered network and its lane rows are never modified. ``apply`` returns a new ``Network`` plus a record of every change
and of every event that could not be applied (and why) - an event is never silently ignored.

Event mapping (only what the data can justify):
* FACILITY_SHUTDOWN, REGION_DISRUPTION (listed FACILITY targets), CUSTOM/UNAVAILABLE: the facility is closed, so every lane
  touching it is unavailable. A region label alone never selects facilities.
* FACILITY_CAPACITY_REDUCTION, CUSTOM/REDUCTION: lanes touching the facility have their capacity reduced by the stated share
  (or are only flagged when the lane has no capacity figure to reduce).
* RELATIONSHIP_UNAVAILABLE / LEAD_TIME_INCREASE with a ``SUPPLY:<id>`` reference: lanes linked to that relationship are closed /
  have the added days appended to their duration.
* LEAD_TIME_INCREASE / CUSTOM/LEAD_TIME on a facility: lanes touching the facility get the added days.
* Supplier-level events do not identify transport lanes, so they are reported as not applied.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.enums import ScenarioStatus
from app.routing.config import LaneAdjustment
from app.routing.network import Network
from app.scenarios import service as scen


@dataclass(frozen=True)
class Disruption:
    kind: str  # CLOSE_NODE | CLOSE_LANES | REDUCE_CAPACITY | ADD_DURATION
    source: str  # "scenario:<uid>:<event_uid>" or "request"
    facility_ids: tuple[int, ...] = ()
    relationship_id: int | None = None
    lane_uids: tuple[str, ...] = ()
    pct: float | None = None
    add_hours: float | None = None
    label: str = ""
    multipliers: tuple[float, float, float] | None = None  # ADJUST: duration, cost, capacity


def from_scenario(session: Session, scenario_id: int) -> tuple[list[Disruption], list[dict[str, Any]], dict[str, Any]]:
    """Translate the events of a READY scenario into disruptions. Returns (disruptions, not_applied, scenario_info)."""
    sc = scen.get_scenario(session, scenario_id)
    if sc.status is not ScenarioStatus.READY:
        raise ConflictError(
            f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used for routing."
        )
    defn = scen.definition_of(session, sc)
    info = {
        "scenario_id": sc.id,
        "scenario_uid": sc.scenario_uid,
        "name": sc.name,
        "version": sc.version,
        "definition_hash": scen.definition_hash(defn),
    }
    out: list[Disruption] = []
    skipped: list[dict[str, Any]] = []
    for ev in scen.events_of(session, sc):
        src = f"scenario:{sc.scenario_uid}:{ev.event_uid}"
        et = ev.event_type.value
        fac = _facilities(ev)
        rel = _relationship(ev.relationship_ref)
        eff = (ev.effect or "").upper() if et == "CUSTOM" else None
        pct = ev.magnitude_pct
        days = ev.added_lead_time_days
        d: Disruption | None = None
        if et == "FACILITY_SHUTDOWN" or et == "REGION_DISRUPTION" or (et == "CUSTOM" and eff == "UNAVAILABLE"):
            if fac:
                d = Disruption("CLOSE_NODE", src, facility_ids=fac, label=et)
        elif et == "FACILITY_CAPACITY_REDUCTION" or (et == "CUSTOM" and eff == "REDUCTION"):
            if fac and pct:
                d = Disruption("REDUCE_CAPACITY", src, facility_ids=fac, pct=float(pct), label=et)
        elif et == "RELATIONSHIP_UNAVAILABLE":
            if rel is not None:
                d = Disruption("CLOSE_LANES", src, relationship_id=rel, label=et)
        elif et == "LEAD_TIME_INCREASE" or (et == "CUSTOM" and eff == "LEAD_TIME"):
            if days and (fac or rel is not None):
                d = Disruption(
                    "ADD_DURATION", src, facility_ids=fac, relationship_id=rel, add_hours=float(days) * 24.0, label=et
                )
        if d is None:
            reason = (
                "Supplier-level events do not identify transport lanes."
                if et.startswith("SUPPLIER_")
                else "The event has no facility or relationship target (or no magnitude) that maps to transport lanes."
            )
            skipped.append({"source": src, "event_type": et, "reason": reason})
        else:
            out.append(d)
    return out, skipped, info


def _facilities(ev: Any) -> tuple[int, ...]:
    ids: list[int] = []
    if ev.target_type is not None and ev.target_type.value == "FACILITY" and ev.target_id:
        ids.append(int(ev.target_id))
    for t in ev.targets or []:
        if isinstance(t, dict) and str(t.get("type", "")).upper() == "FACILITY" and isinstance(t.get("id"), int):
            ids.append(t["id"])
    return tuple(sorted(set(ids)))


def _relationship(ref: str | None) -> int | None:
    if ref and ref.startswith("SUPPLY:") and ref[7:].isdigit():
        return int(ref[7:])
    return None


def from_request(closures: list[str], adjustments: list[LaneAdjustment]) -> list[Disruption]:
    out = (
        [Disruption("CLOSE_LANES", "request", lane_uids=tuple(sorted(set(closures))), label="LANE_CLOSURE")]
        if closures
        else []
    )
    for a in adjustments:
        out.append(
            Disruption(
                "ADJUST",
                "request",
                lane_uids=(a.lane_uid,),
                label="LANE_ADJUSTMENT",
                multipliers=(a.duration_multiplier, a.cost_multiplier, a.capacity_multiplier),
            )
        )
    return out


def apply(base: Network, disruptions: list[Disruption]) -> tuple[Network, list[dict[str, Any]]]:
    """Return (new network, applied changes). ``base`` is not modified."""
    lanes = dict(base.lanes)
    closed = dict(base.closed)
    closed_nodes = dict(base.closed_nodes)
    applied: list[dict[str, Any]] = []

    def touching(fids: tuple[int, ...]) -> list[str]:
        s = set(fids)
        return [u for u, ln in lanes.items() if ln.origin in s or ln.dest in s]

    for d in disruptions:
        if d.kind == "CLOSE_NODE":
            hit = [f for f in d.facility_ids if f in base.nodes]
            for f in hit:
                closed_nodes[f] = d.source
            applied.append({"source": d.source, "kind": d.kind, "label": d.label, "facilities": list(d.facility_ids), "unknown_facilities": sorted(set(d.facility_ids) - set(hit)), "lanes_closed": sorted(touching(tuple(hit)))})  # fmt: skip
        elif d.kind == "CLOSE_LANES":
            if d.relationship_id is not None:
                uids = sorted(u for u, ln in lanes.items() if ln.relationship_id == d.relationship_id)
            else:
                uids = sorted(u for u in d.lane_uids if u in lanes)
            for u in uids:
                closed[u] = d.source
                del lanes[u]
            applied.append({"source": d.source, "kind": d.kind, "label": d.label, "lanes_closed": uids})
        elif d.kind == "REDUCE_CAPACITY":
            uids = sorted(touching(d.facility_ids))
            unquantified = []
            for u in uids:
                ln = lanes[u]
                note = f"capacity reduced {d.pct:g}% by {d.source}"
                if ln.capacity is None:
                    unquantified.append(u)
                    lanes[u] = replace(
                        ln, degraded=(*ln.degraded, note + " (lane has no capacity figure: flagged only)")
                    )
                else:
                    lanes[u] = replace(
                        ln, capacity=ln.capacity * (1 - (d.pct or 0) / 100.0), degraded=(*ln.degraded, note)
                    )
            applied.append(
                {
                    "source": d.source,
                    "kind": d.kind,
                    "label": d.label,
                    "pct": d.pct,
                    "lanes_changed": uids,
                    "flag_only": unquantified,
                }
            )
        elif d.kind == "ADD_DURATION":
            if d.relationship_id is not None:
                uids = sorted(u for u, ln in lanes.items() if ln.relationship_id == d.relationship_id)
            else:
                uids = sorted(touching(d.facility_ids))
            unknown = []
            for u in uids:
                ln = lanes[u]
                note = f"+{(d.add_hours or 0) / 24:g} days by {d.source}"
                if ln.duration_hours is None:
                    unknown.append(u)
                    lanes[u] = replace(ln, degraded=(*ln.degraded, note + " (lane has no duration: flagged only)"))
                else:
                    lanes[u] = replace(ln, duration_hours=ln.duration_hours + (d.add_hours or 0), duration_quality="ESTIMATED" if ln.duration_quality == "VERIFIED" else ln.duration_quality, degraded=(*ln.degraded, note))  # fmt: skip
            applied.append(
                {
                    "source": d.source,
                    "kind": d.kind,
                    "label": d.label,
                    "add_hours": d.add_hours,
                    "lanes_changed": uids,
                    "flag_only": unknown,
                }
            )
        elif d.kind == "ADJUST":
            u = d.lane_uids[0]
            dm, cm, capm = d.multipliers or (1.0, 1.0, 1.0)
            if u not in lanes:
                applied.append(
                    {
                        "source": d.source,
                        "kind": "ADJUST",
                        "lane_uid": u,
                        "applied": False,
                        "reason": "lane is not in the traversable network",
                    }
                )
                continue
            ln = lanes[u]
            lanes[u] = replace(
                ln,
                duration_hours=None if ln.duration_hours is None else ln.duration_hours * dm,
                cost=None if ln.cost is None else ln.cost * cm,
                capacity=None if ln.capacity is None else ln.capacity * capm,
                degraded=(*ln.degraded, f"request adjustment x{dm:g} duration, x{cm:g} cost, x{capm:g} capacity"),
                duration_quality="ESTIMATED" if dm != 1 and ln.duration_quality == "VERIFIED" else ln.duration_quality,
                cost_quality="ESTIMATED" if cm != 1 and ln.cost_quality == "VERIFIED" else ln.cost_quality,
            )
            applied.append({"source": d.source, "kind": "ADJUST", "lane_uid": u, "applied": True, "duration_multiplier": dm, "cost_multiplier": cm, "capacity_multiplier": capm})  # fmt: skip
    net = Network(lanes=lanes, nodes=dict(base.nodes), excluded=list(base.excluded), issues=list(base.issues), as_of=base.as_of, closed=closed, closed_nodes=closed_nodes)  # fmt: skip
    return net, applied


def overlay_hash(applied: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps([applied, skipped], sort_keys=True, default=str).encode()).hexdigest()
