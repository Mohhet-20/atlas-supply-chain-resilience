"""Deterministic data-quality rule catalog. Rule ids are stable and documented."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.models.enums import Severity


@dataclass(frozen=True)
class Rule:
    rule_id: str
    name: str
    severity: Severity
    description: str


_RULES = [
    Rule("DQ001", "missing_required_field", Severity.ERROR, "A required field is empty or not mapped."),
    Rule("DQ002", "invalid_identifier", Severity.ERROR, "External identifier violates the identifier policy."),
    Rule("DQ003", "invalid_number", Severity.ERROR, "Value is not a finite decimal number."),
    Rule("DQ004", "invalid_date", Severity.ERROR, "Value is not a valid calendar date in the declared format."),
    Rule("DQ005", "unsupported_enum_value", Severity.ERROR, "Value is not one of the allowed values."),
    Rule("DQ006", "negative_value", Severity.ERROR, "A quantity that cannot be negative is negative."),
    Rule("DQ007", "out_of_range", Severity.ERROR, "Value is outside its valid range."),
    Rule(
        "DQ008",
        "unknown_field",
        Severity.WARNING,
        "Source field is not mapped (severity follows the unknown-field policy).",
    ),
    Rule("DQ009", "unresolved_reference", Severity.ERROR, "A referenced record does not exist in the registry."),
    Rule(
        "DQ010",
        "duplicate_in_batch",
        Severity.ERROR,
        "Same external identifier appears earlier in this batch; first occurrence wins.",
    ),
    Rule(
        "DQ011",
        "possible_duplicate",
        Severity.WARNING,
        "Another record looks similar. A candidate only: never merged automatically.",
    ),
    Rule(
        "DQ012",
        "inconsistent_reference",
        Severity.ERROR,
        "Endpoint combination is invalid or an immutable link would change.",
    ),
    Rule("DQ013", "malformed_row", Severity.ERROR, "Row could not be parsed into fields."),
    Rule(
        "DQ014",
        "constraint_violation",
        Severity.ERROR,
        "The database rejected the record (constraint or concurrent change).",
    ),
    Rule("DQ015", "duplicate_relationship", Severity.ERROR, "A relationship with the same endpoints already exists."),
    Rule("DQ016", "unsupported_unit", Severity.ERROR, "Unit is not covered by the profile's unit rules."),
    Rule(
        "DQ017",
        "conflicting_fields",
        Severity.ERROR,
        "Related fields are inconsistent (paired values, date range, self reference).",
    ),
    Rule("DQ018", "value_too_long", Severity.ERROR, "Value exceeds the maximum length."),
    Rule("DQ019", "file_level_problem", Severity.CRITICAL, "The file/payload as a whole cannot be processed."),
    Rule("DQ020", "missing_optional_context", Severity.INFO, "Optional context is unknown (never fabricated)."),
]
RULES: dict[str, Rule] = {r.rule_id: r for r in _RULES}


@dataclass
class Finding:
    rule_id: str
    message: str
    field: str | None = None
    severity: Severity | None = None
    suggestion: str | None = None
    observed: str | None = None
    row_index: int | None = None
    source_line: int | None = None
    external_id: str | None = None

    def __post_init__(self) -> None:
        if self.severity is None:
            self.severity = RULES[self.rule_id].severity
        if self.observed is not None:
            self.observed = _safe_observed(self.observed)

    @property
    def blocking(self) -> bool:
        return self.severity in (Severity.ERROR, Severity.CRITICAL)

    def key(self, batch_uid: str) -> str:
        raw = f"{batch_uid}|{self.rule_id}|{self.row_index}|{self.field}|{self.message}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _safe_observed(value: str, limit: int = 120) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return cleaned[:limit]
