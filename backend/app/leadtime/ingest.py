"""Delivery snapshot and event imports (preview and commit).

Reuses the ingestion byte-level parsers and limits (CSV/JSON, size, depth, columns). Suppliers, facilities and products
resolve to the canonical registry by code and are never created here. Invalid rows are reported with codes and kept for
inspection; identical re-imports are detected by file fingerprint and by record hash, so nothing is counted twice.
Nothing is coerced: contradictory chronology, a changed original promise, a changed supplier/destination for the same
order line, and cancelled-with-arrival are rejected with a reason (unless the row carries a documented exception note
for a chronology problem, in which case it is kept, flagged, and excluded from analysis by default).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError
from app.ingestion.constants import ALLOWED_DATE_FORMATS
from app.ingestion.normalize import clean_text, parse_number
from app.ingestion.parsers import detect_encoding, parse_csv, parse_json
from app.leadtime.config import EVENT_TYPES
from app.leadtime.semantics import Delivery, chronology_violations, parse_ts
from app.models.delivery import (
    DeliveryEvent,
    DeliveryImportBatch,
    DeliveryImportKind,
    DeliveryImportRow,
    DeliveryImportStatus,
    DeliveryRowOutcome,
    DeliverySnapshot,
)  # fmt: skip
from app.models.enums import EntityStatus, Provenance
from app.models.facility import Facility
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.types import utcnow

K = DeliveryImportKind
STATUSES = ("OPEN", "CONFIRMED", "IN_TRANSIT", "DELIVERED", "PARTIALLY_RECEIVED", "CANCELLED", "UNKNOWN")
TS_FIELDS = ("order_at", "confirmed_at", "promised_dispatch_at", "dispatched_at", "original_promised_at", "revised_promised_at", "arrived_at", "received_at", "cancelled_at", "observed_at")  # fmt: skip
ACTUAL_FIELDS = (
    "order_at",
    "confirmed_at",
    "dispatched_at",
    "arrived_at",
    "received_at",
    "cancelled_at",
    "observed_at",
    "occurred_at",
)  # cannot be in the future
REQUIRED = {
    K.DELIVERIES: ("delivery_ref", "product_code", "destination_facility_code", "supplier_code"),
    K.EVENTS: ("delivery_ref", "event_type", "occurred_at"),
}
OPTIONAL = {
    K.DELIVERIES: ("origin_facility_code", "quantity", "unit", "status", "received_quantity", "transport_mode", "exception_note", *TS_FIELDS),
    K.EVENTS: ("product_code", "value_at", "quantity", "source_event_id"),
}  # fmt: skip
ALIASES = {
    "delivery_ref": ("po_number", "order_ref", "shipment_ref", "order_id", "reference"),
    "destination_facility_code": ("facility_code", "destination", "ship_to"),
    "supplier_code": ("vendor_code", "supplier"),
    "order_at": ("order_date", "ordered_at"),
    "original_promised_at": ("promised_at", "promise_date", "promised_delivery", "original_promise"),
    "revised_promised_at": ("revised_promise", "revised_promise_date"),
    "dispatched_at": ("ship_date", "shipped_at", "dispatch_date"),
    "arrived_at": ("arrival_date", "delivered_at"),
    "received_at": ("receipt_date", "received_date"),
    "transport_mode": ("mode", "carrier_mode"),
    "event_type": ("event",),
    "occurred_at": ("event_at", "timestamp"),
    "quantity": ("qty", "ordered_quantity"),
    "unit": ("uom", "unit_of_measure"),
}  # fmt: skip
MAX_QTY = 1e12
FUTURE_SLACK = timedelta(days=1)
MAX_ROW_ERRORS_RETURNED = 200


def fields_of(kind: K) -> list[str]:
    return [*REQUIRED[kind], *OPTIONAL[kind]]


def _err(code: str, field: str | None, msg: str) -> dict[str, Any]:
    return {"code": code, "field": field, "message": msg}


def default_mapping(kind: K, columns: list[str]) -> dict[str, str]:
    low = {c.strip().lower(): c for c in columns}
    out: dict[str, str] = {}
    for f in fields_of(kind):
        for cand in (f, *ALIASES.get(f, ())):
            if cand in low:
                out[f] = low[cand]
                break
    return out


def _hash(rec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _fingerprint(kind: K, mapping: dict[str, str], tz: str, data: bytes) -> str:
    h = hashlib.sha256()
    h.update(f"{kind.value}|{json.dumps(mapping, sort_keys=True)}|{tz}|".encode())
    h.update(data)
    return h.hexdigest()


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
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.columns: list[str] = []
        self.mapping: dict[str, str] = {}


def prepare(
    session: Session, kind: K, data: bytes, *, fmt: str, encoding: str | None, delimiter: str, mapping: dict[str, str] | None,
    time_zone: str, date_format: str, max_rows: int, now: datetime, chronology_tolerance_hours: float = 0.0,
) -> Prepared:  # fmt: skip
    if date_format not in ALLOWED_DATE_FORMATS:
        raise DomainValidationError(f"date_format must be one of {', '.join(ALLOWED_DATE_FORMATS)}.")
    try:
        ZoneInfo(time_zone)
    except Exception as exc:  # noqa: BLE001
        raise DomainValidationError(
            f"Unknown time zone '{time_zone}'. Use an IANA name such as 'Europe/Berlin' or 'UTC'."
        ) from exc
    enc = encoding or detect_encoding(data)
    parsed = parse_csv(data, encoding=enc, delimiter=delimiter, max_rows=max_rows) if fmt == "CSV" else parse_json(data, encoding=enc, max_rows=max_rows)  # fmt: skip
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
    cleaned = [
        (r, {f: (clean_text(r.values.get(m[f])) if f in m else None) for f in fields_of(kind)}) for r in parsed.rows
    ]
    products = _resolve(
        session, Product, Product.product_code, {v["product_code"] for _, v in cleaned if v.get("product_code")}
    )
    fac_codes = {v[k] for _, v in cleaned for k in ("destination_facility_code", "origin_facility_code") if v.get(k)}
    facilities = _resolve(session, Facility, Facility.facility_code, fac_codes)
    suppliers = _resolve(session, Supplier, Supplier.supplier_code, {v["supplier_code"] for _, v in cleaned if v.get("supplier_code")}) if kind is K.DELIVERIES else {}  # fmt: skip
    seen: dict[Any, tuple[str, int]] = {}
    ident: dict[tuple[str, int], tuple[Any, Any, int]] = {}
    orig: dict[tuple[str, int], tuple[datetime, int]] = {}
    for raw, v in cleaned:
        errors: list[dict[str, Any]] = []
        warns: list[dict[str, Any]] = []
        rec: dict[str, Any] = {}
        if raw.error is not None:
            errors.append(_err("UNREADABLE_ROW", None, raw.error.message))
        elif kind is K.DELIVERIES:
            _validate_delivery(
                v, rec, errors, warns, products, facilities, suppliers, time_zone, now, chronology_tolerance_hours
            )
        else:
            _validate_event(v, rec, errors, warns, products, time_zone, now)
        key = None
        if not errors:
            body = {k: x for k, x in rec.items() if not (k == "observed_at" and rec.get("observed_at_defaulted"))}
            rec["row_hash"] = _hash(body)
            h = rec["row_hash"]
            if kind is K.DELIVERIES:
                ik = (rec["delivery_ref"], rec["product_id"])
                key = (*ik, rec["observed_at"] if not rec["observed_at_defaulted"] else None)
                prior_id = ident.get(ik)
                if prior_id and prior_id[:2] != (rec["supplier_id"], rec["destination_facility_id"]):
                    errors.append(_err("IDENTITY_CONFLICT", "supplier_code", f"Row {prior_id[2]} gives the same order line a different supplier or destination; the row is not attached to either silently."))  # fmt: skip
                else:
                    ident.setdefault(ik, (rec["supplier_id"], rec["destination_facility_id"], raw.index))
                o = rec.get("original_promised_at")
                if o is not None and not errors:
                    po = orig.get(ik)
                    if po and po[0] != o:
                        errors.append(_err("ORIGINAL_PROMISE_CHANGED", "original_promised_at", f"Row {po[1]} already gives a different original promise for this order line. A new commitment belongs in revised_promised_at."))  # fmt: skip
                    else:
                        orig.setdefault(ik, (o, raw.index))
            else:
                key = (
                    rec["delivery_ref"],
                    rec.get("product_id"),
                    rec["event_type"],
                    rec["occurred_at"],
                    rec.get("value_at"),
                    rec.get("quantity"),
                )
            if not errors and key in seen:
                prior_hash, prior_row = seen[key]
                if prior_hash == h:
                    prep.rows.append({"n": raw.index, "outcome": DeliveryRowOutcome.DUPLICATE, "errors": [], "warnings": warns + [_err("DUPLICATE_IN_FILE", None, f"Identical to row {prior_row}; counted once.")], "raw": raw.values, "record": None, "key": key})  # fmt: skip
                    continue
                errors.append(_err("CONFLICTING_RECORD", None, f"Same record key as row {prior_row} but different values; neither is trusted over the other."))  # fmt: skip
            elif not errors:
                seen[key] = (h, raw.index)
        prep.rows.append({"n": raw.index, "outcome": DeliveryRowOutcome.REJECTED if errors else DeliveryRowOutcome.ACCEPTED, "errors": errors, "warnings": warns, "raw": raw.values, "record": None if errors else rec, "key": key})  # fmt: skip
    _check_existing(session, kind, prep)
    return prep


def _ts(
    v: dict[str, Any], f: str, tz: str, errors: list, warns: list, now: datetime, tol: float = 0.0
) -> datetime | None:  # noqa: ARG001
    if v.get(f) is None:
        return None
    try:
        ts, assumed, date_only = parse_ts(v[f], tz)
    except (ValueError, OverflowError):
        errors.append(_err("INVALID_TIMESTAMP", f, f"{f} must be an ISO 8601 date or date-time."))
        return None
    if ts.year < 1990 or ts.year > 2100:
        errors.append(_err("OUT_OF_RANGE", f, f"{f} is outside the supported years."))
        return None
    if f in ACTUAL_FIELDS and ts > now + FUTURE_SLACK:
        errors.append(_err("FUTURE_DATED", f, f"{f} records something that happened; it cannot be in the future."))
        return None
    if assumed:
        warns.append(_err("TIMESTAMP_ASSUMED_TZ", f, f"No time zone given; read as {tz}."))
    if date_only:
        warns.append(_err("DATE_ONLY_PRECISION", f, f"{f} has date precision only (midnight {tz})."))
    return ts


def _validate_delivery(v, rec, errors, warns, products, facilities, suppliers, tz, now, tol) -> None:  # noqa: ANN001, C901
    for f in REQUIRED[K.DELIVERIES]:
        if v.get(f) is None:
            errors.append(_err("MISSING_REQUIRED", f, f"{f} is required."))
    if errors:
        return
    if len(v["delivery_ref"]) > 128:
        errors.append(_err("INVALID_REFERENCE", "delivery_ref", "delivery_ref is longer than 128 characters."))
    prod, dest, sup = (
        products.get(v["product_code"]),
        facilities.get(v["destination_facility_code"]),
        suppliers.get(v["supplier_code"]),
    )
    for obj, code, field, what in ((prod, "PRODUCT", "product_code", "product"), (dest, "FACILITY", "destination_facility_code", "facility"), (sup, "SUPPLIER", "supplier_code", "supplier")):  # fmt: skip
        if obj is None:
            errors.append(
                _err(
                    f"UNRESOLVED_{code}",
                    field,
                    f"No {what} with code '{v[field][:60]}' in the registry; none is created.",
                )
            )
        elif obj.status is not EntityStatus.ACTIVE:
            errors.append(_err(f"INACTIVE_{code}", field, f"The {what} is not active."))
    org = None
    if v.get("origin_facility_code"):
        org = facilities.get(v["origin_facility_code"])
        if org is None:
            errors.append(
                _err(
                    "UNRESOLVED_FACILITY",
                    "origin_facility_code",
                    f"No facility with code '{v['origin_facility_code'][:60]}' in the registry.",
                )
            )
    if errors:
        return
    rec.update(delivery_ref=v["delivery_ref"], product_id=prod.id, supplier_id=sup.id, destination_facility_id=dest.id, origin_facility_id=org.id if org else None)  # fmt: skip
    for f in ("quantity", "received_quantity"):
        rec[f] = None
        if v.get(f) is not None:
            try:
                x = parse_number(v[f])
            except ValueError:
                errors.append(_err("INVALID_NUMBER", f, f"{f} must be a plain decimal number."))
                continue
            if abs(x) > MAX_QTY:
                errors.append(_err("OUT_OF_RANGE", f, f"{f} is outside the supported range."))
            elif f == "quantity" and x <= 0:
                errors.append(_err("NON_POSITIVE_QUANTITY", f, "quantity must be greater than zero."))
            elif f == "received_quantity" and x < 0:
                errors.append(_err("NEGATIVE_QUANTITY", f, "received_quantity cannot be negative."))
            else:
                rec[f] = x
    rec["unit"] = v.get("unit")
    if rec["unit"] and len(rec["unit"]) > 32:
        errors.append(_err("INVALID_UNIT", "unit", "unit is longer than 32 characters."))
    if rec["quantity"] is not None and not rec["unit"]:
        errors.append(_err("MISSING_UNIT", "unit", "unit is required when quantity is given."))
    if rec["unit"] and prod.unit_of_measure and rec["unit"].strip().upper() != prod.unit_of_measure.strip().upper():
        errors.append(_err("UNIT_MISMATCH", "unit", f"Unit '{rec['unit']}' differs from the product's registered unit '{prod.unit_of_measure}'. No conversion is applied."))  # fmt: skip
    st = v.get("status")
    if st is not None:
        st = st.strip().upper().replace(" ", "_")
        if st not in STATUSES:
            errors.append(_err("INVALID_STATUS", "status", f"status must be one of {', '.join(STATUSES)}."))
    rec["reported_status"] = st
    rec["transport_mode"] = v.get("transport_mode")[:32] if v.get("transport_mode") else None
    note = v.get("exception_note")
    rec["exception_note"] = note[:300] if note else None
    for f in TS_FIELDS:
        rec[f] = _ts(v, f, tz, errors, warns, now)
    rec["observed_at_defaulted"] = rec["observed_at"] is None
    if rec["observed_at"] is None:
        rec["observed_at"] = now
        warns.append(
            _err(
                "OBSERVED_AT_DEFAULTED",
                "observed_at",
                "No observed_at given; the import time is used as the time this state was reported.",
            )
        )
    if errors:
        return
    probe = Delivery(key="", ref="", product_id=0, supplier_id=None, destination_id=0, **{k: rec[k] for k in ("order_at", "confirmed_at", "dispatched_at", "arrived_at", "received_at")})  # fmt: skip
    bad = chronology_violations(probe, tol)
    if bad:
        if rec["exception_note"]:
            warns.append(_err("CHRONOLOGY_EXCEPTION", None, f"Contradictory event order ({', '.join(bad)}) kept because a documented exception was supplied; excluded from analysis by default."))  # fmt: skip
        else:
            errors.append(
                _err(
                    "CHRONOLOGY_VIOLATION",
                    None,
                    f"Contradictory event order: {', '.join(bad)}. Add exception_note for a documented correction.",
                )
            )
    if (rec["cancelled_at"] or st == "CANCELLED") and (rec["arrived_at"] or rec["received_at"]):
        errors.append(
            _err("CANCELLED_WITH_ARRIVAL", "status", "A cancelled order cannot also record an arrival or receipt.")
        )
    if st == "DELIVERED" and not (rec["arrived_at"] or rec["received_at"]):
        warns.append(
            _err(
                "DELIVERED_WITHOUT_TIMESTAMP",
                "status",
                "Status says delivered but no arrival/receipt time is recorded; no lead time can be computed.",
            )
        )
    if rec["revised_promised_at"] and not rec["original_promised_at"]:
        warns.append(
            _err(
                "REVISED_WITHOUT_ORIGINAL",
                "revised_promised_at",
                "A revised promise without an original promise: performance against the original commitment is unknown.",
            )
        )
    if rec["quantity"] and rec["received_quantity"] and rec["received_quantity"] > rec["quantity"] * 1.0001:
        warns.append(
            _err("OVER_RECEIPT", "received_quantity", "Received quantity exceeds ordered quantity; kept as reported.")
        )


def _validate_event(v, rec, errors, warns, products, tz, now) -> None:  # noqa: ANN001
    for f in REQUIRED[K.EVENTS]:
        if v.get(f) is None:
            errors.append(_err("MISSING_REQUIRED", f, f"{f} is required."))
    if errors:
        return
    et = v["event_type"].strip().upper().replace(" ", "_")
    if et not in EVENT_TYPES:
        errors.append(_err("INVALID_EVENT_TYPE", "event_type", f"event_type must be one of {', '.join(EVENT_TYPES)}."))
        return
    rec.update(delivery_ref=v["delivery_ref"][:128], event_type=et, product_id=None, quantity=None, value_at=None)
    if v.get("product_code"):
        p = products.get(v["product_code"])
        if p is None:
            errors.append(
                _err(
                    "UNRESOLVED_PRODUCT",
                    "product_code",
                    f"No product with code '{v['product_code'][:60]}' in the registry.",
                )
            )
        else:
            rec["product_id"] = p.id
    ts = _ts({"occurred_at": v["occurred_at"]}, "occurred_at", tz, errors, warns, now)
    rec["occurred_at"] = ts
    if et == "PROMISE_REVISED":
        if v.get("value_at") is None:
            errors.append(
                _err(
                    "MISSING_REQUIRED",
                    "value_at",
                    "value_at (the newly committed time) is required for PROMISE_REVISED.",
                )
            )
        else:
            rec["value_at"] = _ts_any(v["value_at"], tz, errors)
    if v.get("quantity") is not None:
        try:
            q = parse_number(v["quantity"])
            if q <= 0 or q > MAX_QTY:
                raise ValueError
            rec["quantity"] = q
        except ValueError:
            errors.append(_err("INVALID_NUMBER", "quantity", "quantity must be a positive plain decimal number."))
    if et == "PARTIAL_RECEIPT" and rec["quantity"] is None and not errors:
        errors.append(_err("MISSING_REQUIRED", "quantity", "quantity is required for PARTIAL_RECEIPT."))
    rec["source_event_id"] = (v.get("source_event_id") or "")[:128] or None


def _ts_any(text: str, tz: str, errors: list) -> datetime | None:
    try:
        return parse_ts(text, tz)[0]
    except (ValueError, OverflowError):
        errors.append(_err("INVALID_TIMESTAMP", "value_at", "value_at must be an ISO 8601 date or date-time."))
        return None


def _check_existing(session: Session, kind: K, prep: Prepared) -> None:
    live = [r for r in prep.rows if r["outcome"] is DeliveryRowOutcome.ACCEPTED]
    if not live:
        return
    refs = sorted({r["record"]["delivery_ref"] for r in live})
    if kind is K.DELIVERIES:
        have: dict[tuple[str, int], list[DeliverySnapshot]] = {}
        for part in _chunks(refs):
            for s in session.scalars(select(DeliverySnapshot).where(DeliverySnapshot.delivery_ref.in_(part))):
                have.setdefault((s.delivery_ref, s.product_id), []).append(s)
        for r in live:
            rec = r["record"]
            old = have.get((rec["delivery_ref"], rec["product_id"]), [])
            if any(s.row_hash == rec["row_hash"] for s in old):
                r["outcome"], r["record"] = DeliveryRowOutcome.DUPLICATE, None
                r["warnings"].append(
                    _err("ALREADY_IMPORTED", None, "An identical record already exists; it is not added again.")
                )
                continue
            if old and any(
                (s.supplier_id, s.destination_facility_id) != (rec["supplier_id"], rec["destination_facility_id"])
                for s in old
            ):
                r["outcome"], r["record"] = DeliveryRowOutcome.REJECTED, None
                r["errors"].append(_err("IDENTITY_CONFLICT", "supplier_code", "An earlier record of this order line has a different supplier or destination; it is not re-attached silently."))  # fmt: skip
                continue
            prior_orig = [s.original_promised_at for s in old if s.original_promised_at is not None]
            if (
                rec.get("original_promised_at") is not None
                and prior_orig
                and prior_orig[0] != rec["original_promised_at"]
            ):
                r["outcome"], r["record"] = DeliveryRowOutcome.REJECTED, None
                r["errors"].append(_err("ORIGINAL_PROMISE_CHANGED", "original_promised_at", "The original promised date is already recorded and is never overwritten. Send the new commitment as revised_promised_at."))  # fmt: skip
                continue
            if (
                any(s.observed_at == rec["observed_at"] and s.row_hash != rec["row_hash"] for s in old)
                and not rec["observed_at_defaulted"]
            ):
                r["outcome"], r["record"] = DeliveryRowOutcome.REJECTED, None
                r["errors"].append(_err("CONFLICTING_SNAPSHOT", None, "A different snapshot already exists for this order line at the same observed_at. Existing records are never overwritten."))  # fmt: skip
    else:
        have_h: set[str] = set()
        for part in _chunks(refs):
            have_h.update(session.scalars(select(DeliveryEvent.row_hash).where(DeliveryEvent.delivery_ref.in_(part))))
        for r in live:
            if r["record"]["row_hash"] in have_h:
                r["outcome"], r["record"] = DeliveryRowOutcome.DUPLICATE, None
                r["warnings"].append(
                    _err("ALREADY_IMPORTED", None, "An identical event already exists; it is not added again.")
                )
        known: set[str] = set()
        for part in _chunks(refs):
            known.update(
                session.scalars(select(DeliverySnapshot.delivery_ref).where(DeliverySnapshot.delivery_ref.in_(part)))
            )
        for r in prep.rows:
            if r["outcome"] is DeliveryRowOutcome.ACCEPTED and r["record"]["delivery_ref"] not in known:
                r["warnings"].append(_err("ORPHAN_EVENT", "delivery_ref", "No delivery record with this reference exists yet; the event is kept and used once a delivery record arrives."))  # fmt: skip


def summarize(prep: Prepared, kind: K) -> dict[str, Any]:
    c = Counter(r["outcome"].value for r in prep.rows)
    return {
        "kind": kind.value, "rows_total": len(prep.rows), "rows_accepted": c.get("ACCEPTED", 0), "rows_duplicate": c.get("DUPLICATE", 0),
        "rows_rejected": c.get("REJECTED", 0), "error_codes": dict(Counter(e["code"] for r in prep.rows for e in r["errors"])),
        "warning_codes": dict(Counter(w["code"] for r in prep.rows for w in r["warnings"])), "columns": prep.columns, "mapping": prep.mapping,
    }  # fmt: skip


def _cap(raw: Any) -> Any:
    s = json.dumps(raw, default=str)
    return raw if len(s) <= 2000 else {"truncated": s[:2000]}


def row_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "row_number": r["n"],
        "outcome": r["outcome"].value,
        "errors": r["errors"],
        "warnings": r["warnings"],
        "raw": _cap(r["raw"]),
    }


def preview(session: Session, kind: K, data: bytes, **kw: Any) -> dict[str, Any]:
    prep = prepare(session, kind, data, now=utcnow(), **kw)
    s = summarize(prep, kind)
    shown = [row_view(r) for r in prep.rows if r["outcome"] is not DeliveryRowOutcome.ACCEPTED or r["warnings"]][
        :MAX_ROW_ERRORS_RETURNED
    ]
    return {"mode": "preview", "committed": False, "summary": s, "rows": shown, "sample_accepted": [row_view(r) for r in prep.rows if r["outcome"] is DeliveryRowOutcome.ACCEPTED][:5], "would_write": s["rows_accepted"]}  # fmt: skip


def commit(
    session: Session, kind: K, data: bytes, *, source_label: str, provenance: Provenance, requested_by: str, file_name: str | None,
    atomic: bool = False, **kw: Any,
) -> tuple[DeliveryImportBatch, bool, Prepared | None]:  # fmt: skip
    fp = _fingerprint(kind, kw.get("mapping") or {}, kw["time_zone"], data)
    prior = session.scalar(
        select(DeliveryImportBatch).where(DeliveryImportBatch.kind == kind, DeliveryImportBatch.fingerprint == fp)
    )
    if prior is not None:
        return prior, False, None
    now = utcnow()
    prep = prepare(session, kind, data, now=now, **kw)
    s = summarize(prep, kind)
    write = s["rows_accepted"] > 0 and not (atomic and s["rows_rejected"] > 0)
    status = DeliveryImportStatus.COMPLETED if write or s["rows_rejected"] == 0 else DeliveryImportStatus.REJECTED
    batch = DeliveryImportBatch(
        batch_uid=f"DB-{uuid.uuid4().hex[:16].upper()}", kind=kind,
        fingerprint=fp if status is DeliveryImportStatus.COMPLETED else hashlib.sha256((fp + uuid.uuid4().hex).encode()).hexdigest(),
        status=status, source_label=source_label[:200], provenance=provenance, file_name=(file_name or None) and file_name[:255],
        file_format=kw["fmt"], requested_by=requested_by, time_zone=kw["time_zone"], mapping=prep.mapping, rows_total=s["rows_total"],
        rows_accepted=s["rows_accepted"] if write else 0, rows_duplicate=s["rows_duplicate"], rows_rejected=s["rows_rejected"],
        summary={**s, "atomic": atomic, "written": write}, created_at=now,
    )  # fmt: skip
    session.add(batch)
    session.flush()
    for r in prep.rows:
        out, rid = r["outcome"], None
        if write and out is DeliveryRowOutcome.ACCEPTED:
            rid = _insert(session, kind, batch, r["record"], provenance, source_label)
        elif out is DeliveryRowOutcome.ACCEPTED:
            out = DeliveryRowOutcome.REJECTED
            r["errors"].append(
                _err("BATCH_NOT_APPLIED", None, "Atomic import: another row failed, so no row was written.")
            )
        session.add(DeliveryImportRow(batch_id=batch.id, row_number=r["n"], outcome=out, errors=r["errors"] + r["warnings"] or None, raw=_cap(r["raw"]), record_id=rid))  # fmt: skip
    session.flush()
    return batch, True, prep


_SNAP = ("delivery_ref", "product_id", "supplier_id", "destination_facility_id", "origin_facility_id", "quantity", "unit", "order_at", "confirmed_at", "promised_dispatch_at", "dispatched_at", "original_promised_at", "revised_promised_at", "arrived_at", "received_at", "cancelled_at", "reported_status", "received_quantity", "transport_mode", "observed_at", "observed_at_defaulted", "exception_note", "row_hash")  # fmt: skip
_EVT = (
    "delivery_ref",
    "product_id",
    "event_type",
    "occurred_at",
    "value_at",
    "quantity",
    "source_event_id",
    "row_hash",
)


def _insert(
    session: Session, kind: K, b: DeliveryImportBatch, rec: dict[str, Any], prov: Provenance, label: str
) -> int:
    common = {"batch_id": b.id, "provenance": prov, "source_label": label[:200]}
    obj: Any = DeliverySnapshot(**common, **{k: rec[k] for k in _SNAP}) if kind is K.DELIVERIES else DeliveryEvent(**common, **{k: rec[k] for k in _EVT})  # fmt: skip
    session.add(obj)
    session.flush()
    return obj.id


__all__ = ["K", "commit", "default_mapping", "fields_of", "preview", "prepare"]
