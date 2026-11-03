"""Dedicated scenario validation: ten explicit checks, nothing is silently defaulted or repaired.

Validation reads the registry and relationship tables only. It never writes. ``ERROR`` messages block a run; ``WARNING``
messages are shown with the results; ``INFO`` messages explain scope decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError
from app.graph.store import get_rec, parse_ref
from app.models.enums import EdgeStatus, EntityStatus, EventType, RegistryType
from app.models.facility import Facility
from app.models.supplier import Supplier
from app.scenarios.config import Assumptions

S, F = RegistryType.SUPPLIER, RegistryType.FACILITY
MAX_DURATION_DAYS = 3_650
MAX_ADDED_LEAD_DAYS = 3_650
MAX_START_OFFSET_DAYS = 3_650

REDUCTION_TYPES = {EventType.SUPPLIER_CAPACITY_REDUCTION, EventType.FACILITY_CAPACITY_REDUCTION}
UNAVAILABLE_TYPES = {EventType.SUPPLIER_UNAVAILABLE, EventType.FACILITY_SHUTDOWN, EventType.RELATIONSHIP_UNAVAILABLE}
SINGLE_TARGET = {
    EventType.SUPPLIER_UNAVAILABLE: S,
    EventType.SUPPLIER_CAPACITY_REDUCTION: S,
    EventType.FACILITY_SHUTDOWN: F,
    EventType.FACILITY_CAPACITY_REDUCTION: F,
}
LIST_TARGET = {EventType.REGION_DISRUPTION, EventType.CUSTOM}
CHECKS = [
    "Every target entity exists",
    "Target entity types are supported",
    "Event types are compatible with target types",
    "Capacity reduction values are in range",
    "Duration and effective-time fields are consistent",
    "Referenced relationships exist and belong to the intended endpoints",
    "Required assumptions are supplied",
    "Scenario size and event count are within limits",
    "The caller may access all referenced entities",
    "The scenario has enough information to analyse",
]


@dataclass(frozen=True)
class Limits:
    max_events: int = 25
    max_targets_per_event: int = 50
    max_depth: int = 4


def _m(code: str, severity: str, text: str, *, field: str | None = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "text": text, "field": field}


def event_targets(ev: dict[str, Any]) -> list[tuple[RegistryType, int]]:
    """Entity targets of an event as (type, id) pairs, in a stable order; relationship refs are separate."""
    et = EventType(ev["event_type"])
    out: list[tuple[RegistryType, int]] = []
    if ev.get("target_type") and ev.get("target_id") is not None:
        out.append((RegistryType(ev["target_type"]), int(ev["target_id"])))
    if et in LIST_TARGET:
        for t in ev.get("targets") or []:
            out.append((RegistryType(t["type"]), int(t["id"])))
    seen: set[tuple[RegistryType, int]] = set()
    uniq = []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def validate_definition(
    session: Session, definition: dict[str, Any], limits: Limits, *, caller_can_read: bool = True
) -> dict[str, Any]:
    scenario, events = definition["scenario"], definition["events"]
    scen_msgs: list[dict[str, Any]] = []
    per_event: dict[str, list[dict[str, Any]]] = {e["event_uid"]: [] for e in events}
    failed: set[int] = set()  # indexes into CHECKS that produced an ERROR

    def err(i: int, uid: str | None, code: str, text: str, field: str | None = None) -> None:
        failed.add(i)
        (per_event[uid] if uid else scen_msgs).append(_m(code, "ERROR", text, field=field))

    def warn(uid: str | None, code: str, text: str, field: str | None = None) -> None:
        (per_event[uid] if uid else scen_msgs).append(_m(code, "WARNING", text, field=field))

    # (7) assumptions -----------------------------------------------------------------------------------------------
    try:
        a = Assumptions.model_validate(scenario.get("assumptions") or {})
        if a.downstream_depth > limits.max_depth or a.upstream_depth > limits.max_depth:
            err(
                6,
                None,
                "ASSUMPTION_DEPTH_EXCEEDS_LIMIT",
                f"Traversal depth may not exceed the server limit of {limits.max_depth}.",
                "assumptions",
            )
        if not a.assume_no_buffer:
            err(
                6,
                None,
                "ASSUMPTION_BUFFER_UNSUPPORTED",
                "Inventory buffers and substitution are not modelled; assume_no_buffer must be true.",
                "assumptions",
            )
    except ValidationError as exc:
        a = None
        first = exc.errors()[0]
        err(
            6,
            None,
            "ASSUMPTIONS_INVALID",
            f"Assumptions are invalid: {'.'.join(map(str, first['loc']))}: {first['msg']}",
            "assumptions",
        )

    # (8) size ------------------------------------------------------------------------------------------------------
    if len(events) > limits.max_events:
        err(
            7,
            None,
            "TOO_MANY_EVENTS",
            f"A scenario may contain at most {limits.max_events} events (has {len(events)}).",
        )
    # (10) minimal content ------------------------------------------------------------------------------------------
    if not events:
        err(9, None, "NO_EVENTS", "Add at least one disruption event before validating or running the scenario.")

    # gather entities for batched existence checks
    want: dict[RegistryType, set[int]] = {S: set(), F: set()}
    for ev in events:
        for t, i in event_targets(ev):
            if t in want:
                want[t].add(i)
    found_s = {r.id: r for r in session.scalars(select(Supplier).where(Supplier.id.in_(want[S])))} if want[S] else {}
    found_f = {r.id: r for r in session.scalars(select(Facility).where(Facility.id.in_(want[F])))} if want[F] else {}
    found = {S: found_s, F: found_f}

    resolvable_events = 0
    reachable_targets = 0
    for ev in events:
        uid = ev["event_uid"]
        try:
            et = EventType(ev["event_type"])
        except ValueError:
            err(2, uid, "EVENT_TYPE_UNSUPPORTED", f"Event type {ev['event_type']!r} is not supported.", "event_type")
            continue
        ref = ev.get("relationship_ref")
        tgts: list[tuple[RegistryType, int]] = []
        try:
            tgts = event_targets(ev)
        except (KeyError, ValueError, TypeError):
            err(1, uid, "TARGET_MALFORMED", "targets must be a list of {type, id} entries.", "targets")
        # (8) per-event size
        if len(tgts) > limits.max_targets_per_event:
            err(
                7,
                uid,
                "TOO_MANY_TARGETS",
                f"An event may target at most {limits.max_targets_per_event} entities (has {len(tgts)}).",
                "targets",
            )
        # (2)(3) target kinds
        for t, _i in tgts:
            if t is RegistryType.PRODUCT:
                err(
                    1,
                    uid,
                    "TARGET_TYPE_UNSUPPORTED",
                    "Products cannot be disrupted directly: they have no capacity or outflow of their own. Disrupt the supplier or facility that provides them.",
                    "target_type",
                )
        if et in SINGLE_TARGET:
            want_t = SINGLE_TARGET[et]
            if not tgts:
                err(2, uid, "TARGET_REQUIRED", f"{et.value} needs a {want_t.value.lower()} target.", "target_id")
            elif any(t is not want_t and t is not RegistryType.PRODUCT for t, _ in tgts):
                err(
                    2,
                    uid,
                    "TARGET_INCOMPATIBLE",
                    f"{et.value} applies to a {want_t.value.lower()}, not {tgts[0][0].value.lower()}.",
                    "target_type",
                )
            if len(tgts) > 1:
                err(
                    2,
                    uid,
                    "TARGET_SINGLE_ONLY",
                    f"{et.value} takes exactly one target; use a multi-entity event for several.",
                    "target_id",
                )
            if ref:
                err(
                    5,
                    uid,
                    "RELATIONSHIP_NOT_ALLOWED",
                    f"{et.value} does not take a relationship reference.",
                    "relationship_ref",
                )
        elif et is EventType.RELATIONSHIP_UNAVAILABLE:
            if not ref:
                err(
                    5,
                    uid,
                    "RELATIONSHIP_REQUIRED",
                    "RELATIONSHIP_UNAVAILABLE needs a relationship reference such as SUPPLY:12.",
                    "relationship_ref",
                )
        elif et is EventType.LEAD_TIME_INCREASE:
            if not ref and not tgts:
                err(
                    5,
                    uid,
                    "TARGET_REQUIRED",
                    "LEAD_TIME_INCREASE needs a relationship reference or a supplier/facility target.",
                    "relationship_ref",
                )
            if ref and len(tgts) > 1:
                err(
                    2,
                    uid,
                    "TARGET_SINGLE_ONLY",
                    "With a relationship reference, give at most one endpoint target.",
                    "target_id",
                )
        elif et in LIST_TARGET:
            if not tgts:
                err(
                    2,
                    uid,
                    "TARGET_REQUIRED",
                    f"{et.value} needs at least one explicitly selected supplier or facility.",
                    "targets",
                )
            if ref:
                err(
                    5,
                    uid,
                    "RELATIONSHIP_NOT_ALLOWED",
                    f"{et.value} does not take a relationship reference.",
                    "relationship_ref",
                )
            if et is EventType.REGION_DISRUPTION and not (ev.get("region_label") or "").strip():
                err(
                    2,
                    uid,
                    "REGION_LABEL_REQUIRED",
                    "A region label is required (descriptive only; the targets you select define what is disrupted).",
                    "region_label",
                )
            if ev.get("effect") not in ("UNAVAILABLE", "REDUCTION"):
                err(3, uid, "EFFECT_REQUIRED", "Choose an effect: UNAVAILABLE or REDUCTION.", "effect")
        if et not in LIST_TARGET and ev.get("targets"):
            err(2, uid, "TARGET_LIST_NOT_ALLOWED", f"{et.value} does not take a target list.", "targets")

        # (1) existence + lifecycle
        for t, i in tgts:
            if t not in found:
                continue
            row = found[t].get(i)
            if row is None:
                err(0, uid, "ENTITY_NOT_FOUND", f"{t.value.title()} {i} does not exist.", "target_id")
                continue
            if row.merged_into_id is not None:
                err(
                    0,
                    uid,
                    "ENTITY_MERGED",
                    f"{t.value.title()} {i} was merged into {row.merged_into_id}; target the surviving record.",
                    "target_id",
                )
                continue
            if row.status is EntityStatus.INACTIVE:
                warn(
                    uid,
                    "ENTITY_INACTIVE",
                    f"{t.value.title()} {row.name} is INACTIVE; disrupting it has little operational meaning.",
                    "target_id",
                )
            reachable_targets += 1

        # (4) magnitude
        mag = ev.get("magnitude_pct")
        is_reduction = et in REDUCTION_TYPES or (et in LIST_TARGET and ev.get("effect") == "REDUCTION")
        if is_reduction:
            if mag is None:
                err(
                    3,
                    uid,
                    "MAGNITUDE_REQUIRED",
                    "A capacity-reduction percentage greater than 0 and at most 100 is required; none is assumed.",
                    "magnitude_pct",
                )
            elif not (0 < float(mag) <= 100):
                err(3, uid, "MAGNITUDE_RANGE", "magnitude_pct must be greater than 0 and at most 100.", "magnitude_pct")
            elif float(mag) == 100:
                warn(
                    uid,
                    "MAGNITUDE_IS_FULL_LOSS",
                    "A 100% reduction is a complete loss; consider an unavailable/shutdown event instead.",
                    "magnitude_pct",
                )
        elif mag is not None:
            warn(uid, "MAGNITUDE_IGNORED", f"magnitude_pct is ignored for {et.value}.", "magnitude_pct")
        lt = ev.get("added_lead_time_days")
        if et is EventType.LEAD_TIME_INCREASE:
            if lt is None or not (0 < float(lt) <= MAX_ADDED_LEAD_DAYS):
                err(
                    3,
                    uid,
                    "LEAD_TIME_REQUIRED",
                    f"added_lead_time_days must be greater than 0 and at most {MAX_ADDED_LEAD_DAYS}.",
                    "added_lead_time_days",
                )
        elif lt is not None:
            warn(uid, "LEAD_TIME_IGNORED", f"added_lead_time_days is ignored for {et.value}.", "added_lead_time_days")

        # (5) time fields
        start, dur, rec = ev.get("start_offset_days"), ev.get("duration_days"), ev.get("recovery_days")
        if start is not None and not (0 <= start <= MAX_START_OFFSET_DAYS):
            err(
                4,
                uid,
                "START_RANGE",
                f"start_offset_days must be between 0 and {MAX_START_OFFSET_DAYS}.",
                "start_offset_days",
            )
        if dur is not None and not (1 <= dur <= MAX_DURATION_DAYS):
            err(4, uid, "DURATION_RANGE", f"duration_days must be between 1 and {MAX_DURATION_DAYS}.", "duration_days")
        if rec is not None and not (0 <= rec <= MAX_DURATION_DAYS):
            err(4, uid, "RECOVERY_RANGE", f"recovery_days must be between 0 and {MAX_DURATION_DAYS}.", "recovery_days")
        if rec is not None and dur is None:
            err(
                4,
                uid,
                "RECOVERY_WITHOUT_DURATION",
                "recovery_days needs duration_days: recovery follows the disruption.",
                "recovery_days",
            )
        if dur is None:
            warn(
                uid,
                "DURATION_UNKNOWN",
                "No duration is set, so the disruption window is reported as unknown.",
                "duration_days",
            )

        # (6) relationship reference
        if ref and et in (EventType.RELATIONSHIP_UNAVAILABLE, EventType.LEAD_TIME_INCREASE):
            try:
                kind, row_id = parse_ref(ref)
                rec_edge = get_rec(session, kind, row_id)
            except DomainValidationError as exc:
                err(
                    5,
                    uid,
                    "RELATIONSHIP_REF_INVALID",
                    exc.message if hasattr(exc, "message") else str(exc),
                    "relationship_ref",
                )
                rec_edge = None
            else:
                if rec_edge is None:
                    err(5, uid, "RELATIONSHIP_NOT_FOUND", f"Relationship {ref} does not exist.", "relationship_ref")
                elif rec_edge.status is not EdgeStatus.CONFIRMED:
                    err(
                        5,
                        uid,
                        "RELATIONSHIP_NOT_CONFIRMED",
                        f"Relationship {ref} is {rec_edge.status.value}; only CONFIRMED relationships are part of the analysed network, so this event would have no effect.",
                        "relationship_ref",
                    )
                else:
                    reachable_targets += 1
                    ends = {rec_edge.source, rec_edge.destination}
                    if tgts and any((t, i) not in ends for t, i in tgts):
                        err(
                            5,
                            uid,
                            "RELATIONSHIP_ENDPOINT_MISMATCH",
                            f"Relationship {ref} does not connect the selected target.",
                            "relationship_ref",
                        )

        # region label sanity (descriptive only)
        if et is EventType.REGION_DISRUPTION and (ev.get("region_label") or "").strip():
            lab = ev["region_label"].strip().casefold()
            diff = 0
            for t, i in tgts:
                row = found.get(t, {}).get(i)
                if row is not None and (row.country_or_region or "").strip().casefold() != lab:
                    diff += 1
            if diff:
                warn(
                    uid,
                    "REGION_LABEL_MISMATCH",
                    f"{diff} selected target(s) are registered in a different country/region than the label {ev['region_label']!r}. The label is descriptive; the selected targets are what is disrupted.",
                    "region_label",
                )

        if not any(m["severity"] == "ERROR" for m in per_event[uid]):
            resolvable_events += 1

    # (9) authorization -----------------------------------------------------------------------------------------------
    if not caller_can_read:
        err(8, None, "NOT_AUTHORIZED", "The caller may not read the referenced entities.")
    else:
        scen_msgs.append(
            _m(
                "AUTHORIZATION_MODEL",
                "INFO",
                "Access is role-based. ATLAS has no per-entity permissions or tenancy, so any authenticated reader may reference any registry entity.",
            )
        )

    # (10) enough to analyse ------------------------------------------------------------------------------------------
    if events and resolvable_events == 0:
        err(9, None, "NO_VALID_EVENTS", "No event is valid yet, so there is nothing to analyse.")
    if events and resolvable_events and reachable_targets == 0:
        err(9, None, "NO_USABLE_TARGETS", "No valid target remains.")

    ev_out = []
    for ev in events:
        msgs = per_event[ev["event_uid"]]
        status = (
            "INVALID"
            if any(m["severity"] == "ERROR" for m in msgs)
            else ("WARNING" if any(m["severity"] == "WARNING" for m in msgs) else "VALID")
        )
        ev_out.append({"event_uid": ev["event_uid"], "status": status, "messages": msgs})
    errors = sum(m["severity"] == "ERROR" for m in scen_msgs) + sum(e["status"] == "INVALID" for e in ev_out)
    warnings = sum(m["severity"] == "WARNING" for m in scen_msgs) + sum(
        m["severity"] == "WARNING" for e in ev_out for m in e["messages"]
    )
    return {
        "valid": errors == 0, "can_run": errors == 0 and bool(events), "error_count": errors, "warning_count": warnings,
        "scenario_messages": scen_msgs, "events": ev_out,
        "checks": [{"number": n + 1, "name": name, "passed": n not in failed} for n, name in enumerate(CHECKS)],
        "assumptions_checked": a.model_dump(mode="json") if a else None,
    }  # fmt: skip
