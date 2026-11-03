"""Safe, deterministic automated remediation.

Only fixes whose result is fully determined by the current value are offered. Nothing here invents a value, confirms a
relationship, merges records or deletes anything. Each fix runs in its own savepoint so one failure never undoes others.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import Actor, Role
from app.core.errors import DomainValidationError, ForbiddenError, VersionConflictError
from app.models.enums import EntityKind, LocationStatus, RegistryType, ResolutionStatus
from app.models.ingestion import DataQualityFinding
from app.quality.findings import OPEN_LIKE, add_event, get_finding
from app.quality.framework import catalog
from app.quality.rules_entity import _CONTROL, TEXT_FIELDS
from app.registry import audit
from app.registry.entities import MODELS

MAX_BULK = 25
REQUIRED_TEXT = {"name", "country_or_region"}


def _trim(entity: Any, kind: EntityKind) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for f in TEXT_FIELDS:
        v = getattr(entity, f, None)
        if isinstance(v, str):
            new = " ".join(_CONTROL.sub(" ", v).split()) if _CONTROL.search(v) else v.strip()
            if new != v:
                if not new and f in REQUIRED_TEXT:
                    raise DomainValidationError(f"'{f}' would become empty; fix it by hand.")
                changes[f] = new or None
    if not changes:
        raise DomainValidationError("Nothing to trim; the record already looks clean.")
    return changes


def _clear_coords(entity: Any, kind: EntityKind) -> dict[str, Any]:
    if kind is not EntityKind.FACILITY or entity.latitude != 0 or entity.longitude != 0:
        raise DomainValidationError("The coordinates are no longer the (0, 0) placeholder.")
    return {"latitude": None, "longitude": None, "location_status": LocationStatus.UNKNOWN}


FIXES = {"TRIM_TEXT": _trim, "CLEAR_PLACEHOLDER_COORDINATES": _clear_coords}


def apply_fix(
    session: Session, actor: Actor, finding_id: int, expected_version: int, request_id: str | None
) -> tuple[dict[str, Any], tuple[EntityKind, int]]:
    """Apply the finding's safe fix. Caller wraps in a transaction; re-evaluation happens after commit."""
    if actor.role < Role.REVIEWER:
        raise ForbiddenError("Applying an automated fix requires the reviewer role.")
    f = get_finding(session, finding_id)
    if f.version != expected_version:
        raise VersionConflictError(f"The finding changed (current version {f.version}). Reload and retry.")
    rule = catalog().get(f.rule_id)
    if rule is None or not rule.fix_action or rule.fix_action not in FIXES:
        raise DomainValidationError("This finding has no safe automated fix.")
    if f.resolution_status not in OPEN_LIKE:
        raise DomainValidationError("Only open findings can be fixed.")
    if f.entity_type not in (EntityKind.SUPPLIER, EntityKind.FACILITY, EntityKind.PRODUCT) or f.entity_id is None:
        raise DomainValidationError("This finding is not about a registry record.")
    entity = session.get(MODELS[RegistryType(f.entity_type.value)], f.entity_id)
    if entity is None or entity.merged_into_id is not None:
        raise DomainValidationError("The record no longer exists or was merged.")
    changes = FIXES[rule.fix_action](entity, f.entity_type)
    before = {k: ("(set)" if k in ("latitude", "longitude") else getattr(entity, k)) for k in changes}
    try:
        with session.begin_nested():
            for k, v in changes.items():
                setattr(entity, k, v)
            session.flush()
    except IntegrityError as exc:
        raise DomainValidationError("The fix conflicts with another record; resolve it by hand.") from exc
    except SQLAlchemyError as exc:
        raise VersionConflictError("The record changed concurrently. Reload and retry.") from exc
    audit.record(
        session,
        actor,
        "quality.remediation.applied",
        target_type=f.entity_type.value,
        target_id=entity.id,
        details={"fix": rule.fix_action, "rule": f.rule_id, "fields": sorted(changes), "finding_id": f.id},
        request_id=request_id,
    )
    old = f.resolution_status
    f.resolution_status = ResolutionStatus.IN_REMEDIATION if old is not ResolutionStatus.IN_REMEDIATION else old
    f.version += 1
    session.flush()
    add_event(
        session,
        f,
        actor.name,
        "AUTO_FIX",
        old.value,
        f.resolution_status.value,
        details={"fix": rule.fix_action, "fields": sorted(changes), "before": before},
    )
    return {"finding_id": f.id, "fix": rule.fix_action, "fields": sorted(changes), "status": "APPLIED"}, (
        f.entity_type,
        f.entity_id,
    )


def apply_bulk(
    session: Session, actor: Actor, items: list[tuple[int, int]], request_id: str | None
) -> tuple[list[dict[str, Any]], set[tuple[EntityKind, int]]]:
    if len(items) > MAX_BULK:
        raise DomainValidationError(f"At most {MAX_BULK} findings per bulk fix.")
    if len({i for i, _ in items}) != len(items):
        raise DomainValidationError("Duplicate finding ids in request.")
    results, touched = [], set()
    for fid, ver in items:
        try:
            with session.begin_nested():
                res, key = apply_fix(session, actor, fid, ver, request_id)
            results.append(res)
            touched.add(key)
        except ForbiddenError:
            raise
        except Exception as exc:  # per-item failure is reported, not raised
            results.append(
                {"finding_id": fid, "status": "SKIPPED", "reason": getattr(exc, "message", None) or str(exc)[:200]}
            )
    return results, touched


__all__ = ["apply_fix", "apply_bulk", "MAX_BULK", "DataQualityFinding"]
