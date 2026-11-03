"""Declarative mapping of an external record into an ATLAS alert-event.

Nothing is invented: a missing required source field rejects the record (with a reason) rather than being defaulted, defaults exist
only where the mapping states one, and source fields the mapping does not name are *listed* in the result (``unmapped``) so the
run can report them; the alert-event payload schema is closed, so they are not stored. ``unknown: reject`` rejects such records. Provenance is always IMPORTED_EXTERNAL and the source is the integration, so every event traces
back to its integration and external id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MAPPING_VERSION = 1
TYPES = {"str", "int", "float", "datetime", "bool"}
UNITS = {  # (from, to) -> factor; deliberately small and explicit
    ("kg", "t"): 0.001, ("t", "kg"): 1000.0, ("lb", "kg"): 0.45359237, ("kg", "lb"): 2.2046226218,
    ("m", "km"): 0.001, ("km", "m"): 1000.0, ("g", "kg"): 0.001, ("kg", "g"): 1000.0,
}  # fmt: skip


class MappingError(ValueError):
    pass


def _get(rec: dict[str, Any], path: str) -> Any:
    cur: Any = rec
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def validate_spec(spec: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if spec.get("version", MAPPING_VERSION) != MAPPING_VERSION:
        errs.append(f"version: only {MAPPING_VERSION} is supported")
    for k in ("event_type", "id", "occurred_at"):
        if not isinstance(spec.get(k), str) or not spec.get(k):
            errs.append(f"{k}: required")
    fields = spec.get("fields", {})
    if not isinstance(fields, dict) or not fields:
        errs.append("fields: at least one payload field is required")
        fields = {}
    for name, f in fields.items():
        if not isinstance(f, dict) or not isinstance(f.get("from"), str):
            errs.append(f"fields.{name}.from: required")
            continue
        if f.get("type", "str") not in TYPES:
            errs.append(f"fields.{name}.type: must be one of {', '.join(sorted(TYPES))}")
        if (f.get("unit_from"), f.get("unit_to")) != (None, None) and (
            f.get("unit_from"),
            f.get("unit_to"),
        ) not in UNITS:
            errs.append(f"fields.{name}: unsupported unit conversion")
    if spec.get("unknown", "keep") not in ("keep", "drop", "reject"):
        errs.append("unknown: must be keep, drop or reject")
    return errs


def _convert(value: Any, f: dict[str, Any], tz: ZoneInfo | None) -> Any:
    t = f.get("type", "str")
    try:
        if t == "str":
            return str(value)[:200]
        if t == "int":
            return int(value)
        if t == "float":
            v = float(value)
            if f.get("unit_from"):
                v *= UNITS[(f["unit_from"], f["unit_to"])]
            return v
        if t == "bool":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1", "yes"):
                return True
            if str(value).lower() in ("false", "0", "no"):
                return False
            raise ValueError
        if t == "datetime":
            return to_utc(value, tz)
    except (TypeError, ValueError):
        raise MappingError(f"cannot convert {f['from']!r} to {t}") from None
    raise MappingError(f"unknown type {t}")


def to_utc(value: Any, tz: ZoneInfo | None) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz or timezone.utc)  # naive source times are interpreted in the configured source timezone
    return d.astimezone(timezone.utc)


def map_record(spec: dict[str, Any], rec: dict[str, Any], integration_uid: str) -> dict[str, Any]:
    """Return the fields of an alert EventBody, or raise MappingError."""
    tz = ZoneInfo(spec["source_timezone"]) if spec.get("source_timezone") else None
    ext_id = _get(rec, spec["id"])
    if ext_id in (None, ""):
        raise MappingError(f"required identifier {spec['id']!r} is missing")
    ext_id = str(ext_id)[:100]
    try:
        occurred = to_utc(_get(rec, spec["occurred_at"]), tz)
    except (TypeError, ValueError):
        raise MappingError(f"{spec['occurred_at']!r} is missing or not a date-time") from None
    payload: dict[str, Any] = {}
    used = {spec["id"], spec["occurred_at"]}
    for name, f in spec["fields"].items():
        used.add(f["from"].split(".")[0])
        v = _get(rec, f["from"])
        if v is None or v == "":
            if "default" in f:
                v = f["default"]
            elif f.get("required", True):
                raise MappingError(f"required field {f['from']!r} is missing")
            else:
                continue
        payload[name] = _convert(v, f, tz)
    leftovers = {
        k: v
        for k, v in rec.items()
        if k not in used and k not in (spec.get("entity") or {}).get("from", "").split(".")[:1]
    }
    if leftovers:
        mode = spec.get("unknown", "keep")
        if mode == "reject":
            raise MappingError(f"unmapped fields: {', '.join(sorted(leftovers))}")
        # kept values are not added to the payload: the alert-event schema is closed, so they are reported, not invented into it
    entities = []
    ent = spec.get("entity")
    if ent:
        eid = _get(rec, ent["from"])
        if eid not in (None, ""):
            entities.append({"entity_type": ent["entity_type"], "entity_id": str(eid)[:64]})
    return {
        "event_type": spec["event_type"],
        "source_record_id": ext_id,
        "idempotency_key": f"{integration_uid}:{ext_id}:{occurred.isoformat()}"[:128],
        "occurred_at": occurred,
        "payload": payload,
        "entities": entities,
        "unmapped": sorted(leftovers),
    }


SANDBOX_MAPPING: dict[str, Any] = {
    "version": 1, "event_type": "metric.observation", "id": "external_id", "occurred_at": "updated_at", "unknown": "keep",
    "entity": {"entity_type": "facility", "from": "facility"},
    "fields": {"metric": {"from": "metric", "type": "str"}, "value": {"from": "quantity", "type": "float"}, "unit": {"from": "unit", "type": "str"}},
}  # fmt: skip
