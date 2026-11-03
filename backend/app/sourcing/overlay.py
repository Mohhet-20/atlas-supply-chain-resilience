"""Disruption overlay: scenario events and what-if adjustments applied to COPIES of the candidates.

Stored offers, suppliers, risk assessments and the scenario are never changed. Every change is recorded, and every scenario event
that cannot be mapped to a supplier offer is reported as not applied with a reason - never silently ignored.

Mapping (only what the data can justify):
* SUPPLIER_UNAVAILABLE (SUPPLIER target): all of that supplier's candidates become unavailable.
* SUPPLIER_CAPACITY_REDUCTION (SUPPLIER target, magnitude %): capacity x (1 - pct/100).
* FACILITY_SHUTDOWN / REGION_DISRUPTION with listed targets: a destination facility target makes every candidate unavailable (nothing
  can be delivered); a facility owned by a supplier makes that supplier unavailable; listed SUPPLIER targets do the same.
* RELATIONSHIP_UNAVAILABLE / LEAD_TIME_INCREASE with SUPPLY:<id>: acts on that relationship's supplier at its target facility.
* LEAD_TIME_INCREASE on a SUPPLIER target adds days to its lead time.
* Facility capacity events do not state supplier output, so they are reported as not applied. Event timing windows are not modelled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.enums import ScenarioStatus
from app.models.facility import Facility
from app.models.relationship import SupplyRelationship
from app.scenarios import service as scen
from app.sourcing.config import SupplierAdjustment
from app.sourcing.model import Candidate


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


def _note(c: Candidate, text: str) -> tuple[str, ...]:
    return (*c.notes, text)


def from_scenario(
    session: Session, scenario_id: int, cands: list[Candidate], dest_id: int
) -> tuple[list[Candidate], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sc = scen.get_scenario(session, scenario_id)
    if sc.status is not ScenarioStatus.READY:
        raise ConflictError(
            f"Scenario {sc.scenario_uid} is {sc.status.value}; only READY scenarios can be used for sourcing."
        )
    defn = scen.definition_of(session, sc)
    info = {
        "scenario_id": sc.id,
        "scenario_uid": sc.scenario_uid,
        "name": sc.name,
        "version": sc.version,
        "definition_hash": scen.definition_hash(defn),
    }
    owner: dict[int, int] = {}
    fac_ids = {fid for ev in scen.events_of(session, sc) for fid in _targets(ev, "FACILITY")}
    if fac_ids:
        for f in session.scalars(select(Facility).where(Facility.id.in_(fac_ids))):
            if f.owner_supplier_id:
                owner[f.id] = f.owner_supplier_id
    out = list(cands)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def hit(pred: Any, fn: Any, label: str, src: str, extra: dict[str, Any] | None = None) -> int:
        n = 0
        for i, c in enumerate(out):
            if pred(c):
                out[i] = fn(c)
                n += 1
        applied.append({"source": src, "effect": label, "offers_affected": n, **(extra or {})})
        return n

    for ev in scen.events_of(session, sc):
        src = f"scenario:{sc.scenario_uid}:{ev.event_uid}"
        et = ev.event_type.value
        sup = _targets(ev, "SUPPLIER")
        fac = _targets(ev, "FACILITY")
        rel_id = _rel(ev.relationship_ref)
        pct, days = ev.magnitude_pct, ev.added_lead_time_days
        done = False
        if et in ("SUPPLIER_UNAVAILABLE", "FACILITY_SHUTDOWN", "REGION_DISRUPTION") or (
            et == "CUSTOM" and (ev.effect or "").upper() == "UNAVAILABLE"
        ):
            if dest_id in fac:
                hit(
                    lambda c: True,
                    lambda c: replace(
                        c,
                        unavailable_reason="the destination facility is shut down by the scenario",
                        notes=_note(c, "destination shut down"),
                    ),
                    "destination shut down: nothing can be delivered",
                    src,
                )
                done = True
            victims = set(sup) | {owner[f] for f in fac if f in owner}
            if victims:
                hit(
                    lambda c: c.supplier_id in victims,
                    lambda c: replace(
                        c, unavailable_reason="unavailable under the scenario", notes=_note(c, "unavailable")
                    ),
                    "supplier unavailable",
                    src,
                    {"supplier_ids": sorted(victims)},
                )
                done = True
        elif et == "SUPPLIER_CAPACITY_REDUCTION" and sup and pct:
            f = 1 - float(pct) / 100.0
            hit(
                lambda c: c.supplier_id in sup,
                lambda c: replace(
                    c, capacity=None if c.capacity is None else c.capacity * f, notes=_note(c, f"capacity -{pct:g}%")
                ),
                f"capacity reduced by {pct:g}%",
                src,
                {"supplier_ids": sorted(sup)},
            )
            done = True
        elif et == "RELATIONSHIP_UNAVAILABLE" and rel_id is not None:
            r = session.get(SupplyRelationship, rel_id)
            if r is not None and r.supplier_id and r.target_facility_id == dest_id:
                hit(
                    lambda c: c.supplier_id == r.supplier_id,
                    lambda c: replace(
                        c,
                        unavailable_reason="its supply relationship is unavailable under the scenario",
                        notes=_note(c, "relationship unavailable"),
                    ),
                    "relationship unavailable",
                    src,
                    {"supplier_ids": [r.supplier_id]},
                )
                done = True
        elif et == "LEAD_TIME_INCREASE" and days:
            who: set[int] = set(sup)
            if rel_id is not None:
                r = session.get(SupplyRelationship, rel_id)
                if r is not None and r.supplier_id and r.target_facility_id == dest_id:
                    who.add(r.supplier_id)
            if who:
                hit(
                    lambda c: c.supplier_id in who,
                    lambda c: replace(
                        c,
                        lead_days=None if c.lead_days is None else c.lead_days + float(days),
                        notes=_note(c, f"lead time +{days:g} d"),
                    ),
                    f"lead time +{days:g} days",
                    src,
                    {"supplier_ids": sorted(who)},
                )
                done = True
        if not done:
            reason = (
                "Facility capacity events do not state supplier output." if et == "FACILITY_CAPACITY_REDUCTION"
                else "The event has no supplier, destination or relationship target (or no magnitude) that maps to an offer for this requirement."
            )  # fmt: skip
            skipped.append({"source": src, "event_type": et, "reason": reason})
    return out, applied, skipped, info


def from_adjustments(
    cands: list[Candidate], adjustments: list[SupplierAdjustment]
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    by = {a.supplier_id: a for a in adjustments}
    out: list[Candidate] = []
    applied: list[dict[str, Any]] = []
    seen: set[int] = set()
    for c in cands:
        a = by.get(c.supplier_id)
        if a is None:
            out.append(c)
            continue
        seen.add(c.supplier_id)
        upd: dict[str, Any] = {"notes": _note(c, "what-if adjustment")}
        if a.unavailable:
            upd["unavailable_reason"] = "marked unavailable by a what-if adjustment"
        if c.price is not None:
            upd["price"] = c.price * a.price_multiplier
        if c.capacity is not None:
            upd["capacity"] = c.capacity * a.capacity_multiplier
        if c.lead_days is not None:
            upd["lead_days"] = c.lead_days + a.add_lead_days
        if a.risk_override is not None:
            upd["risk"] = a.risk_override
        out.append(replace(c, **upd))
    for sid, a in by.items():
        applied.append(
            {
                "source": "request",
                "effect": "what-if adjustment",
                "supplier_id": sid,
                **a.model_dump(),
                "matched_offers": sid in seen,
            }
        )
    return out, applied


def overlay_hash(applied: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps([applied, skipped], sort_keys=True, default=str).encode()).hexdigest()
