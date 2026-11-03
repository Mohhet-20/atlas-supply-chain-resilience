"""SYNTHETIC fixtures for inventory-optimisation tests. Every number is invented; results verify the software only."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.invopt import data, engine
from app.invopt.config import Limits, ReplenishRequest
from tests.inventory_helpers import code, d, iso, load_demand, load_inventory, load_receipts, world

RP = "/api/v1/replenishment"
LIM = Limits()
__all__ = [
    "RP",
    "LIM",
    "setup",
    "request",
    "snapshot",
    "run",
    "noisy_rows",
    "add_policy",
    "intermittent_setup",
    "load_leads",
]


def noisy_rows(
    w: dict[str, Any], p: str, f: str, mean: float, sd: float, days: int, seed: int = 1, unit: str = "EA"
) -> list[list[Any]]:
    rng = random.Random(seed)
    return [
        [code(w, p), code(w, f), "CONSUMPTION", d(i + 1), d(i), round(max(0.0, rng.gauss(mean, sd)), 2), unit]
        for i in range(days)
    ]


def setup(client, *, mean: float = 10.0, sd: float = 3.0, days: int = 120, on_hand: float = 100.0, receipts: bool = False, backordered: Any = "", seed: int = 1) -> dict[str, Any]:  # fmt: skip
    w = world(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(0.1), "EA", on_hand, 0, 0, 0, 0, "", backordered]])
    load_demand(client, noisy_rows(w, "p1", "f1", mean, sd, days, seed))
    if receipts:
        load_receipts(client, [[code(w, "p1"), code(w, "f1"), "PO-1", "CONFIRMED", 80, "EA", d(-4), code(w, "s1")]])
    return w


def request(w: dict[str, Any], **kw: Any) -> ReplenishRequest:
    base: dict[str, Any] = dict(
        product_id=w["p1"]["id"],
        facility_id=w["f1"]["id"],
        lead_time_days=5,
        lead_time_std_days=1.0,
        replications=30,
        simulation_days=90,
        seed=7,
    )
    return ReplenishRequest(**(base | kw))


def snapshot(client, req: ReplenishRequest, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    with client.app.state.session_factory() as s:
        return data.load(s, req, now, LIM)


def run(client, **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    w = kw.pop("w", None) or setup(client)
    req = request(w, **kw)
    snap = snapshot(client, req)
    return snap, engine.compute(snap, req, LIM)


def add_policy(client, w: dict[str, Any], **kw: Any) -> None:
    from datetime import timedelta

    from app.models.enums import Provenance
    from app.models.inventory import InventoryPolicy, PolicyStatus
    from app.models.types import utcnow

    base: dict[str, Any] = dict(product_id=w["p1"]["id"], facility_id=w["f1"]["id"], version=1, status=PolicyStatus.ACTIVE, unit="EA", effective_from=utcnow() - timedelta(days=30), owner="tester", provenance=Provenance.USER_PROVIDED, created_by="tester")  # fmt: skip
    with client.app.state.session_factory() as s, s.begin():
        s.add(InventoryPolicy(**(base | kw)))


def intermittent_setup(client, *, days: int = 120, on_hand: float = 60.0, seed: int = 3) -> dict[str, Any]:
    w = world(client)
    rng = random.Random(seed)
    rows = [[code(w, "p1"), code(w, "f1"), "CONSUMPTION", d(i + 1), d(i), float(rng.choice([4, 6, 9])) if rng.random() < 0.25 else 0.0, "EA"] for i in range(days)]  # fmt: skip
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(0.1), "EA", on_hand, 0, 0, 0, 0, "", ""]])
    load_demand(client, rows)
    return w


def load_leads(client, w: dict[str, Any], leads: list[float]) -> None:
    """Completed deliveries of p1 to f1 from s1, one per lead time (days), ordered well in the past."""
    from datetime import timedelta

    from app.models.delivery import DeliveryImportKind
    from tests.leadtime_helpers import csv_bytes, do_import
    from tests.leadtime_helpers import iso as liso

    now = datetime.now(timezone.utc)
    rows = []
    for i, lt in enumerate(leads):
        order = now - timedelta(days=60 + 3 * i)
        arr = order + timedelta(days=lt)
        rows.append({"delivery_ref": f"D-{i:03d}", "product_code": w["p1"]["code"], "destination_facility_code": w["f1"]["code"], "supplier_code": w["s1"]["code"], "quantity": 100, "unit": "EA", "order_at": liso(order), "original_promised_at": liso(order + timedelta(days=lt)), "arrived_at": liso(arr), "received_at": liso(arr), "status": "DELIVERED", "observed_at": liso(arr)})  # fmt: skip
    do_import(client, DeliveryImportKind.DELIVERIES, csv_bytes(rows))
