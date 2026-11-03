"""Resolve immutable snapshots and events into the current (or as-known-at-T) state of each order line.

Rules: the original promised date is the earliest recorded one and is never replaced; a later commitment is carried as the
latest promise. A snapshot value wins over an event value for the same fact (a conflict is flagged, not hidden). Later
corrections of a timestamp are applied (latest snapshot wins) and flagged. Nothing is invented: an absent fact stays absent.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leadtime.semantics import Delivery
from app.models.delivery import DeliveryEvent, DeliverySnapshot

_TS = ("order_at", "confirmed_at", "promised_dispatch_at", "dispatched_at", "arrived_at", "received_at", "cancelled_at")
_EVENT_FIELD = {"ORDER_CREATED": "order_at", "ORDER_CONFIRMED": "confirmed_at", "DISPATCHED": "dispatched_at", "ARRIVED": "arrived_at", "RECEIVED": "received_at", "CANCELLED": "cancelled_at"}  # fmt: skip


def key_of(ref: str, product_id: int) -> str:
    return f"{ref}|{product_id}"


def resolve_one(
    snaps: list[DeliverySnapshot], events: list[DeliveryEvent], as_of: datetime | None = None
) -> Delivery | None:
    snaps = sorted((s for s in snaps if as_of is None or s.observed_at <= as_of), key=lambda s: (s.observed_at, s.id))
    if not snaps:
        return None
    events = sorted((e for e in events if as_of is None or e.occurred_at <= as_of), key=lambda e: (e.occurred_at, e.id))
    last, first = snaps[-1], snaps[0]
    d = Delivery(
        key=key_of(last.delivery_ref, last.product_id), ref=last.delivery_ref, product_id=last.product_id, supplier_id=last.supplier_id,
        destination_id=last.destination_facility_id, origin_id=last.origin_facility_id, quantity=last.quantity, unit=last.unit,
        mode=last.transport_mode, observed_at=last.observed_at, first_observed_at=first.observed_at, reported_status=last.reported_status,
        received_quantity=last.received_quantity, exception_note=last.exception_note, provenance=last.provenance.value,
        snapshot_ids=[s.id for s in snaps], event_ids=[e.id for e in events],
    )  # fmt: skip
    for f in _TS:
        seen = [getattr(s, f) for s in snaps if getattr(s, f) is not None]
        if seen:
            setattr(d, f, seen[-1])
            if len({*seen}) > 1:
                d.flags.append(f"CORRECTED_{f.upper()}")
    originals = [s.original_promised_at for s in snaps if s.original_promised_at is not None]
    revised = [s.revised_promised_at for s in snaps if s.revised_promised_at is not None]
    d.original_promised_at = originals[0] if originals else None
    d.latest_promised_at = revised[-1] if revised else None
    if d.mode is None:
        modes = [s.transport_mode for s in snaps if s.transport_mode]
        d.mode = modes[-1] if modes else None
    by_type: dict[str, list[DeliveryEvent]] = defaultdict(list)
    for e in events:
        by_type[e.event_type].append(e)
    for etype, field in _EVENT_FIELD.items():
        evs = by_type.get(etype)
        if not evs:
            continue
        if len(evs) > 1:
            d.flags.append(f"MULTIPLE_{etype}_EVENTS")
        v = evs[0].occurred_at
        cur = getattr(d, field)
        if cur is None:
            setattr(d, field, v)
        elif cur != v:
            d.flags.append(f"EVENT_CONFLICT_{field.upper()}")
    revs = by_type.get("PROMISE_REVISED")
    if revs:
        newest = revs[-1]
        if newest.value_at is not None and (not revised or newest.occurred_at > snaps[-1].observed_at):
            d.latest_promised_at = newest.value_at
        if d.original_promised_at is None:
            d.flags.append("REVISED_WITHOUT_ORIGINAL")
    part = by_type.get("PARTIAL_RECEIPT")
    if part:
        got = sum(e.quantity or 0.0 for e in part)
        d.received_quantity = max(d.received_quantity or 0.0, got)
        if d.quantity is not None and got + 1e-9 < d.quantity and d.received_at is None:
            d.reported_status = "PARTIALLY_RECEIVED"
    rec = by_type.get("RECEIVED")
    if rec and rec[0].quantity is not None:
        d.received_quantity = rec[0].quantity
    if d.original_promised_at is None and d.latest_promised_at is not None:
        d.flags.append("REVISED_WITHOUT_ORIGINAL")
    return d


def load_deliveries(
    session: Session, *, as_of: datetime | None = None, supplier_id: int | None = None, destination_id: int | None = None,
    product_id: int | None = None, refs: list[str] | None = None, limit: int | None = None,
) -> list[Delivery]:  # fmt: skip
    q = select(DeliverySnapshot)
    if supplier_id:
        q = q.where(DeliverySnapshot.supplier_id == supplier_id)
    if destination_id:
        q = q.where(DeliverySnapshot.destination_facility_id == destination_id)
    if product_id:
        q = q.where(DeliverySnapshot.product_id == product_id)
    if refs:
        q = q.where(DeliverySnapshot.delivery_ref.in_(refs))
    if as_of is not None:
        q = q.where(DeliverySnapshot.observed_at <= as_of)
    groups: dict[str, list[DeliverySnapshot]] = defaultdict(list)
    for s in session.scalars(q.order_by(DeliverySnapshot.id)):
        groups[key_of(s.delivery_ref, s.product_id)].append(s)
    keys = sorted(groups)
    if limit is not None:
        keys = keys[: limit + 1]
    ref_set = sorted({groups[k][0].delivery_ref for k in keys})
    ev: dict[str, list[DeliveryEvent]] = defaultdict(list)
    for i in range(0, len(ref_set), 400):
        for e in session.scalars(select(DeliveryEvent).where(DeliveryEvent.delivery_ref.in_(ref_set[i : i + 400]))):
            ev[e.delivery_ref].append(e)
    out = []
    for k in keys:
        snaps = groups[k]
        pid = snaps[0].product_id
        evs = [e for e in ev.get(snaps[0].delivery_ref, []) if e.product_id in (None, pid)]
        d = resolve_one(snaps, evs, as_of)
        if d is not None:
            out.append(d)
    return out


def data_hash(deliveries: list[Delivery]) -> str:
    import hashlib

    h = hashlib.sha256()
    for d in sorted(deliveries, key=lambda x: x.key):
        h.update(f"{d.key}:{d.snapshot_ids}:{d.event_ids}".encode())
    return h.hexdigest()


def summary_of(d: Delivery) -> dict[str, Any]:
    def iso(x: datetime | None) -> str | None:
        return None if x is None else x.isoformat()

    return {
        "key": d.key, "delivery_ref": d.ref, "product_id": d.product_id, "supplier_id": d.supplier_id, "destination_facility_id": d.destination_id,
        "origin_facility_id": d.origin_id, "quantity": d.quantity, "unit": d.unit, "transport_mode": d.mode, "order_at": iso(d.order_at),
        "confirmed_at": iso(d.confirmed_at), "promised_dispatch_at": iso(d.promised_dispatch_at), "dispatched_at": iso(d.dispatched_at),
        "original_promised_at": iso(d.original_promised_at), "latest_promised_at": iso(d.latest_promised_at), "arrived_at": iso(d.arrived_at),
        "received_at": iso(d.received_at), "cancelled_at": iso(d.cancelled_at), "reported_status": d.reported_status,
        "received_quantity": d.received_quantity, "observed_at": iso(d.observed_at), "flags": d.flags, "provenance": d.provenance,
        "snapshots": len(d.snapshot_ids), "events": len(d.event_ids),
    }  # fmt: skip
