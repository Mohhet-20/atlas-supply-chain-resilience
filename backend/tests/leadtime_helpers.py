"""SYNTHETIC delivery fixtures. Every record here is invented; none represents a real supplier or shipment."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from app.leadtime.semantics import Delivery
from tests.graph_helpers import node
from tests.quality_helpers import TABLE, TOK, h  # noqa: F401

LT = "/api/v1/leadtime"
NOW = datetime.now(timezone.utc).replace(microsecond=0, second=0, minute=0)
COLS = ["delivery_ref", "product_code", "destination_facility_code", "supplier_code", "quantity", "unit", "order_at", "confirmed_at", "promised_dispatch_at", "dispatched_at", "original_promised_at", "revised_promised_at", "arrived_at", "received_at", "status", "transport_mode", "observed_at"]  # fmt: skip

# supplier -> (mean lead days, sd, promise offset days, extra days per 100 units)
SUPPLIERS = {"S1": (10.0, 1.5, 11.0, 0.0), "S2": (14.0, 4.0, 13.0, 1.0), "S3": (7.0, 1.0, 8.0, 0.0)}


def iso(d: datetime | None) -> str | None:
    return None if d is None else d.strftime("%Y-%m-%dT%H:%M:%SZ")


def gen_rows(
    n: int = 240, seed: int = 7, span_days: int = 420, now: datetime = NOW, start_ref: int = 0
) -> list[dict[str, Any]]:
    """n order lines over ``span_days`` ending ~30 days ago, plus special cases appended (see below)."""
    r = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        s = r.choice(list(SUPPLIERS))
        mean, sd, prom, per100 = SUPPLIERS[s]
        qty = float(r.choice([50, 100, 200, 400]))
        order = now - timedelta(days=30 + span_days * (1 - i / n)) + timedelta(hours=r.randint(0, 12))
        lead = max(2.0, r.gauss(mean + per100 * qty / 100, sd))
        confirm = order + timedelta(hours=r.randint(2, 30))
        dispatch = order + timedelta(days=lead * 0.3)
        arrive = order + timedelta(days=lead)
        promised = confirm + timedelta(days=prom)
        row = {
            "delivery_ref": f"PO-{start_ref + i:05d}", "product_code": r.choice(["P1", "P2"]), "destination_facility_code": r.choice(["F1", "F2"]),
            "supplier_code": s, "quantity": qty, "unit": "EA", "order_at": iso(order), "confirmed_at": iso(confirm),
            "promised_dispatch_at": iso(confirm + timedelta(days=prom * 0.3)), "dispatched_at": iso(dispatch), "original_promised_at": iso(promised),
            "revised_promised_at": None, "arrived_at": iso(arrive), "received_at": iso(arrive + timedelta(hours=4)), "status": "DELIVERED",
            "transport_mode": r.choice(["ROAD", "SEA"]), "observed_at": iso(arrive + timedelta(hours=5)),
        }  # fmt: skip
        rows.append(row)
    return rows


def special_rows(now: datetime = NOW) -> dict[str, dict[str, Any]]:
    """In-transit, open, cancelled, partial and revised-promise orders (all synthetic)."""
    base = now - timedelta(days=12)
    common = {
        "product_code": "P1",
        "destination_facility_code": "F1",
        "supplier_code": "S1",
        "quantity": 100.0,
        "unit": "EA",
    }
    return {
        "in_transit": {
            **common,
            "delivery_ref": "PO-IT",
            "order_at": iso(base),
            "confirmed_at": iso(base + timedelta(hours=6)),
            "dispatched_at": iso(base + timedelta(days=3)),
            "original_promised_at": iso(now + timedelta(days=2)),
            "status": "IN_TRANSIT",
            "observed_at": iso(now - timedelta(hours=1)),
        },  # fmt: skip
        "open": {
            **common,
            "delivery_ref": "PO-OPEN",
            "order_at": iso(now - timedelta(days=2)),
            "confirmed_at": iso(now - timedelta(days=2) + timedelta(hours=5)),
            "original_promised_at": iso(now + timedelta(days=9)),
            "status": "CONFIRMED",
            "observed_at": iso(now - timedelta(hours=1)),
        },  # fmt: skip
        "cancelled": {
            **common,
            "delivery_ref": "PO-CAN",
            "order_at": iso(base),
            "cancelled_at": None,
            "status": "CANCELLED",
            "observed_at": iso(now - timedelta(days=5)),
        },  # fmt: skip
        "partial": {
            **common,
            "delivery_ref": "PO-PART",
            "order_at": iso(base),
            "arrived_at": iso(base + timedelta(days=9)),
            "original_promised_at": iso(base + timedelta(days=10)),
            "status": "PARTIALLY_RECEIVED",
            "observed_at": iso(base + timedelta(days=9)),
        },  # fmt: skip
        "revised": {
            **common,
            "delivery_ref": "PO-REV",
            "order_at": iso(now - timedelta(days=60)),
            "confirmed_at": iso(now - timedelta(days=60) + timedelta(hours=4)),
            "dispatched_at": iso(now - timedelta(days=55)),
            "original_promised_at": iso(now - timedelta(days=50)),
            "revised_promised_at": iso(now - timedelta(days=45)),
            "arrived_at": iso(now - timedelta(days=46)),
            "received_at": iso(now - timedelta(days=46)),
            "status": "DELIVERED",
            "observed_at": iso(now - timedelta(days=46)),
        },  # fmt: skip
    }


def csv_bytes(rows: list[dict[str, Any]], cols: list[str] | None = None) -> bytes:
    cols = cols or COLS
    lines = [",".join(cols)] + [",".join("" if r.get(c) is None else str(r[c]) for c in cols) for r in rows]
    return ("\n".join(lines) + "\n").encode()


IDS = {"S1": 1, "S2": 2, "S3": 3, "F1": 1, "F2": 2, "P1": 1, "P2": 2}


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_deliveries(rows: list[dict[str, Any]]) -> list[Delivery]:
    """Build Delivery objects directly (no database) for engine tests."""
    out = []
    for r in rows:
        out.append(Delivery(
            key=f"{r['delivery_ref']}|{IDS[r['product_code']]}", ref=r["delivery_ref"], product_id=IDS[r["product_code"]],
            supplier_id=IDS[r["supplier_code"]], destination_id=IDS[r["destination_facility_code"]], quantity=r.get("quantity"), unit=r.get("unit"),
            mode=r.get("transport_mode"), order_at=_dt(r.get("order_at")), confirmed_at=_dt(r.get("confirmed_at")),
            promised_dispatch_at=_dt(r.get("promised_dispatch_at")), dispatched_at=_dt(r.get("dispatched_at")),
            original_promised_at=_dt(r.get("original_promised_at")), latest_promised_at=_dt(r.get("revised_promised_at")),
            arrived_at=_dt(r.get("arrived_at")), received_at=_dt(r.get("received_at")), reported_status=r.get("status"),
            observed_at=_dt(r.get("observed_at")),
        ))  # fmt: skip
    return out


def world(client) -> dict[str, Any]:
    """Suppliers S1-S3, facilities F1-F2, products P1-P2 in the registry. Returns {'S1': node, ...}."""
    w: dict[str, Any] = {}
    for k, name in (("S1", "Alder Parts"), ("S2", "Birch Metals"), ("S3", "Cedar Resin")):
        w[k] = node(client, "suppliers", name)
    for k, name in (("F1", "Dogwood Plant"), ("F2", "Elm Hub")):
        w[k] = node(client, "facilities", name)
    for k, name in (("P1", "Fir Widget"), ("P2", "Gum Bracket")):
        w[k] = node(client, "products", name, unit_of_measure="EA")
    return w


def code_map(w: dict[str, Any]) -> dict[str, str]:
    return {k: v["code"] for k, v in w.items()}


def remap(rows: list[dict[str, Any]], cm: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        r = dict(r)
        r["product_code"], r["destination_facility_code"], r["supplier_code"] = (
            cm[r["product_code"]],
            cm[r["destination_facility_code"]],
            cm[r["supplier_code"]],
        )
        out.append(r)
    return out


def do_import(
    client,
    kind,
    data: bytes,
    *,
    label: str = "synthetic",
    atomic: bool = False,
    fmt: str = "CSV",
    tz: str = "UTC",
    **kw,
):
    from app.leadtime import ingest
    from app.models.enums import Provenance

    with client.app.state.session_factory() as s, s.begin():
        batch, created, _p = ingest.commit(
            s, kind, data, source_label=label, provenance=Provenance.SYNTHETIC_DEMO, requested_by="tester", file_name="t.csv", atomic=atomic,
            fmt=fmt, encoding=None, delimiter=",", mapping=kw.get("mapping"), time_zone=tz, date_format="%Y-%m-%d", max_rows=20_000,
            chronology_tolerance_hours=kw.get("tol", 0.0),
        )  # fmt: skip
        s.expunge(batch)
        return batch, created


def load_world_deliveries(
    client, n: int = 240, seed: int = 7, special: bool = True
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    from app.models.delivery import DeliveryImportKind

    w = world(client)
    cm = code_map(w)
    rows = gen_rows(n, seed) + (list(special_rows().values()) if special else [])
    rows = remap(rows, cm)
    do_import(client, DeliveryImportKind.DELIVERIES, csv_bytes(rows))
    return w, cm, rows


def snapshots(client) -> list[Any]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.delivery import DeliverySnapshot

    with Session(client.app.state.engine) as s:
        rows = list(s.scalars(select(DeliverySnapshot)))
        for r in rows:
            s.expunge(r)
        return rows
