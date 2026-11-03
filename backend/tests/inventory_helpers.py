"""Synthetic fixtures for inventory and stockout tests. Every number here is invented."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.inventory import config as cfg
from app.inventory import ingest, runs
from app.models.enums import Provenance
from app.models.inventory import ImportKind, StockoutAnalysis, StockoutFinding
from tests.graph_helpers import node, ok
from tests.impact_helpers import analyze, make_run, sup_event
from tests.quality_helpers import TABLE, TOK, h

INV = "/api/v1/inventory"
__all__ = ["INV", "TABLE", "TOK", "h"]

NOW = datetime.now(timezone.utc).replace(microsecond=0)
TODAY = NOW.date()


def iso(days_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def world(client) -> dict[str, Any]:
    """S1 supplies F1 (100%); F2 has no supplier. Products P1 (EA) and P2 (EA), P3 (KG)."""
    w = {
        "s1": node(client, "suppliers", "Alder Parts"),
        "f1": node(client, "facilities", "Cedar Plant"),
        "f2": node(client, "facilities", "Dogwood Hub"),
        "p1": node(client, "products", "Elm Widget", unit_of_measure="EA"),
        "p2": node(client, "products", "Fir Bracket", unit_of_measure="EA"),
        "p3": node(client, "products", "Gum Resin", unit_of_measure="KG"),
    }
    ok(client, "SUPPLIES", w["s1"], w["f1"], supply_share_pct=100.0, confidence=0.9)
    ok(client, "PRODUCES", w["f1"], w["p1"], supply_share_pct=100.0, confidence=0.9)
    return w


def code(w: dict[str, Any], key: str) -> str:
    return w[key]["code"]


def csv_of(header: list[str], rows: list[list[Any]]) -> bytes:
    return (
        "\n".join([",".join(header)] + [",".join("" if c is None else str(c) for c in r) for r in rows]) + "\n"
    ).encode()


def do_import(
    client, kind: ImportKind, data: bytes, *, label: str = "synthetic", atomic: bool = False, fmt: str = "CSV", **kw
):
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        batch, created, _p = ingest.commit(
            s, kind, data, source_label=label, provenance=Provenance.SYNTHETIC_DEMO, requested_by="tester", file_name="t.csv",
            atomic=atomic, fmt=fmt, encoding=None, delimiter=",", mapping=kw.get("mapping"), date_format=kw.get("date_format", "%Y-%m-%d"),
            max_rows=10_000,
        )  # fmt: skip
        s.expunge(batch)
        return batch, created


def load_inventory(client, rows: list[list[Any]], **kw):
    hdr = [
        "product_code",
        "facility_code",
        "observed_at",
        "unit",
        "on_hand",
        "reserved",
        "allocated",
        "quarantined",
        "damaged",
        "in_transit",
        "backordered",
    ]
    return do_import(client, ImportKind.INVENTORY, csv_of(hdr, rows), **kw)


def load_demand(client, rows: list[list[Any]], **kw):
    hdr = ["product_code", "facility_code", "kind", "period_start", "period_end", "quantity", "unit"]
    return do_import(client, ImportKind.DEMAND, csv_of(hdr, rows), **kw)


def daily_demand(w, p: str, f: str, per_day: float, days: int = 30, unit: str = "EA") -> list[list[Any]]:
    return [[code(w, p), code(w, f), "CONSUMPTION", d(i + 1), d(i), per_day, unit] for i in range(days)]


def load_receipts(client, rows: list[list[Any]], **kw):
    hdr = ["product_code", "facility_code", "reference", "status", "quantity", "unit", "expected_date", "supplier_code"]
    return do_import(client, ImportKind.REPLENISHMENT, csv_of(hdr, rows), **kw)


def populate(client, w) -> None:
    """P1@F1: 100 on hand, 10/day -> ~10 days cover; inbound 200 in 5 days from S1 (confirmed).
    P1@F2: 1000 on hand, 5/day.  P2@F1: stock but no demand history (stays unknown)."""
    load_inventory(client, [
        [code(w, "p1"), code(w, "f1"), iso(1), "EA", 100, 0, 0, 0, 0, "", ""],
        [code(w, "p1"), code(w, "f2"), iso(1), "EA", 1000, 0, 0, 0, 0, "", ""],
        [code(w, "p2"), code(w, "f1"), iso(1), "EA", 50, 0, 0, 0, 0, "", ""],
    ])  # fmt: skip
    load_demand(client, daily_demand(w, "p1", "f1", 10) + daily_demand(w, "p1", "f2", 5))
    load_receipts(client, [[code(w, "p1"), code(w, "f1"), "PO-1", "CONFIRMED", 200, "EA", d(-5), code(w, "s1")]])


def run_baseline(client, conf: cfg.InventoryConfig | None = None, **kw) -> StockoutAnalysis:
    return runs.analyze_now(
        client.app.state.session_factory,
        "tester",
        runs.Limits(),
        conf or cfg.InventoryConfig(as_of=datetime.now(timezone.utc)),
        **kw,
    )


def scenario_impact(client, w) -> int:
    rid = make_run(client, [sup_event(w["s1"], duration_days=30)])
    return analyze(client, rid).id


def findings_of(client, analysis_id: int) -> dict[tuple[str, str], StockoutFinding]:
    with Session(client.app.state.engine) as s:
        rows = list(s.scalars(select(StockoutFinding).where(StockoutFinding.analysis_id == analysis_id)))
        for r in rows:
            s.expunge(r)
        return {(r.product_code, r.facility_code): r for r in rows}


def _unused() -> date:  # keeps the date import used for type checkers
    return TODAY
