"""Helpers for cascading-impact tests (synthetic data only)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.impact import config as cfg
from app.impact import runs as iruns
from app.models.impact import ImpactAnalysis, ImpactFinding
from app.scenarios import runs as sruns
from app.scenarios import service
from tests.graph_helpers import node, ok
from tests.quality_helpers import TABLE, TOK, h

IM = "/api/v1/impact"
__all__ = ["IM", "TABLE", "TOK", "h"]


def new_scenario(client, events: list[dict[str, Any]], name: str = "S", **assump: Any) -> int:
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        sc = service.create_scenario(s, "tester", name=name, description=None, scenario_type=service.ScenarioType.CUSTOM, assumptions=assump or None, notes=None, template_key=None, max_scenarios=100)  # fmt: skip
        for e in events:
            service.add_event(s, "tester", sc, e, None, 25)
        return sc.id


def sup_event(n: dict, **kw: Any) -> dict[str, Any]:
    return {
        "event_type": "SUPPLIER_UNAVAILABLE",
        "target_type": "SUPPLIER",
        "target_id": n["id"],
        "duration_days": 10,
    } | kw


def make_run(client, events: list[dict[str, Any]], **assump: Any) -> int:
    sid = new_scenario(client, events, **assump)
    return sruns.run_now(client.app.state.session_factory, sid, "tester", sruns.RunLimits()).id


def analyze(
    client, run_id: int, conf: cfg.ImpactConfig | None = None, limits: iruns.AnalysisLimits | None = None
) -> ImpactAnalysis:
    return iruns.analyze_now(client.app.state.session_factory, run_id, "tester", limits or iruns.AnalysisLimits(), conf)


def findings(client, analysis_id: int) -> dict[str, ImpactFinding]:
    with Session(client.app.state.engine) as s:
        rows = list(s.scalars(select(ImpactFinding).where(ImpactFinding.analysis_id == analysis_id)))
        for r in rows:
            s.expunge(r)
        return {r.entity_name: r for r in rows}


def rows_of(client, model, analysis_id: int) -> list:
    with Session(client.app.state.engine) as s:
        rows = list(s.scalars(select(model).where(model.analysis_id == analysis_id)))
        for r in rows:
            s.expunge(r)
        return rows


def linear(client, shares: tuple[float | None, float | None] = (100.0, 100.0)):
    """supplier A -> facility F1 -> facility F2 -> product P1 (produced by F2)."""
    a = node(client, "suppliers", "Alder Parts")
    f1 = node(client, "facilities", "Cedar Plant")
    f2 = node(client, "facilities", "Dogwood Hub")
    p1 = node(client, "products", "Elm Widget")
    kw = lambda s: {} if s is None else {"supply_share_pct": s}  # noqa: E731
    ok(client, "SUPPLIES", a, f1, **kw(shares[0]), confidence=0.9)
    ok(client, "TRANSFERS_TO", f1, f2, **kw(shares[1]), confidence=0.9)
    ok(client, "PRODUCES", f2, p1, supply_share_pct=100.0, confidence=0.9)
    return {"a": a, "f1": f1, "f2": f2, "p1": p1}


def realistic(client):
    """Synthetic multi-tier network with convergence, a single-source facility and alternate paths.

    S1, S2 -> F1 (50/50); S2 -> F2 (100); F1 -> F3 (60), F2 -> F3 (40); F3 produces P1; F1 produces P2.
    Alternates: S3 -> F3 (confirmed), S4 -> F2 (proposed). S1 and S2 are the two overlapping disruptions.
    """
    w = {n: node(client, "suppliers", f"Supplier {n}") for n in ("S1", "S2", "S3", "S4")}
    w |= {n: node(client, "facilities", f"Facility {n}") for n in ("F1", "F2", "F3")}
    w |= {n: node(client, "products", f"Product {n}") for n in ("P1", "P2")}
    ok(
        client,
        "SUPPLIES",
        w["S1"],
        w["F1"],
        supply_share_pct=50.0,
        confidence=0.9,
        capacity_value=100,
        capacity_unit="units/week",
    )
    ok(client, "SUPPLIES", w["S2"], w["F1"], supply_share_pct=50.0, confidence=0.6)
    ok(client, "SUPPLIES", w["S2"], w["F2"], supply_share_pct=100.0, confidence=0.9)
    ok(client, "TRANSFERS_TO", w["F1"], w["F3"], supply_share_pct=60.0, confidence=0.9)
    ok(client, "TRANSFERS_TO", w["F2"], w["F3"], supply_share_pct=40.0, confidence=0.9)
    ok(client, "PRODUCES", w["F3"], w["P1"], supply_share_pct=100.0, confidence=0.9)
    ok(client, "PRODUCES", w["F1"], w["P2"], supply_share_pct=100.0, confidence=0.9)
    ok(client, "SUPPLIES", w["S3"], w["F3"], supply_share_pct=0.0, confidence=0.9)
    ok(client, "SUPPLIES", w["S4"], w["F2"], status="PROPOSED", confidence=0.4)
    return w
