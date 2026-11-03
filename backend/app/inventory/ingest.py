"""Inventory, demand and replenishment imports (preview and commit).

Reuses the ingestion byte-level parsers and limits (CSV/JSON, size, depth, columns). References resolve to the canonical
registry by code and are never created here. Invalid rows are reported with codes and kept for inspection; identical
re-imports are detected by file fingerprint and by record key, so nothing is counted twice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError
from app.ingestion.constants import ALLOWED_DATE_FORMATS
from app.ingestion.normalize import clean_text, parse_date, parse_number
from app.ingestion.parsers import detect_encoding, parse_csv, parse_json
from app.inventory.calc import norm_unit
from app.inventory.config import MAX_ROW_ERRORS_RETURNED
from app.models.enums import EntityStatus, Provenance
from app.models.facility import Facility
from app.models.inventory import (
    DemandKind,
    DemandObservation,
    ImportKind,
    ImportStatus,
    InventoryImportBatch,
    InventoryImportRow,
    InventoryPosition,
    InventoryReplenishment,
    InvRowOutcome,
    ReceiptStatus,
)  # fmt: skip
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.types import utcnow

REQUIRED: dict[ImportKind, tuple[str, ...]] = {
    ImportKind.INVENTORY: ("product_code", "facility_code", "observed_at", "unit", "on_hand"),
    ImportKind.DEMAND: ("product_code", "facility_code", "kind", "period_start", "period_end", "quantity", "unit"),
    ImportKind.REPLENISHMENT: ("product_code", "facility_code", "reference", "status", "quantity", "unit"),
}
OPTIONAL: dict[ImportKind, tuple[str, ...]] = {
    ImportKind.INVENTORY: ("reserved", "allocated", "quarantined", "damaged", "usable_source", "atp_source", "in_transit", "backordered", "source_record_id"),
    ImportKind.DEMAND: ("source_record_id",),
    ImportKind.REPLENISHMENT: ("expected_date", "supplier_code", "observed_at"),
}  # fmt: skip
INV_QTY = (
    "on_hand",
    "reserved",
    "allocated",
    "quarantined",
    "damaged",
    "usable_source",
    "atp_source",
    "in_transit",
    "backordered",
)
MAX_QTY = 1e12
MAX_PERIOD_DAYS = 366
FUTURE_SLACK = timedelta(days=1)


def fields_of(kind: ImportKind) -> list[str]:
    return [*REQUIRED[kind], *OPTIONAL[kind]]


def _err(code: str, field: str | None, msg: str) -> dict[str, Any]:
    return {"code": code, "field": field, "message": msg}


def parse_ts(text: str) -> tuple[datetime, bool]:
    """ISO 8601 date or date-time -> UTC. A value with no offset is read as UTC and flagged."""
    t = text.strip().replace("Z", "+00:00").replace("z", "+00:00")
    d = datetime.fromisoformat(t if "T" in t or " " in t else t + "T00:00:00")
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc), True
    return d.astimezone(timezone.utc), False


def default_mapping(kind: ImportKind, columns: list[str]) -> dict[str, str]:
    low = {c.strip().lower(): c for c in columns}
    aliases = {"usable_source": ("usable", "usable_qty"), "atp_source": ("atp", "available_to_promise"), "source_record_id": ("record_id", "source_id"), "unit": ("uom", "unit_of_measure"), "on_hand": ("onhand", "quantity_on_hand"), "reference": ("po_number", "order_ref", "shipment_ref"), "expected_date": ("eta", "expected_receipt_date")}  # fmt: skip
    out: dict[str, str] = {}
    for f in fields_of(kind):
        for cand in (f, *aliases.get(f, ())):
            if cand in low:
                out[f] = low[cand]
                break
    return out


def _fingerprint(kind: ImportKind, mapping: dict[str, str], date_format: str, data: bytes) -> str:
    h = hashlib.sha256()
    h.update(f"{kind.value}|{json.dumps(mapping, sort_keys=True)}|{date_format}|".encode())
    h.update(data)
    return h.hexdigest()


def _hash(rec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _chunks(seq: list[str], n: int = 400):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _resolve(session: Session, model: Any, col: Any, codes: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for part in _chunks(sorted(codes)):
        for row in session.scalars(select(model).where(col.in_(part))):
            found[getattr(row, col.key)] = row
    return found


class Prepared:
    """Result of validating a file, before anything is written."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []  # {n, outcome, errors, warnings, raw, record, key}
        self.columns: list[str] = []
        self.mapping: dict[str, str] = {}
        self.file_findings: list[str] = []


def prepare(
    session: Session, kind: ImportKind, data: bytes, *, fmt: str, encoding: str | None, delimiter: str,
    mapping: dict[str, str] | None, date_format: str, max_rows: int, now: datetime,
) -> Prepared:  # fmt: skip
    if date_format not in ALLOWED_DATE_FORMATS:
        raise DomainValidationError(f"date_format must be one of {', '.join(ALLOWED_DATE_FORMATS)}.")
    enc = encoding or detect_encoding(data)
    parsed = (
        parse_csv(data, encoding=enc, delimiter=delimiter, max_rows=max_rows)
        if fmt == "CSV"
        else parse_json(data, encoding=enc, max_rows=max_rows)
    )
    if parsed.fatal:
        raise DomainValidationError(
            "; ".join(f.message for f in parsed.file_findings if f.blocking) or "The file could not be read."
        )
    if len(parsed.rows) > max_rows:
        raise DomainValidationError(f"Too many rows: the limit is {max_rows} per file. Split the file.")
    prep = Prepared()
    prep.columns = parsed.columns
    m = dict(mapping) if mapping else default_mapping(kind, parsed.columns)
    bad = [f"{k} -> {v}" for k, v in m.items() if k not in fields_of(kind) or v not in parsed.columns]
    if bad:
        raise DomainValidationError(f"Mapping refers to unknown fields or columns: {', '.join(bad)}.")
    missing = [f for f in REQUIRED[kind] if f not in m]
    if missing:
        raise DomainValidationError(f"Required fields have no mapped column: {', '.join(missing)}.")
    prep.mapping = m

    def cell(raw: dict[str, Any], f: str) -> str | None:
        return clean_text(raw.get(m[f])) if f in m else None

    cleaned = [(r, {f: cell(r.values, f) for f in fields_of(kind)}) for r in parsed.rows]
    products = _resolve(
        session, Product, Product.product_code, {v["product_code"] for _, v in cleaned if v["product_code"]}
    )
    facilities = _resolve(
        session, Facility, Facility.facility_code, {v["facility_code"] for _, v in cleaned if v["facility_code"]}
    )
    suppliers = _resolve(session, Supplier, Supplier.supplier_code, {v.get("supplier_code") for _, v in cleaned if v.get("supplier_code")}) if kind is ImportKind.REPLENISHMENT else {}  # fmt: skip

    seen: dict[Any, tuple[str, int]] = {}
    for raw, v in cleaned:
        errors: list[dict[str, Any]] = []
        warns: list[dict[str, Any]] = []
        rec: dict[str, Any] = {}
        if raw.error is not None:
            errors.append(_err("UNREADABLE_ROW", None, raw.error.message))
        else:
            _validate(kind, v, rec, errors, warns, products, facilities, suppliers, date_format, now)
        key = None
        if not errors:
            skip = {"source_record_id"} | ({"observed_at"} if kind is ImportKind.REPLENISHMENT else set())
            rec["row_hash"] = _hash({k: x for k, x in rec.items() if k not in skip})
            h = rec["row_hash"]
            key = _key(kind, rec)
            if key in seen:
                prior_hash, prior_row = seen[key]
                if prior_hash == h:
                    prep.rows.append({"n": raw.index, "outcome": InvRowOutcome.DUPLICATE, "errors": [], "warnings": warns + [_err("DUPLICATE_IN_FILE", None, f"Identical to row {prior_row}; counted once.")], "raw": raw.values, "record": None, "key": key})  # fmt: skip
                    continue
                errors.append(
                    _err(
                        "CONFLICTING_RECORD",
                        None,
                        f"Same record key as row {prior_row} but different values; neither is trusted over the other.",
                    )
                )
            else:
                seen[key] = (h, raw.index)
        prep.rows.append({"n": raw.index, "outcome": InvRowOutcome.REJECTED if errors else InvRowOutcome.ACCEPTED, "errors": errors, "warnings": warns, "raw": raw.values, "record": None if errors else rec, "key": key})  # fmt: skip
    _check_existing(session, kind, prep)
    return prep


def _key(kind: ImportKind, r: dict[str, Any]) -> tuple:
    if kind is ImportKind.INVENTORY:
        return (r["product_id"], r["facility_id"], r["observed_at"])
    if kind is ImportKind.DEMAND:
        return (r["product_id"], r["facility_id"], r["kind"], r["period_start"], r["period_end"])
    return (r["product_id"], r["facility_id"], r["reference"], r["row_hash"])


def _validate(kind, v, rec, errors, warns, products, facilities, suppliers, date_format, now) -> None:  # noqa: ANN001, C901
    for f in REQUIRED[kind]:
        if v.get(f) is None:
            errors.append(_err("MISSING_REQUIRED", f, f"{f} is required."))
    if errors:
        return
    prod, fac = products.get(v["product_code"]), facilities.get(v["facility_code"])
    if prod is None:
        errors.append(
            _err(
                "UNRESOLVED_PRODUCT",
                "product_code",
                f"No product with code '{v['product_code'][:60]}' in the registry; none is created.",
            )
        )
    elif prod.status is not EntityStatus.ACTIVE:
        errors.append(_err("INACTIVE_PRODUCT", "product_code", "The product is not active."))
    if fac is None:
        errors.append(
            _err(
                "UNRESOLVED_FACILITY",
                "facility_code",
                f"No facility with code '{v['facility_code'][:60]}' in the registry; none is created.",
            )
        )
    elif fac.status is not EntityStatus.ACTIVE:
        errors.append(_err("INACTIVE_FACILITY", "facility_code", "The facility is not active."))
    unit = v["unit"]
    if len(unit) > 32:
        errors.append(_err("INVALID_UNIT", "unit", "unit is longer than 32 characters."))
    elif prod is not None and prod.unit_of_measure and norm_unit(prod.unit_of_measure) != norm_unit(unit):
        errors.append(
            _err(
                "UNIT_MISMATCH",
                "unit",
                f"Unit '{unit}' differs from the product's registered unit '{prod.unit_of_measure}'. No conversion is applied.",
            )
        )
    if errors:
        return
    rec.update(product_id=prod.id, facility_id=fac.id, unit=unit)

    def num(f: str, *, low: float | None = 0.0, positive: bool = False) -> float | None:
        if v.get(f) is None:
            return None
        try:
            x = parse_number(v[f])
        except ValueError:
            errors.append(_err("INVALID_NUMBER", f, f"{f} must be a plain decimal number."))
            return None
        if abs(x) > MAX_QTY:
            errors.append(_err("OUT_OF_RANGE", f, f"{f} is outside the supported range."))
        elif positive and x <= 0:
            errors.append(_err("NON_POSITIVE_QUANTITY", f, f"{f} must be greater than zero."))
        elif low is not None and x < low:
            errors.append(
                _err(
                    "NEGATIVE_QUANTITY", f, f"{f} cannot be negative. Negative balances are not accepted as positions."
                )
            )
        return x

    if kind is ImportKind.INVENTORY:
        try:
            ts, assumed = parse_ts(v["observed_at"])
        except ValueError:
            errors.append(
                _err("INVALID_TIMESTAMP", "observed_at", "observed_at must be an ISO 8601 date or date-time.")
            )
            return
        if ts > now + FUTURE_SLACK:
            errors.append(_err("FUTURE_DATED", "observed_at", "An observation cannot be dated in the future."))
        if assumed:
            warns.append(_err("TIMESTAMP_ASSUMED_UTC", "observed_at", "No time zone given; read as UTC."))
        for f in INV_QTY:
            rec[f] = num(f)
        rec.update(observed_at=ts, source_record_id=v.get("source_record_id"))
        if errors:
            return
        excl = sum(rec[f] or 0 for f in ("reserved", "quarantined", "damaged"))
        if excl > rec["on_hand"] + 1e-9:
            errors.append(
                _err(
                    "EXCLUSIONS_EXCEED_ON_HAND",
                    "reserved",
                    f"Reserved + quarantined + damaged ({excl:g}) exceed on-hand ({rec['on_hand']:g}); the categories may overlap or the data is wrong.",
                )
            )
        if rec["allocated"] is not None and rec["allocated"] > rec["on_hand"] + 1e-9:
            warns.append(
                _err(
                    "OVER_ALLOCATED",
                    "allocated",
                    "Allocated quantity exceeds on-hand; kept, but available inventory will show as over-committed.",
                )
            )
        if rec["usable_source"] is not None and rec["usable_source"] > rec["on_hand"] + 1e-9:
            errors.append(_err("USABLE_EXCEEDS_ON_HAND", "usable_source", "Source usable quantity exceeds on-hand."))
        if rec["in_transit"] is not None:
            warns.append(
                _err(
                    "IN_TRANSIT_SEPARATE",
                    "in_transit",
                    "In-transit is stored apart from on-hand and is never added to it.",
                )
            )
    elif kind is ImportKind.DEMAND:
        try:
            dk = DemandKind(v["kind"].upper())
        except ValueError:
            errors.append(
                _err("INVALID_KIND", "kind", f"kind must be one of {', '.join(k.value for k in DemandKind)}.")
            )
            return
        try:
            ps, pe = parse_date(v["period_start"], date_format), parse_date(v["period_end"], date_format)
        except ValueError:
            errors.append(
                _err("INVALID_DATE", "period_start", f"period_start and period_end must match {date_format}.")
            )
            return
        if pe <= ps:
            errors.append(_err("INVALID_PERIOD", "period_end", "period_end (exclusive) must be after period_start."))
        elif (pe - ps).days > MAX_PERIOD_DAYS:
            errors.append(
                _err("PERIOD_TOO_LONG", "period_end", f"A single observation may span at most {MAX_PERIOD_DAYS} days.")
            )
        elif pe > now.date() + timedelta(days=1):
            errors.append(
                _err(
                    "FORECAST_NOT_OBSERVATION",
                    "period_end",
                    "The period ends in the future. Observed demand and forecasts are kept separate; this file type accepts observations only.",
                )
            )
        q = num("quantity", low=None if dk is DemandKind.ADJUSTMENT else 0.0)
        rec.update(
            kind=dk.value, period_start=ps, period_end=pe, quantity=q, source_record_id=v.get("source_record_id")
        )
    else:
        try:
            st = ReceiptStatus(v["status"].upper().replace(" ", "_"))
        except ValueError:
            errors.append(
                _err("INVALID_STATUS", "status", f"status must be one of {', '.join(s.value for s in ReceiptStatus)}.")
            )
            return
        q = num("quantity", positive=True)
        exp = None
        if v.get("expected_date"):
            try:
                exp = parse_date(v["expected_date"], date_format)
            except ValueError:
                errors.append(_err("INVALID_DATE", "expected_date", f"expected_date must match {date_format}."))
        else:
            warns.append(
                _err(
                    "NO_EXPECTED_DATE",
                    "expected_date",
                    "Without an expected date this receipt cannot be used in a projection.",
                )
            )
        sup = None
        if v.get("supplier_code"):
            sup = suppliers.get(v["supplier_code"])
            if sup is None:
                errors.append(
                    _err(
                        "UNRESOLVED_SUPPLIER",
                        "supplier_code",
                        f"No supplier with code '{v['supplier_code'][:60]}'; none is created.",
                    )
                )
        obs = now
        if v.get("observed_at"):
            try:
                obs, _a = parse_ts(v["observed_at"])
            except ValueError:
                errors.append(
                    _err("INVALID_TIMESTAMP", "observed_at", "observed_at must be an ISO 8601 date or date-time.")
                )
        else:
            warns.append(_err("OBSERVED_AT_DEFAULTED", "observed_at", "No observed_at; the import time is recorded."))
        if st is ReceiptStatus.CONFIRMED and exp is None:
            warns.append(
                _err(
                    "CONFIRMED_WITHOUT_DATE",
                    "expected_date",
                    "A confirmed receipt without a date is not counted in projections.",
                )
            )
        rec.update(
            reference=v["reference"][:128],
            status=st.value,
            quantity=q,
            expected_date=exp,
            supplier_id=sup.id if sup else None,
            observed_at=obs,
        )


def _check_existing(session: Session, kind: ImportKind, prep: Prepared) -> None:
    """Mark rows that already exist (identical -> DUPLICATE, same key with other values -> REJECTED)."""
    live = [r for r in prep.rows if r["outcome"] is InvRowOutcome.ACCEPTED]
    if not live:
        return
    pids = {r["record"]["product_id"] for r in live}
    fids = {r["record"]["facility_id"] for r in live}
    if kind is ImportKind.INVENTORY:
        have = {(p.product_id, p.facility_id, _naive(p.observed_at)): p.row_hash for p in session.scalars(select(InventoryPosition).where(InventoryPosition.product_id.in_(pids), InventoryPosition.facility_id.in_(fids)))}  # fmt: skip
        norm = lambda r: (r["product_id"], r["facility_id"], _naive(r["observed_at"]))  # noqa: E731
    elif kind is ImportKind.DEMAND:
        have = {(d.product_id, d.facility_id, d.kind.value, d.period_start, d.period_end): d.row_hash for d in session.scalars(select(DemandObservation).where(DemandObservation.product_id.in_(pids), DemandObservation.facility_id.in_(fids)))}  # fmt: skip
        norm = lambda r: (r["product_id"], r["facility_id"], r["kind"], r["period_start"], r["period_end"])  # noqa: E731
    else:
        have = {(x.product_id, x.facility_id, x.reference, x.row_hash): x.row_hash for x in session.scalars(select(InventoryReplenishment).where(InventoryReplenishment.product_id.in_(pids), InventoryReplenishment.facility_id.in_(fids)))}  # fmt: skip
        norm = lambda r: (r["product_id"], r["facility_id"], r["reference"], r["row_hash"])  # noqa: E731
    for r in live:
        k = norm(r["record"])
        if k in have:
            if have[k] == r["record"]["row_hash"]:
                r["outcome"], r["record"] = InvRowOutcome.DUPLICATE, None
                r["warnings"].append(
                    _err("ALREADY_IMPORTED", None, "An identical record already exists; it is not added again.")
                )
            else:
                r["outcome"], r["record"] = InvRowOutcome.REJECTED, None
                r["errors"].append(
                    _err(
                        "CONFLICTING_SNAPSHOT",
                        None,
                        "A different record already exists for the same product, facility and time/period. Existing records are never overwritten.",
                    )
                )


def _naive(d: datetime) -> datetime:
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def summarize(prep: Prepared, kind: ImportKind) -> dict[str, Any]:
    c = Counter(r["outcome"].value for r in prep.rows)
    codes = Counter(e["code"] for r in prep.rows for e in r["errors"])
    warn = Counter(w["code"] for r in prep.rows for w in r["warnings"])
    return {
        "kind": kind.value, "rows_total": len(prep.rows), "rows_accepted": c.get("ACCEPTED", 0), "rows_duplicate": c.get("DUPLICATE", 0),
        "rows_rejected": c.get("REJECTED", 0), "error_codes": dict(codes), "warning_codes": dict(warn), "columns": prep.columns, "mapping": prep.mapping,
    }  # fmt: skip


def row_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "row_number": r["n"],
        "outcome": r["outcome"].value,
        "errors": r["errors"],
        "warnings": r["warnings"],
        "raw": _cap(r["raw"]),
    }


def _cap(raw: Any) -> Any:
    s = json.dumps(raw, default=str)
    return raw if len(s) <= 2000 else {"truncated": s[:2000]}


def preview(session: Session, kind: ImportKind, data: bytes, **kw: Any) -> dict[str, Any]:
    now = utcnow()
    prep = prepare(session, kind, data, now=now, **kw)
    s = summarize(prep, kind)
    return {"mode": "preview", "committed": False, "summary": s, "rows": [row_view(r) for r in prep.rows[:MAX_ROW_ERRORS_RETURNED] if r["outcome"] is not InvRowOutcome.ACCEPTED or r["warnings"]][:MAX_ROW_ERRORS_RETURNED], "sample_accepted": [row_view(r) for r in prep.rows if r["outcome"] is InvRowOutcome.ACCEPTED][:5], "would_write": s["rows_accepted"]}  # fmt: skip


def commit(
    session: Session, kind: ImportKind, data: bytes, *, source_label: str, provenance: Provenance, requested_by: str,
    file_name: str | None, atomic: bool = False, **kw: Any,
) -> tuple[InventoryImportBatch, bool, Prepared | None]:  # fmt: skip
    """Persist one batch. Returns (batch, created, prepared). A byte-identical earlier import is returned unchanged."""
    fp = _fingerprint(kind, kw.get("mapping") or {}, kw["date_format"], data)
    prior = session.scalar(
        select(InventoryImportBatch).where(InventoryImportBatch.kind == kind, InventoryImportBatch.fingerprint == fp)
    )
    if prior is not None:
        return prior, False, None
    now = utcnow()
    prep = prepare(session, kind, data, now=now, **kw)
    s = summarize(prep, kind)
    write = s["rows_accepted"] > 0 and not (atomic and s["rows_rejected"] > 0)
    status = ImportStatus.COMPLETED if write or (s["rows_rejected"] == 0) else ImportStatus.REJECTED
    batch = InventoryImportBatch(
        batch_uid=f"IB-{uuid.uuid4().hex[:16].upper()}", kind=kind,
        fingerprint=fp if status is ImportStatus.COMPLETED else hashlib.sha256((fp + uuid.uuid4().hex).encode()).hexdigest(),
        status=status, source_label=source_label[:200], provenance=provenance, file_name=(file_name or None) and file_name[:255],
        file_format=kw["fmt"], requested_by=requested_by, mapping=prep.mapping, rows_total=s["rows_total"],
        rows_accepted=s["rows_accepted"] if write else 0, rows_duplicate=s["rows_duplicate"], rows_rejected=s["rows_rejected"],
        summary={**s, "atomic": atomic, "written": write}, created_at=now,
    )  # fmt: skip
    session.add(batch)
    session.flush()
    for r in prep.rows:
        out = r["outcome"]
        rid = None
        if write and out is InvRowOutcome.ACCEPTED:
            rid = _insert(session, kind, batch, r["record"], provenance, source_label, now)
        elif out is InvRowOutcome.ACCEPTED:  # an atomic batch with rejects writes nothing
            out = InvRowOutcome.REJECTED
            r["errors"].append(
                _err("BATCH_NOT_APPLIED", None, "Atomic import: another row failed, so no row was written.")
            )
        session.add(
            InventoryImportRow(
                batch_id=batch.id,
                row_number=r["n"],
                outcome=out,
                errors=r["errors"] + r["warnings"] or None,
                raw=_cap(r["raw"]),
                record_id=rid,
            )
        )
    session.flush()
    return batch, True, prep


def _insert(
    session: Session,
    kind: ImportKind,
    b: InventoryImportBatch,
    rec: dict[str, Any],
    prov: Provenance,
    label: str,
    now: datetime,
) -> int:
    common = dict(batch_id=b.id, provenance=prov, source_label=label[:200], row_hash=rec["row_hash"])
    if kind is ImportKind.INVENTORY:
        obj: Any = InventoryPosition(
            **common,
            **{k: rec[k] for k in ("product_id", "facility_id", "observed_at", "unit", *INV_QTY, "source_record_id")},
        )
    elif kind is ImportKind.DEMAND:
        obj = DemandObservation(**common, kind=DemandKind(rec["kind"]), **{k: rec[k] for k in ("product_id", "facility_id", "period_start", "period_end", "quantity", "unit", "source_record_id")})  # fmt: skip
    else:
        obj = InventoryReplenishment(**common, status=ReceiptStatus(rec["status"]), **{k: rec[k] for k in ("product_id", "facility_id", "supplier_id", "reference", "quantity", "unit", "expected_date", "observed_at")})  # fmt: skip
    session.add(obj)
    session.flush()
    return obj.id


__all__ = ["ImportKind", "commit", "default_mapping", "fields_of", "preview", "prepare"]
