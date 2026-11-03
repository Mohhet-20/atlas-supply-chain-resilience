"""Byte-level parsing for CSV and JSON. Content is only ever parsed as data, never executed."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.constants import (
    ALLOWED_DELIMITERS,
    ALLOWED_ENCODINGS,
    MAX_COLUMNS,
    MAX_FIELD_CHARS,
    MAX_JSON_DEPTH,
)
from app.ingestion.rules import Finding

csv.field_size_limit(MAX_FIELD_CHARS * 4)


@dataclass
class RawRow:
    index: int  # 1-based data row / record index
    line: int | None  # first physical line (CSV) of the record
    values: dict[str, Any]
    error: Finding | None = None


@dataclass
class ParsedFile:
    format: str
    columns: list[str] = field(default_factory=list)
    rows: list[RawRow] = field(default_factory=list)
    file_findings: list[Finding] = field(default_factory=list)
    encoding: str | None = None
    delimiter: str | None = None

    @property
    def fatal(self) -> bool:
        return any(f.blocking for f in self.file_findings)


def _fatal(parsed: ParsedFile, message: str, suggestion: str | None = None) -> ParsedFile:
    parsed.file_findings.append(Finding("DQ019", message, suggestion=suggestion))
    return parsed


def decode_bytes(data: bytes, encoding: str) -> tuple[str | None, str | None]:
    if encoding not in ALLOWED_ENCODINGS:
        return None, f"Unsupported encoding '{encoding[:20]}'. Allowed: {', '.join(ALLOWED_ENCODINGS)}."
    if b"\x00" in data and not encoding.startswith("utf-16"):
        return None, "Binary content detected (NUL bytes); this is not a text CSV/JSON file."
    try:
        return data.decode(encoding), None
    except (UnicodeDecodeError, LookupError):
        return None, f"File is not valid {encoding}. Re-save as UTF-8 or choose another encoding."


def detect_encoding(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def parse_csv(data: bytes, *, encoding: str, delimiter: str, max_rows: int) -> ParsedFile:
    parsed = ParsedFile(format="CSV", encoding=encoding)
    sep = ALLOWED_DELIMITERS.get(delimiter)
    if sep is None:
        return _fatal(parsed, f"Unsupported delimiter '{delimiter[:10]}'. Allowed: {', '.join(ALLOWED_DELIMITERS)}.")
    parsed.delimiter = delimiter
    text, err = decode_bytes(data, encoding)
    if err or text is None:
        return _fatal(parsed, err or "Undecodable content.")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=sep, strict=False)
    try:
        header = next(reader)
    except StopIteration:
        return _fatal(parsed, "File is empty.")
    except csv.Error as exc:
        return _fatal(parsed, f"Header row could not be parsed: {exc}")
    columns = [h.strip().lstrip("\ufeff").strip() if i == 0 else h.strip() for i, h in enumerate(header)]
    if not any(columns):
        return _fatal(parsed, "Header row is blank.")
    if len(columns) > MAX_COLUMNS:
        return _fatal(parsed, f"Too many columns ({len(columns)}; limit {MAX_COLUMNS}).")
    lowered = [c.lower() for c in columns if c]
    if len(lowered) != len(set(lowered)):
        return _fatal(parsed, "Duplicate column names in header (names are compared case-insensitively).")
    if any(not c for c in columns):
        parsed.file_findings.append(
            Finding("DQ008", "Header contains an empty column name; that column is ignored.", severity=None)
        )
    parsed.columns = [c for c in columns if c]
    last_line = reader.line_num
    index = 0
    while True:
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            index += 1
            parsed.rows.append(RawRow(index, last_line + 1, {}, Finding("DQ013", f"Row could not be parsed: {exc}")))
            break  # csv state is unreliable after an error
        start_line = last_line + 1
        last_line = reader.line_num
        if not any(cell.strip() for cell in record):
            continue  # fully blank line: not a record
        index += 1
        if index > max_rows:
            return _fatal(
                parsed, f"Too many rows: the limit is {max_rows} per file.", "Split the file into smaller batches."
            )
        if len(record) != len(columns):
            parsed.rows.append(
                RawRow(
                    index,
                    start_line,
                    {},
                    Finding(
                        "DQ013",
                        f"Expected {len(columns)} columns but found {len(record)}.",
                        suggestion="Quote fields that contain the delimiter or line breaks.",
                    ),
                )
            )
            continue
        values = {col: cell for col, cell in zip(columns, record, strict=True) if col}
        parsed.rows.append(RawRow(index, start_line, values))
    if index == 0 and not parsed.rows:
        parsed.file_findings.append(Finding("DQ019", "File has a header but no data rows."))
    return parsed


def json_depth_exceeds(text: str, limit: int) -> bool:
    """Iterative bracket-depth scan (string-aware) so deeply nested input never recurses."""
    depth = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _no_constants(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key '{k[:40]}'")
        out[k] = v
    return out


def flatten(obj: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    stack = [(prefix, obj)]
    while stack:
        pre, current = stack.pop()
        for key, value in current.items():
            path = f"{pre}{key}"
            if isinstance(value, dict):
                stack.append((f"{path}.", value))
            else:
                flat[path] = value
    return flat


def parse_json(data: bytes, *, encoding: str, max_rows: int) -> ParsedFile:
    parsed = ParsedFile(format="JSON", encoding=encoding)
    text, err = decode_bytes(data, encoding)
    if err or text is None:
        return _fatal(parsed, err or "Undecodable content.")
    if json_depth_exceeds(text, MAX_JSON_DEPTH):
        return _fatal(parsed, f"JSON is nested deeper than {MAX_JSON_DEPTH} levels.")
    try:
        doc = json.loads(text, parse_constant=_no_constants, object_pairs_hook=_no_dupes)
    except (ValueError, RecursionError) as exc:
        return _fatal(parsed, f"Invalid JSON: {str(exc)[:120]}")
    if isinstance(doc, dict) and isinstance(doc.get("records"), list) and len(doc) == 1:
        records: list[Any] = doc["records"]
    elif isinstance(doc, dict):
        records = [doc]
    elif isinstance(doc, list):
        records = doc
    else:
        return _fatal(parsed, 'Top-level JSON must be an object, an array of objects, or {"records": [...]}.')
    if not records:
        return _fatal(parsed, "No records found.")
    if len(records) > max_rows:
        return _fatal(parsed, f"Too many records: the limit is {max_rows} per file.", "Split into smaller batches.")
    seen: dict[str, None] = {}
    for i, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            parsed.rows.append(RawRow(i, None, {}, Finding("DQ013", "Record is not a JSON object.")))
            continue
        flat = flatten(rec)
        for key in flat:
            seen.setdefault(key, None)
        parsed.rows.append(RawRow(i, None, flat))
    parsed.columns = list(seen)
    return parsed
