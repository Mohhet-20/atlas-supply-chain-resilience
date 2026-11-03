"""CSV export with spreadsheet formula-injection neutralization."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

from app.ingestion.constants import FORMULA_PREFIXES


def neutralize(value: Any) -> str:
    """Prefix risky leading characters so Excel/Sheets treat the cell as text, never a formula."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([neutralize(h) for h in header])
    for row in rows:
        writer.writerow([neutralize(c) for c in row])
    return buf.getvalue()
