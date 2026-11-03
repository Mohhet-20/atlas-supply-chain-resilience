"""Scenario persistence: definitions, events, history, validation marking and output shaping.

A scenario change bumps ``version``, returns a READY scenario to DRAFT (it must be validated again) and appends a history
row holding the full definition. Runs keep their own frozen copy, so none of this rewrites earlier results.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    DomainValidationError,
    InvalidTransitionError,
    NotFoundError,
    VersionConflictError,
)
from app.models.enums import EventType, EventValidation, RegistryType, ScenarioStatus, ScenarioType, SimStatus
from app.models.scenario import (
    Scenario,
    ScenarioEvent,
    ScenarioHistory,
    SimulationAlternative,
    SimulationEdgeImpact,
    SimulationImpact,
    SimulationRun,
)
from app.models.types import utcnow
from app.scenarios import config as cfg
from app.scenarios.validation import Limits, validate_definition

EVENT_FIELDS = (
    "event_type", "label", "target_type", "target_id", "relationship_ref", "targets", "region_label", "effect", "magnitude_pct",
    "added_lead_time_days", "start_offset_days", "duration_days", "recovery_days", "assumption_notes", "input_provenance",
)  # fmt: skip


def new_uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


# ------------------------------------------------------------------------------------------------ definition


def _enum_val(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def event_def(e: ScenarioEvent) -> dict[str, Any]:
    d = {f: _enum_val(getattr(e, f)) for f in EVENT_FIELDS}
    d["event_uid"] = e.event_uid
    d["position"] = e.position
    return d


def definition_of(db: Session, sc: Scenario) -> dict[str, Any]:
    events = db.scalars(
        select(ScenarioEvent)
        .where(ScenarioEvent.scenario_id == sc.id)
        .order_by(ScenarioEvent.position, ScenarioEvent.id)
    ).all()
    return {
        "scenario": {
            "scenario_uid": sc.scenario_uid, "name": sc.name, "description": sc.description, "scenario_type": sc.scenario_type.value,
            "template_key": sc.template_key, "version": sc.version, "assumptions": sc.assumptions, "config_version": sc.config_version, "notes": sc.notes,
        },
        "events": [event_def(e) for e in events],
    }  # fmt: skip


def definition_hash(defn: dict[str, Any]) -> str:
    """Hash of what can change a result: assumptions, type and events (not names, notes or versions)."""
    core = {
        "type": defn["scenario"]["scenario_type"], "assumptions": defn["scenario"]["assumptions"],
        "events": [{k: v for k, v in e.items() if k not in ("event_uid", "position", "label", "assumption_notes", "input_provenance")} for e in defn["events"]],
    }  # fmt: skip
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def _history(db: Session, sc: Scenario, actor: str, action: str, summary: str) -> None:
    db.add(
        ScenarioHistory(
            scenario_id=sc.id,
            version=sc.version,
            action=action,
            actor=actor,
            at=utcnow(),
            summary=summary[:300],
            definition=definition_of(db, sc),
        )
    )


def _touch(db: Session, sc: Scenario, actor: str, action: str, summary: str) -> None:
    if sc.status is ScenarioStatus.ARCHIVED:
        raise InvalidTransitionError("An archived scenario cannot be edited. Restore it first.")
    sc.version += 1
    sc.updated_by = actor
    if sc.status is ScenarioStatus.READY:
        sc.status = ScenarioStatus.DRAFT
    db.flush()
    _history(db, sc, actor, action, summary)


def get_scenario(db: Session, scenario_id: int) -> Scenario:
    sc = db.get(Scenario, scenario_id)
    if sc is None:
        raise NotFoundError("Scenario not found.")
    return sc


def _check_version(sc: Scenario, expected: int | None) -> None:
    if expected is not None and expected != sc.version:
        raise VersionConflictError(
            f"The scenario changed (now version {sc.version}, you edited version {expected}). Reload and retry."
        )


def _clean_assumptions(raw: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return cfg.Assumptions.model_validate(raw or {}).model_dump(mode="json")
    except Exception as exc:  # pydantic ValidationError -> a user-presentable error
        raise DomainValidationError(
            f"Invalid assumptions: {str(exc).splitlines()[1] if len(str(exc).splitlines()) > 1 else exc}"
        ) from None


def create_scenario(
    db: Session,
    actor: str,
    *,
    name: str,
    description: str | None,
    scenario_type: ScenarioType,
    assumptions: dict[str, Any] | None,
    notes: str | None,
    template_key: str | None,
    max_scenarios: int,
) -> Scenario:
    if (db.scalar(select(func.count()).select_from(Scenario)) or 0) >= max_scenarios:
        raise ConflictError(f"The scenario limit of {max_scenarios} is reached; archive or remove unused scenarios.")
    sc = Scenario(scenario_uid=new_uid("SC"), name=name.strip(), description=description, scenario_type=scenario_type, template_key=template_key, status=ScenarioStatus.DRAFT, version=1, assumptions=_clean_assumptions(assumptions), config_version=cfg.CONFIG_VERSION, notes=notes, owner=actor, updated_by=actor)  # fmt: skip
    db.add(sc)
    db.flush()
    _history(db, sc, actor, "scenario.created", f"Scenario created: {sc.name}")
    return sc


def update_scenario(db: Session, actor: str, sc: Scenario, patch: dict[str, Any], expected_version: int) -> Scenario:
    _check_version(sc, expected_version)
    changed = []
    for key in ("name", "description", "scenario_type", "notes"):
        if key in patch and patch[key] != getattr(sc, key):
            setattr(sc, key, patch[key].strip() if key == "name" else patch[key])
            changed.append(key)
    if "assumptions" in patch:
        new = _clean_assumptions(patch["assumptions"])
        if new != sc.assumptions:
            sc.assumptions = new
            changed.append("assumptions")
    if not changed:
        return sc
    _touch(db, sc, actor, "scenario.updated", "Updated " + ", ".join(changed))
    return sc


def archive(db: Session, actor: str, sc: Scenario, expected_version: int | None) -> Scenario:
    _check_version(sc, expected_version)
    if sc.status is ScenarioStatus.ARCHIVED:
        return sc
    active = db.scalar(
        select(func.count())
        .select_from(SimulationRun)
        .where(SimulationRun.scenario_id == sc.id, SimulationRun.status.in_([SimStatus.QUEUED, SimStatus.RUNNING]))
    )
    if active:
        raise ConflictError("The scenario has a queued or running simulation. Cancel it or wait for it to finish.")
    sc.status, sc.archived_at, sc.version, sc.updated_by = ScenarioStatus.ARCHIVED, utcnow(), sc.version + 1, actor
    db.flush()
    _history(db, sc, actor, "scenario.archived", "Archived (run history kept)")
    return sc


def restore(db: Session, actor: str, sc: Scenario) -> Scenario:
    if sc.status is not ScenarioStatus.ARCHIVED:
        raise InvalidTransitionError("Only an archived scenario can be restored.")
    sc.status, sc.archived_at, sc.version, sc.updated_by = ScenarioStatus.DRAFT, None, sc.version + 1, actor
    db.flush()
    _history(db, sc, actor, "scenario.restored", "Restored to DRAFT; validate before running")
    return sc


def events_of(db: Session, sc: Scenario) -> list[ScenarioEvent]:
    return list(
        db.scalars(
            select(ScenarioEvent)
            .where(ScenarioEvent.scenario_id == sc.id)
            .order_by(ScenarioEvent.position, ScenarioEvent.id)
        )
    )


def _event_values(body: dict[str, Any]) -> dict[str, Any]:
    vals = {k: v for k, v in body.items() if k in EVENT_FIELDS}
    if "event_type" in vals:
        vals["event_type"] = EventType(vals["event_type"])
    if vals.get("target_type"):
        vals["target_type"] = RegistryType(vals["target_type"])
    if vals.get("targets") is not None:
        vals["targets"] = [{"type": RegistryType(t["type"]).value, "id": int(t["id"])} for t in vals["targets"]]
    return vals


def add_event(
    db: Session, actor: str, sc: Scenario, body: dict[str, Any], expected_version: int | None, max_events: int
) -> ScenarioEvent:
    _check_version(sc, expected_version)
    existing = events_of(db, sc)
    if len(existing) >= max_events:
        raise DomainValidationError(f"A scenario may contain at most {max_events} events.")
    ev = ScenarioEvent(event_uid=new_uid("EV"), scenario_id=sc.id, position=(existing[-1].position + 1) if existing else 0, validation_status=EventValidation.UNVALIDATED, **_event_values(body))  # fmt: skip
    db.add(ev)
    db.flush()
    _touch(db, sc, actor, "event.added", f"Added event {ev.event_type.value}")
    return ev


def get_event(db: Session, sc: Scenario, event_id: int) -> ScenarioEvent:
    ev = db.get(ScenarioEvent, event_id)
    if ev is None or ev.scenario_id != sc.id:
        raise NotFoundError("Event not found in this scenario.")
    return ev


def update_event(
    db: Session, actor: str, sc: Scenario, ev: ScenarioEvent, body: dict[str, Any], expected_version: int | None
) -> ScenarioEvent:
    _check_version(sc, expected_version)
    vals = _event_values(body)
    for k, v in vals.items():
        setattr(ev, k, v)
    ev.validation_status, ev.validation_messages = EventValidation.UNVALIDATED, None
    db.flush()
    _touch(db, sc, actor, "event.updated", f"Updated event {ev.event_uid}")
    return ev


def remove_event(db: Session, actor: str, sc: Scenario, ev: ScenarioEvent, expected_version: int | None) -> None:
    _check_version(sc, expected_version)
    uid = ev.event_uid
    db.delete(ev)
    db.flush()
    _touch(db, sc, actor, "event.removed", f"Removed event {uid}")


def validate_scenario(db: Session, sc: Scenario, limits: Limits, *, mark: bool = True) -> dict[str, Any]:
    defn = definition_of(db, sc)
    report = validate_definition(db, defn, limits)
    if mark:
        by_uid = {e["event_uid"]: e for e in report["events"]}
        for ev in events_of(db, sc):
            r = by_uid.get(ev.event_uid)
            if r:
                ev.validation_status = EventValidation(r["status"])
                ev.validation_messages = r["messages"]
        if sc.status is not ScenarioStatus.ARCHIVED:
            if report["can_run"]:
                sc.status, sc.validated_version = ScenarioStatus.READY, sc.version
            elif sc.status is ScenarioStatus.READY:
                sc.status = ScenarioStatus.DRAFT
        db.flush()
    report["scenario_version"] = sc.version
    report["scenario_status"] = sc.status.value
    return report


# --------------------------------------------------------------------------------------------------- output


def event_out(e: ScenarioEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        **event_def(e),
        "validation_status": e.validation_status.value,
        "validation_messages": e.validation_messages or [],
        "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


def scenario_out(
    sc: Scenario, events: list[ScenarioEvent] | None = None, run_count: int | None = None
) -> dict[str, Any]:
    out = {
        "id": sc.id, "scenario_uid": sc.scenario_uid, "name": sc.name, "description": sc.description, "scenario_type": sc.scenario_type.value,
        "template_key": sc.template_key, "status": sc.status.value, "version": sc.version, "assumptions": sc.assumptions,
        "config_version": sc.config_version, "notes": sc.notes, "owner": sc.owner, "updated_by": sc.updated_by, "created_at": sc.created_at,
        "updated_at": sc.updated_at, "archived_at": sc.archived_at, "validated_version": sc.validated_version,
        "validation_current": sc.validated_version == sc.version, "run_count": run_count,
        "disclaimer": "A scenario is a hypothetical definition. It is not an observed event or a forecast.",
    }  # fmt: skip
    if events is not None:
        out["events"] = [event_out(e) for e in events]
        out["event_count"] = len(events)
    return out


def run_out(r: SimulationRun, scenario_name: str | None = None, full: bool = False) -> dict[str, Any]:
    out = {
        "id": r.id, "run_uid": r.run_uid, "scenario_id": r.scenario_id, "scenario_name": scenario_name, "scenario_version": r.scenario_version,
        "status": r.status.value, "requested_by": r.requested_by, "queued_at": r.queued_at, "started_at": r.started_at,
        "finished_at": r.finished_at, "duration_ms": r.duration_ms, "engine_version": r.engine_version, "config_hash": r.config_hash,
        "definition_hash": r.definition_hash, "graph_hash": r.graph_hash, "graph_snapshot": r.graph_snapshot,
        "entities_examined": r.entities_examined, "entities_affected": r.entities_affected, "direct_count": r.direct_count,
        "indirect_count": r.indirect_count, "truncated": r.truncated, "truncation_reasons": r.truncation_reasons or [],
        "cancel_requested": r.cancel_requested, "complete": r.status is SimStatus.COMPLETED,
        "warnings": r.warnings or [], "missing_data": r.missing_data or [], "failures": r.failures or [], "error": r.error,
        "assumptions": r.assumptions, "summary": r.summary,
        "disclaimer": cfg.EXPLAIN_NOTE,
    }  # fmt: skip
    if full:
        out["scenario_snapshot"] = r.scenario_snapshot
    return out


def impact_out(i: SimulationImpact) -> dict[str, Any]:
    return {
        "id": i.id, "entity_type": i.entity_type.value, "entity_id": i.entity_id, "entity_code": i.entity_code, "entity_name": i.entity_name,
        "impact_kind": i.impact_kind.value, "depth": i.depth, "event_uids": i.event_uids or [], "quantification": i.quantification.value,
        "exposure_level": i.exposure_level, "loss_pct": i.loss_pct, "loss_pct_lower_bound": i.loss_pct_lower_bound,
        "capacity_lost_value": i.capacity_lost_value, "capacity_unit": i.capacity_unit, "added_lead_time_days": i.added_lead_time_days,
        "path_count": i.path_count, "primary_path": i.primary_path or [], "parent_edges": i.parent_edges or [], "rules": i.rules or [],
        "uncertainty": i.uncertainty or [], "explanation": i.explanation, "potential_exposure": True,
    }  # fmt: skip


def edge_out(e: SimulationEdgeImpact) -> dict[str, Any]:
    return {
        c: (getattr(e, c).value if hasattr(getattr(e, c), "value") else getattr(e, c))
        for c in (
            "edge_ref",
            "edge_type",
            "source_type",
            "source_id",
            "destination_type",
            "destination_id",
            "effect",
            "supply_share_pct",
            "capacity_value",
            "capacity_unit",
            "lead_time_days",
            "added_lead_time_days",
            "event_uids",
        )
    }


def alt_out(a: SimulationAlternative) -> dict[str, Any]:
    return {
        c: (getattr(a, c).value if hasattr(getattr(a, c), "value") else getattr(a, c))
        for c in (
            "for_type",
            "for_id",
            "source_type",
            "source_id",
            "source_code",
            "source_name",
            "edge_ref",
            "edge_type",
            "edge_status",
            "supply_share_pct",
            "capacity_value",
            "capacity_unit",
            "lead_time_days",
            "confidence",
            "missing",
            "caveat",
        )
    }
