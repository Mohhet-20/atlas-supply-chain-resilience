"""Migration 0007 -> 0008, insert-only results, constraints and a measured (not asserted-as-scale) benchmark."""

from __future__ import annotations

import pathlib
import random
import time

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from alembic import command
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from app.impact import config as cfg
from app.impact import runs as iruns
from app.impact.engine import ImpactLimits
from app.models.enums import (
    EdgeStatus,
    EntityStatus,
    FacilityType,
    ProductLinkType,
    Provenance,
    RelationshipType,
)
from app.models.facility import Facility
from app.models.impact import ImpactAnalysis, ImpactCriticality, ImpactFinding
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.scenarios import runs as sruns
from tests.impact_helpers import analyze, findings, linear, make_run, new_scenario, sup_event

TABLES = {"impact_analyses", "impact_findings", "impact_edge_snapshots", "impact_criticality", "impact_alternatives"}


def test_upgrade_from_0007_keeps_data_and_adds_empty_impact_tables(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    conf = alembic_config(url)
    command.upgrade(conf, "0007")
    eng = create_db_engine(url)
    assert TABLES.isdisjoint(inspect(eng).get_table_names())
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppliers (supplier_code, name, name_normalized, country_or_region, category, status, provenance, version, created_at, updated_at) VALUES ('S-1','Legacy Supplier','legacy supplier','Germany','Parts','ACTIVE','USER_PROVIDED',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
    eng.dispose()
    command.upgrade(conf, "head")
    eng = create_db_engine(url)
    insp = inspect(eng)
    assert TABLES <= set(insp.get_table_names())
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM suppliers")).scalar() == "Legacy Supplier"
        for t in TABLES:
            assert c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0  # noqa: S608 - nothing is seeded or invented
    assert "uq_impact_analyses_active_run" in {i["name"] for i in insp.get_indexes("impact_analyses")}
    command.downgrade(conf, "0007")
    assert TABLES.isdisjoint(inspect(eng).get_table_names())
    eng.dispose()


def test_results_are_insert_only(client):
    w = linear(client)
    a = analyze(client, make_run(client, [sup_event(w["a"])]))
    with Session(client.app.state.engine) as s:
        f = s.query(ImpactFinding).filter_by(analysis_id=a.id).first()
        f.explanation = "tampered"
        with pytest.raises(ValueError, match="append-only"):
            s.flush()
        s.rollback()
        c = s.query(ImpactCriticality).filter_by(analysis_id=a.id).first()
        s.delete(c)
        with pytest.raises(ValueError, match="append-only"):
            s.flush()


def test_one_active_analysis_per_run_is_enforced_by_the_database(client):
    from sqlalchemy.exc import IntegrityError

    w = linear(client)
    run = make_run(client, [sup_event(w["a"])])
    with Session(client.app.state.engine) as s:

        def row(uid):
            return ImpactAnalysis(analysis_uid=uid, run_id=run, scenario_id=1, scenario_version=1, status="QUEUED", requested_by="t", config={}, config_hash="x", algorithm_version="ci-1")  # fmt: skip

        s.add(row("IA-A"))
        s.flush()
        s.add(row("IA-B"))
        with pytest.raises(IntegrityError):
            s.flush()


def _bulk_layered(client, tiers=(8, 40, 80, 120), products=100, fan_in=2, seed=7):
    """Synthetic layered network inserted directly (speed). Names are generic and clearly synthetic."""
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic benchmark data
    with Session(client.app.state.engine) as s, s.begin():
        sup = [Supplier(supplier_code=f"BENCH-S{i}", name=f"Bench Supplier {i}", country_or_region="XX", category="Bench", status=EntityStatus.ACTIVE, provenance=Provenance.SYNTHETIC_DEMO) for i in range(tiers[0])]  # fmt: skip
        s.add_all(sup)
        layers = []
        for ti, n in enumerate(tiers[1:], 1):
            fl = [Facility(facility_code=f"BENCH-F{ti}-{i}", name=f"Bench Facility {ti}-{i}", facility_type=FacilityType.MANUFACTURING, country_or_region="XX", status=EntityStatus.ACTIVE, provenance=Provenance.SYNTHETIC_DEMO) for i in range(n)]  # fmt: skip
            s.add_all(fl)
            layers.append(fl)
        prods = [Product(product_code=f"BENCH-P{i}", name=f"Bench Product {i}", status=EntityStatus.ACTIVE, provenance=Provenance.SYNTHETIC_DEMO) for i in range(products)]  # fmt: skip
        s.add_all(prods)
        s.flush()
        n_edges = 0
        for f in layers[0]:
            for sp in rng.sample(sup, fan_in):
                s.add(SupplyRelationship(relationship_code=f"BENCH-R{n_edges}", relationship_type=RelationshipType.SUPPLIES, supplier_id=sp.id, target_facility_id=f.id, provenance=Provenance.SYNTHETIC_DEMO, status=EdgeStatus.CONFIRMED, supply_share_pct=100.0 / fan_in, confidence=0.8))  # fmt: skip
                n_edges += 1
        for prev, nxt in zip(layers, layers[1:], strict=False):
            for f in nxt:
                for src in rng.sample(prev, fan_in):
                    s.add(SupplyRelationship(relationship_code=f"BENCH-R{n_edges}", relationship_type=RelationshipType.TRANSFERS_TO, source_facility_id=src.id, target_facility_id=f.id, provenance=Provenance.SYNTHETIC_DEMO, status=EdgeStatus.CONFIRMED, supply_share_pct=100.0 / fan_in, confidence=0.8))  # fmt: skip
                    n_edges += 1
        for p in prods:
            s.add(ProductLink(link_code=f"BENCH-L{p.id}", link_type=ProductLinkType.PRODUCES, product_id=p.id, facility_id=rng.choice(layers[-1]).id, provenance=Provenance.SYNTHETIC_DEMO, status=EdgeStatus.CONFIRMED))  # fmt: skip
            n_edges += 1
        ids = [x.id for x in sup]
    return ids, n_edges, sum(tiers[1:]) + tiers[0] + products


def test_benchmark_layered_network(client):
    sup_ids, n_edges, n_nodes = _bulk_layered(client)
    events = [
        {"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": i, "duration_days": 7}
        for i in sup_ids[:5]
    ]
    big = sruns.RunLimits(max_depth=4, max_nodes=2000, max_edges=5000, time_budget_seconds=120)
    sid = new_scenario(client, events, downstream_depth=4)
    t0 = time.perf_counter()
    run = sruns.run_now(client.app.state.session_factory, sid, "bench", big)
    t_run = time.perf_counter() - t0
    assert run.status.value in ("COMPLETED", "PARTIAL")
    lim = iruns.AnalysisLimits(
        engine=ImpactLimits(
            max_depth=4,
            max_nodes=2000,
            max_edges=5000,
            time_budget_seconds=120,
            max_path_steps=50_000,
            max_findings=2000,
        )
    )
    t0 = time.perf_counter()
    a = analyze(client, run.id, cfg.ImpactConfig(), lim)
    t_an = time.perf_counter() - t0
    f = findings(client, a.id)
    s = a.summary
    print(f"BENCH5 nodes={n_nodes} edges={n_edges} events=5 depth=4 path_cap=50000 sim_run_s={t_run:.2f} analysis_s={t_an:.2f} status={a.status.value} unique_entities={s['unique_entities']} distinct_paths_found={s['distinct_paths_found']} paths_exact={s['distinct_paths_exact']} reasons={a.truncation_reasons}")  # fmt: skip
    assert len(f) == s["unique_entities"] <= 2000 and s["unique_entities"] > 5
    assert s["distinct_paths_found"] >= s["unique_entities"] - 5 or not s["distinct_paths_exact"]
    if a.status.value == "PARTIAL":  # a hit limit must be visible, never silent
        assert a.truncated and a.truncation_reasons
