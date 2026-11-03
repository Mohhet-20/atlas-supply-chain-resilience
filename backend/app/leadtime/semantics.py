"""Delivery semantics (pure): lead-time definitions, units, calendars, commitments, states and chronology checks.

Sign convention: deviation = actual arrival - committed delivery time. Positive = late, zero = on the commitment,
negative = early. A delivery is on time when deviation <= grace (default 0). A missing promised date is *unknown*,
never on time. Units are never mixed: every duration is in exactly one of HOURS, CALENDAR_DAYS (elapsed hours / 24)
or BUSINESS_DAYS (business dates strictly after the start date up to and including the end date, in the stated
time zone, excluding weekend days and listed holidays).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# --- state vocabulary --------------------------------------------------------------------------------------------
COMPLETED = "COMPLETED"  # arrived (and, if reported, fully received)
PARTIAL = "PARTIAL"  # partially received: not a full delivery
CANCELLED = "CANCELLED"
IN_TRANSIT = "IN_TRANSIT"  # dispatched, not arrived: right-censored
OPEN = "OPEN"  # ordered, not dispatched, not arrived: right-censored
INVALID = "INVALID"  # contradictory event order
UNKNOWN = "UNKNOWN"  # not enough information to classify
STATES = (COMPLETED, PARTIAL, CANCELLED, IN_TRANSIT, OPEN, INVALID, UNKNOWN)
CENSORED = (IN_TRANSIT, OPEN)


@dataclass
class Delivery:
    """One resolved order line (current state after merging snapshots and events). All timestamps are aware UTC."""

    key: str
    ref: str
    product_id: int
    supplier_id: int | None
    destination_id: int
    origin_id: int | None = None
    quantity: float | None = None
    unit: str | None = None
    mode: str | None = None
    order_at: datetime | None = None
    confirmed_at: datetime | None = None
    promised_dispatch_at: datetime | None = None
    dispatched_at: datetime | None = None
    original_promised_at: datetime | None = None
    latest_promised_at: datetime | None = None
    arrived_at: datetime | None = None
    received_at: datetime | None = None
    cancelled_at: datetime | None = None
    reported_status: str | None = None
    received_quantity: float | None = None
    observed_at: datetime | None = None
    first_observed_at: datetime | None = None
    exception_note: str | None = None
    provenance: str | None = None
    snapshot_ids: list[int] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def lane(self) -> tuple[int | None, int]:
        return (self.supplier_id, self.destination_id)


@dataclass(frozen=True)
class Calendar:
    tz: str = "UTC"
    weekend_days: tuple[int, ...] = (5, 6)
    holidays: frozenset[date] = frozenset()


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def business_days_between(start: datetime, end: datetime, cal: Calendar) -> int:
    """Business dates d with local(start).date < d <= local(end).date (negative if end precedes start)."""
    z = ZoneInfo(cal.tz)
    s, e = start.astimezone(z).date(), end.astimezone(z).date()
    sign = 1
    if e < s:
        s, e, sign = e, s, -1
    n, d = 0, s + timedelta(days=1)
    while d <= e:
        if d.weekday() not in cal.weekend_days and d not in cal.holidays:
            n += 1
        d += timedelta(days=1)
    return sign * n


def elapsed(start: datetime, end: datetime, unit: str, cal: Calendar | None = None) -> float:
    if unit == "HOURS":
        return _hours(start, end)
    if unit == "CALENDAR_DAYS":
        return _hours(start, end) / 24.0
    if unit == "BUSINESS_DAYS":
        if cal is None:
            raise ValueError("BUSINESS_DAYS requires an explicit calendar.")
        return float(business_days_between(start, end, cal))
    raise ValueError(f"Unknown unit {unit}.")


def commitment_of(d: Delivery, which: str) -> datetime | None:
    if which == "ORIGINAL":
        return d.original_promised_at
    return d.latest_promised_at or d.original_promised_at


def endpoints(d: Delivery, definition: str, start_event: str = "ORDER") -> tuple[datetime | None, datetime | None, str]:
    """(start, end, description) for a lead-time definition. No fallback between different events is applied."""
    s_order = d.order_at if start_event == "ORDER" else d.confirmed_at
    s_name = "order placement" if start_event == "ORDER" else "order confirmation"
    if definition == "ORDER_TO_RECEIPT":
        return s_order, d.received_at, f"{s_name} -> receipt"
    if definition == "DISPATCH_TO_ARRIVAL":
        return d.dispatched_at, d.arrived_at, "dispatch -> arrival"
    if definition == "ORDER_TO_DISPATCH":
        return s_order, d.dispatched_at, f"{s_name} -> dispatch"
    raise ValueError(f"'{definition}' has no start/end pair.")


def deviation_hours(d: Delivery, commitment: str) -> float | None:
    """actual arrival - committed time, in hours; None when either is unknown."""
    c = commitment_of(d, commitment)
    if c is None or d.arrived_at is None:
        return None
    return _hours(c, d.arrived_at)


def is_late(d: Delivery, commitment: str, grace_hours: float = 0.0) -> bool | None:
    dev = deviation_hours(d, commitment)
    return None if dev is None else dev > grace_hours


_ORDER = (
    ("order_at", "confirmed_at", "CONFIRMED_BEFORE_ORDER"),
    ("order_at", "dispatched_at", "DISPATCHED_BEFORE_ORDER"),
    ("confirmed_at", "dispatched_at", "DISPATCHED_BEFORE_CONFIRMATION"),
    ("dispatched_at", "arrived_at", "ARRIVED_BEFORE_DISPATCH"),
    ("order_at", "arrived_at", "ARRIVED_BEFORE_ORDER"),
    ("arrived_at", "received_at", "RECEIVED_BEFORE_ARRIVAL"),
    ("order_at", "received_at", "RECEIVED_BEFORE_ORDER"),
)


def chronology_violations(d: Delivery, tolerance_hours: float = 0.0) -> list[str]:
    out = []
    for a, b, code in _ORDER:
        ta, tb = getattr(d, a), getattr(d, b)
        if ta is not None and tb is not None and _hours(ta, tb) < -tolerance_hours:
            out.append(code)
    return out


def classify(d: Delivery, as_of: datetime, tolerance_hours: float = 0.0) -> tuple[str, list[str]]:
    """Return (state, reasons). Right-censored orders are never reported as completed deliveries."""
    bad = chronology_violations(d, tolerance_hours)
    if bad:
        return INVALID, bad
    rs = (d.reported_status or "").upper()
    if d.cancelled_at is not None or rs == "CANCELLED":
        return CANCELLED, []
    arrived = d.arrived_at is not None or d.received_at is not None
    partial = rs == "PARTIALLY_RECEIVED" or (
        d.received_quantity is not None and d.quantity is not None and d.received_quantity + 1e-9 < d.quantity
    )
    if arrived and partial:
        return PARTIAL, ["received quantity is below the ordered quantity"]
    if partial and not arrived:
        return PARTIAL, ["partially received; no completion recorded"]
    if arrived:
        return COMPLETED, []
    if d.order_at is None and d.confirmed_at is None and d.dispatched_at is None:
        return UNKNOWN, ["no order, confirmation or dispatch time is recorded"]
    if d.dispatched_at is not None and d.dispatched_at <= as_of:
        return IN_TRANSIT, []
    return OPEN, []


def to_utc(dt: datetime, tz: str = "UTC") -> tuple[datetime, bool]:
    """Aware -> UTC. Naive -> interpreted in ``tz`` (flagged). Ambiguous/non-existent local times are resolved with fold=0."""
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return dt.astimezone(timezone.utc), False
    return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc), True


def parse_ts(text: str, tz: str = "UTC") -> tuple[datetime, bool, bool]:
    """ISO 8601 -> (UTC datetime, assumed_timezone, date_only)."""
    t = text.strip().replace("Z", "+00:00").replace("z", "+00:00")
    date_only = "T" not in t and " " not in t
    d = datetime.fromisoformat(t if not date_only else t + "T00:00:00")
    utc, assumed = to_utc(d, tz)
    return utc, assumed, date_only
