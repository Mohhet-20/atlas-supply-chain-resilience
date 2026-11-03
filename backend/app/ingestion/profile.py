"""Mapping-profile definitions: validation, hashing and the built-in canonical profile."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.ingestion.constants import ALLOWED_DATE_FORMATS, CANONICAL_PROFILE_REF, MAX_COLUMNS
from app.ingestion.fieldspecs import SPECS, spec_map
from app.models.enums import EntityKind

_TRANSFORM_RE = re.compile(r"^(trim|upper|lower|strip_prefix:[^\s]{1,32}|scale:-?\d{1,9}(\.\d{1,9})?)$")


class ColumnMapping(BaseModel):
    source: str = Field(min_length=1, max_length=120, description="Source column name or dotted JSON path")
    transform: list[str] = Field(default_factory=list, max_length=5)
    default: str | int | float | None = Field(default=None, description="Used only when the source value is empty")
    value_map: dict[str, str] = Field(default_factory=dict, max_length=200)

    @field_validator("transform")
    @classmethod
    def _transforms(cls, v: list[str]) -> list[str]:
        for t in v:
            if not _TRANSFORM_RE.match(t):
                raise ValueError(
                    f"unsupported transform '{t[:40]}' (allowed: trim, upper, lower, strip_prefix:X, scale:N)"
                )
        return v

    @field_validator("value_map")
    @classmethod
    def _vm(cls, v: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 120 or len(x) > 120 for k, x in v.items()):
            raise ValueError("value_map keys/values must be at most 120 characters")
        return v


class UnitRule(BaseModel):
    unit_field: str = Field(max_length=60, description="Canonical unit field that accompanies the value")
    target_unit: str = Field(min_length=1, max_length=32)
    factors: dict[str, float] = Field(description="Source unit (upper-case) -> multiplier into target_unit")

    @field_validator("factors")
    @classmethod
    def _f(cls, v: dict[str, float]) -> dict[str, float]:
        if not v or len(v) > 50:
            raise ValueError("factors must contain 1-50 entries")
        for unit, factor in v.items():
            if not (factor > 0) or factor == float("inf"):
                raise ValueError(f"factor for '{unit}' must be a positive finite number")
        return {k.strip().upper(): f for k, f in v.items()}


class ProfileDefinition(BaseModel):
    columns: dict[str, ColumnMapping] = Field(max_length=MAX_COLUMNS)
    unknown_field_policy: Literal["ignore", "warn", "reject"] = "warn"
    date_format: str = "%Y-%m-%d"
    null_tokens: list[str] = Field(default_factory=list, max_length=10)
    unit_rules: dict[str, UnitRule] = Field(default_factory=dict)

    @field_validator("date_format")
    @classmethod
    def _df(cls, v: str) -> str:
        if v not in ALLOWED_DATE_FORMATS:
            raise ValueError(f"date_format must be one of {list(ALLOWED_DATE_FORMATS)}")
        return v

    @field_validator("null_tokens")
    @classmethod
    def _nt(cls, v: list[str]) -> list[str]:
        if any(len(t) > 20 for t in v):
            raise ValueError("null tokens must be at most 20 characters")
        return v


def definition_problems(entity: EntityKind, d: ProfileDefinition) -> list[str]:
    """Semantic validation against the entity's canonical fields."""
    specs = spec_map(entity)
    problems: list[str] = []
    for canonical in d.columns:
        if canonical not in specs:
            problems.append(f"'{canonical}' is not a canonical {entity.value} field.")
    if "external_id" not in d.columns:
        problems.append("external_id must be mapped (it scopes identity within the source namespace).")
    for spec in SPECS[entity]:
        if spec.required and spec.name not in d.columns and not _has_default(d, spec.name):
            problems.append(f"Required field '{spec.name}' has no mapping.")
    sources = [m.source.strip().lower() for m in d.columns.values()]
    if len(sources) != len(set(sources)):
        problems.append("Two canonical fields map the same source column.")
    for fld, rule in d.unit_rules.items():
        if fld not in specs or specs[fld].kind != "number":
            problems.append(f"unit_rules key '{fld}' must be a numeric canonical field.")
        if rule.unit_field not in specs:
            problems.append(f"unit_rules '{fld}': unit_field '{rule.unit_field}' is not a canonical field.")
        if rule.target_unit.upper() not in rule.factors:
            problems.append(f"unit_rules '{fld}': factors must include target unit '{rule.target_unit}' (factor 1).")
    return problems


def _has_default(d: ProfileDefinition, name: str) -> bool:
    m = d.columns.get(name)
    return m is not None and m.default is not None


def canonical_definition(entity: EntityKind) -> ProfileDefinition:
    return ProfileDefinition(columns={s.name: ColumnMapping(source=s.name) for s in SPECS[entity]})


def definition_hash(d: ProfileDefinition) -> str:
    payload = json.dumps(d.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ResolvedProfile:
    ref: str  # "canonical:1" or "name:vN"
    definition: ProfileDefinition
    digest: str
    profile_id: int | None = None
    case_insensitive_headers: bool = True


def builtin_profile(entity: EntityKind) -> ResolvedProfile:
    d = canonical_definition(entity)
    return ResolvedProfile(CANONICAL_PROFILE_REF, d, definition_hash(d), None)


def definition_to_json(d: ProfileDefinition) -> dict[str, Any]:
    return d.model_dump(mode="json")
