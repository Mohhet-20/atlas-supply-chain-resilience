"""Rule contract, catalog registry and finding identity.

Rules are registered Python functions with declarative metadata. There is deliberately no mechanism to load,
compile or evaluate user-supplied expressions: operators may only enable or disable catalog rules and tune the
thresholds that the existing, validated ``QualityConfig`` already exposes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.models.enums import EntityKind, QualityDimension, Severity

# Severity weights used by scoring. INFO findings are reported but never lower a score.
SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.INFO: 0.0,
    Severity.WARNING: 1.0,
    Severity.ERROR: 3.0,
    Severity.CRITICAL: 5.0,
}

ENTITY_KINDS = (EntityKind.SUPPLIER, EntityKind.FACILITY, EntityKind.PRODUCT)
EDGE_KINDS = (EntityKind.RELATIONSHIP, EntityKind.PRODUCT_LINK)


@dataclass(frozen=True)
class Check:
    """The result of one rule check on one subject. Passed checks are kept: they form the score's denominator."""

    field: str | None
    passed: bool
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    severity: Severity | None = None  # overrides the rule severity for the finding (not for the score weight)


@dataclass(frozen=True)
class Subject:
    kind: EntityKind
    id: int
    obj: Any  # registry entity row, or an EdgeRec for relationships / product links
    source_id: int | None
    label: str
    ref: str | None = None  # SUPPLY:3 / PRODUCT:5 for relationships


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    version: int
    name: str
    description: str
    dimension: QualityDimension
    severity: Severity
    applies_to: tuple[EntityKind, ...]
    evaluate: Callable[[Subject, Any], list[Check]]
    remediation: str
    requires_review: bool = False
    default_enabled: bool = True
    params: tuple[str, ...] = ()  # names of QualityConfig thresholds the rule reads (documentation only)
    fix_action: str | None = None  # safe automated remediation key, if one exists

    @property
    def weight(self) -> float:
        return SEVERITY_WEIGHT[self.severity]


_CATALOG: dict[str, RuleSpec] = {}


def register(spec: RuleSpec) -> RuleSpec:
    if spec.rule_id in _CATALOG:
        raise ValueError(f"duplicate rule id {spec.rule_id}")
    if not re.fullmatch(r"QR\d{3}", spec.rule_id):
        raise ValueError("rule ids look like QR101")
    _CATALOG[spec.rule_id] = spec
    return spec


def catalog() -> dict[str, RuleSpec]:
    from app.quality import rules_entity, rules_relationship  # noqa: F401  (registers on import)

    return dict(sorted(_CATALOG.items()))


def finding_key(rule_id: str, kind: EntityKind, subject_id: int, field_name: str | None) -> str:
    raw = f"{rule_id}|{kind.value}|{subject_id}|{field_name or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def catalog_hash(enabled: dict[str, bool], config_hash: str, formula_version: str) -> str:
    """Identity of everything that can change a score: rules, their versions, enabled flags, thresholds, formula."""
    payload = {
        "rules": [
            [r.rule_id, r.version, r.severity.value, bool(enabled.get(r.rule_id, r.default_enabled))]
            for r in catalog().values()
        ],
        "config": config_hash,
        "formula": formula_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------------------------------ evidence hygiene

SENSITIVE_FIELDS = frozenset({"address", "latitude", "longitude"})
_SECRETISH = re.compile(r"(?i)(bearer\s+\S+|token[=:]\s*\S+|password[=:]\s*\S+|api[_-]?key[=:]\s*\S+)")


def safe_value(field_name: str | None, value: Any, limit: int = 80) -> Any:
    """Evidence may show a value only when it is not location/contact-like; secrets-looking text is masked."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, date | datetime):
        return value.isoformat()
    if field_name in SENSITIVE_FIELDS:
        return "(hidden)"
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = _SECRETISH.sub("(masked)", text)
    return text[:limit]
