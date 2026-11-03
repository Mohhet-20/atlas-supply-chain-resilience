"""Reproducible, seeded measurement on synthetic data. It reports what was measured and makes no enterprise-scale claim."""

from __future__ import annotations

import random
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.inventory import config as cfg
from app.inventory import runs
from app.inventory.analysis import InvLimits
from app.models.enums import Provenance
from app.models.facility import Facility
from app.models.inventory import (
    DemandKind,
    DemandObservation,
    ImportKind,
    ImportStatus,
    InventoryImportBatch,
    InventoryPosition,
    InventoryReplenishment,
    ReceiptStatus,
)
from app.models.product import Product
from app.models.types import utcnow
from app.scenarios import runs as sruns
from tests.impact_helpers import analyze, new_scenario
from tests.test_impact_db import _bulk_layered

SEED = 9
PRODUCTS, DEMAND_DAYS, HORIZON = 100, 30, 60


def test_benchmark_inventory_and_stockout(client):
    rng = random.Random(SEED)  # noqa: S311 - deterministic synthetic benchmark data
    sup_ids, n_edges, _n = _bulk_layered(client, tiers=(5, 10, 10, 20), products=PRODUCTS, seed=SEED)
    now = utcnow().replace(microsecond=0)
    with Session(client.app.state.engine) as s, s.begin():
        facs = list(s.scalars(select(Facility).where(Facility.facility_code.like("BENCH-F3-%"))))
        prods = list(s.scalars(select(Product).where(Product.product_code.like("BENCH-P%"))))
        for p in prods:
            p.unit_of_measure = "EA"
        batch = InventoryImportBatch(
            batch_uid="IB-BENCH", kind=ImportKind.INVENTORY, fingerprint="bench", status=ImportStatus.COMPLETED, source_label="bench",
            provenance=Provenance.SYNTHETIC_DEMO, file_format="CSV", requested_by="bench", created_at=now,
        )  # fmt: skip
        s.add(batch)
        s.flush()
        pairs = [(p, f) for p in prods for f in facs[:10]]
        pos, dem, rec = [], [], []
        for p, f in pairs:
            base = rng.uniform(2, 20)
            pos.append(dict(product_id=p.id, facility_id=f.id, batch_id=batch.id, row_hash="h", observed_at=now - timedelta(hours=6), unit="EA", on_hand=round(base * rng.uniform(1, 90), 1), provenance=Provenance.SYNTHETIC_DEMO, source_label="bench", created_at=now))  # fmt: skip
            for i in range(DEMAND_DAYS):
                d0 = now.date() - timedelta(days=i + 1)
                dem.append(dict(product_id=p.id, facility_id=f.id, batch_id=batch.id, kind=DemandKind.CONSUMPTION, period_start=d0, period_end=d0 + timedelta(days=1), quantity=round(base * rng.uniform(0.7, 1.3), 2), unit="EA", row_hash=f"d{i}", provenance=Provenance.SYNTHETIC_DEMO, source_label="bench", created_at=now))  # fmt: skip
            if rng.random() < 0.33:
                rec.append(dict(product_id=p.id, facility_id=f.id, supplier_id=rng.choice(sup_ids), batch_id=batch.id, reference=f"BENCH-PO-{p.id}-{f.id}", status=ReceiptStatus.CONFIRMED, quantity=round(base * 30, 1), unit="EA", expected_date=now.date() + timedelta(days=rng.randint(2, 40)), observed_at=now - timedelta(hours=6), row_hash="r", provenance=Provenance.SYNTHETIC_DEMO, source_label="bench", created_at=now))  # fmt: skip
        s.bulk_insert_mappings(InventoryPosition, pos)
        s.bulk_insert_mappings(DemandObservation, dem)
        s.bulk_insert_mappings(InventoryReplenishment, rec)
    events = [
        {"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": i, "duration_days": 14}
        for i in sup_ids[:3]
    ]
    sid = new_scenario(client, events, downstream_depth=4)
    run = sruns.run_now(
        client.app.state.session_factory,
        sid,
        "bench",
        sruns.RunLimits(max_depth=4, max_nodes=2000, max_edges=5000, time_budget_seconds=120),
    )
    ia = analyze(client, run.id)
    lim = runs.Limits(InvLimits(max_pairs=1000, time_budget_seconds=120))
    conf = cfg.InventoryConfig(as_of=utcnow(), horizon_days=HORIZON)
    t0 = time.perf_counter()
    base = runs.analyze_now(client.app.state.session_factory, "bench", lim, conf)
    t_base = time.perf_counter() - t0
    t0 = time.perf_counter()
    scen = runs.analyze_now(client.app.state.session_factory, "bench", lim, conf, impact_analysis_id=ia.id)
    t_scen = time.perf_counter() - t0
    print(
        f"BENCH9 seed={SEED} products={PRODUCTS} facilities=10 pairs={len(pairs)} positions={len(pos)} demand_obs={len(dem)} receipts={len(rec)} "
        f"horizon_days={HORIZON} impact_events=3 baseline_s={t_base:.2f} scenario_s={t_scen:.2f} base_status={base.status.value} scen_status={scen.status.value} "
        f"by_status={base.summary['by_status']} by_change={scen.summary.get('by_change')}"
    )  # fmt: skip
    assert base.status.value in ("COMPLETED", "PARTIAL") and scen.status.value in ("COMPLETED", "PARTIAL")
    assert base.summary["pairs_analysed"] == len(pairs) == scen.summary["pairs_analysed"]
    assert base.input_hash != scen.input_hash  # the scenario input is part of the hash
    again = runs.analyze_now(
        client.app.state.session_factory, "bench", lim, conf.model_copy(update={"as_of": base.as_of})
    )
    assert again.input_hash == base.input_hash  # same records and settings reproduce the same input hash
