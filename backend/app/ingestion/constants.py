from __future__ import annotations

import re

NORMALIZATION_VERSION = "1"
CANONICAL_PROFILE_REF = "canonical:1"

EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/#]{0,63}\Z")
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}\Z")

MAX_JSON_DEPTH = 32
MAX_FIELD_CHARS = 2_000  # per cell/value before any field-specific limit
MAX_COLUMNS = 100
MAX_FINDINGS_PERSISTED = 5_000
PREVIEW_ROWS_RETURNED = 200
PREVIEW_FINDINGS_RETURNED = 200

ALLOWED_ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "latin-1", "cp1252")
ALLOWED_DELIMITERS = {",": ",", ";": ";", "tab": "\t", "pipe": "|"}
ALLOWED_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d")

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
