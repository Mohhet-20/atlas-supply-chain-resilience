"""Conservative, deterministic value normalization. Never guesses: ambiguous input is an error."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from app.ingestion.constants import (
    ALLOWED_DATE_FORMATS,
    EXTERNAL_ID_PATTERN,
    MAX_FIELD_CHARS,
)
from app.ingestion.fieldspecs import FieldSpec
from app.ingestion.rules import Finding

NUMBER_RE = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: Any) -> str | None:
    """NFC + strip. Empty string means *unknown* (None). Never alters case or inner content."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if len(text) > MAX_FIELD_CHARS:
        text = text[: MAX_FIELD_CHARS + 1]  # flagged as too long by field validation
    text = unicodedata.normalize("NFC", text).strip()
    return text or None


def apply_transforms(value: str | None, transforms: list[str]) -> str | None:
    for t in transforms:
        if value is None:
            return None
        if t == "upper":
            value = value.upper()
        elif t == "lower":
            value = value.lower()
        elif t == "trim":
            value = value.strip()
        elif t.startswith("strip_prefix:"):
            prefix = t.split(":", 1)[1]
            if prefix and value.startswith(prefix):
                value = value[len(prefix) :]
        elif t.startswith("scale:"):
            value = _scale(value, t.split(":", 1)[1])
        value = value.strip() or None if value is not None else None
    return value


def _scale(value: str, factor: str) -> str:
    if not NUMBER_RE.match(value):
        return value  # left for the number rule to flag with the original text
    return repr(float(value) * float(factor))


def parse_number(text: str) -> float:
    if not NUMBER_RE.match(text):
        raise ValueError("not a plain decimal number")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("not finite")
    return number


def parse_date(text: str, fmt: str) -> date:
    if fmt not in ALLOWED_DATE_FORMATS:
        raise ValueError("unsupported date format")
    return datetime.strptime(text, fmt).date()  # noqa: DTZ007 - calendar date only, no timezone semantics


def normalize_external_id(text: str | None) -> str | None:
    if text is None:
        return None
    return unicodedata.normalize("NFC", text).strip()


def coerce_field(spec: FieldSpec, raw: str | None, date_format: str) -> tuple[Any, list[Finding]]:
    """Validate and coerce one cleaned string to its canonical Python value."""
    f = spec.name
    if raw is None:
        if spec.required:
            return None, [Finding("DQ001", f"Required field '{f}' is empty or missing.", field=f)]
        return None, []
    if _CONTROL.search(raw):
        return None, [
            Finding("DQ003" if spec.kind == "number" else "DQ013", f"'{f}' contains control characters.", field=f)
        ]
    if spec.kind == "id":
        if not EXTERNAL_ID_PATTERN.match(raw):
            return None, [
                Finding(
                    "DQ002",
                    "External identifier violates the policy "
                    "(1-64 chars, letters/digits then . _ - / #; no spaces or ':').",
                    field=f,
                    observed=raw,
                    suggestion="Use the source system's stable identifier without spaces or ':'.",
                )
            ]
        return raw, []
    if spec.kind in ("text", "ref"):
        if len(raw) > spec.max_len and spec.kind == "text":
            return None, [Finding("DQ018", f"'{f}' is longer than {spec.max_len} characters.", field=f)]
        if spec.kind == "ref" and len(raw) > 130:
            return None, [Finding("DQ018", f"'{f}' reference is too long.", field=f)]
        return raw, []
    if spec.kind == "enum":
        assert spec.enum is not None  # noqa: S101
        candidate = re.sub(r"[\s\-]+", "_", raw.upper())
        allowed = {m.value for m in spec.enum}
        if candidate not in allowed:
            return None, [
                Finding(
                    "DQ005",
                    f"'{raw[:40]}' is not a supported value for '{f}'.",
                    field=f,
                    observed=raw,
                    suggestion="Allowed: " + ", ".join(sorted(allowed)) + ". Use a profile value_map to translate.",
                )
            ]
        return spec.enum(candidate), []
    if spec.kind == "number":
        try:
            number = parse_number(raw)
        except ValueError:
            return None, [
                Finding(
                    "DQ003",
                    f"'{f}' is not a valid number.",
                    field=f,
                    observed=raw,
                    suggestion="Use plain decimals such as 1234.5 (no thousands separators).",
                )
            ]
        if spec.nonneg and number < 0:
            return None, [Finding("DQ006", f"'{f}' must not be negative.", field=f, observed=raw)]
        if (spec.minimum is not None and number < spec.minimum) or (spec.maximum is not None and number > spec.maximum):
            return None, [
                Finding("DQ007", f"'{f}' must be between {spec.minimum} and {spec.maximum}.", field=f, observed=raw)
            ]
        return number, []
    if spec.kind == "date":
        try:
            return parse_date(raw, date_format), []
        except ValueError:
            return None, [
                Finding(
                    "DQ004",
                    f"'{f}' is not a valid date in format {date_format}.",
                    field=f,
                    observed=raw,
                    suggestion="Declare date_format in the mapping profile; ambiguous dates are never guessed.",
                )
            ]
    raise AssertionError(f"unknown field kind {spec.kind}")  # pragma: no cover
