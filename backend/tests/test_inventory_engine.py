from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.inventory import config as cfg
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.inventory import InventoryPosition
from app.models.product import Product
from tests.inventory_helpers import (
    NOW,
    code,
    daily_demand,
    findings_of,
    iso,
    load_demand,
    load_inventory,
    populate,
    run_baseline,
    scenario_impact,
    world,
)  # fmt: skip


def test_baseline_end_to_end(client):
    w = world(client)
    populate(client, w)
    a = run_baseline(client)
    assert a.status is SimStatus.COMPLETED, (a.error, a.failures)
    f = findings_of(client, a.id)
    assert len(f) == 3
    k1 = (code(w, "p1"), code(w, "f1"))
    assert f[k1].coverage_days["status"] == "calculable"
    assert abs(f[k1].coverage_days["days"] - 10) < 0.01
    assert f[(code(w, "p2"), code(w, "f1"))].status in ("unknown", "insufficient_data")
    assert f[(code(w, "p1"), code(w, "f2"))].status == "within_policy" or f[(code(w, "p1"), code(w, "f2"))].status in (
        "unknown",
    )


def test_counts_unchanged_and_reproducible(client):
    w = world(client)
    populate(client, w)
    conf = cfg.InventoryConfig(as_of=datetime.now(timezone.utc))
    a1, a2 = run_baseline(client, conf), run_baseline(client, conf)
    assert a1.input_hash == a2.input_hash
    f1, f2 = findings_of(client, a1.id), findings_of(client, a2.id)
    assert {k: v.status for k, v in f1.items()} == {k: v.status for k, v in f2.items()}


def test_scenario_changes_projection_and_canonical_unchanged(client):
    w = world(client)
    populate(client, w)
    with Session(client.app.state.engine) as s:
        before = (
            s.scalar(select(func.count()).select_from(InventoryPosition)),
            s.scalar(select(func.count()).select_from(Product)),
            s.scalar(select(func.count()).select_from(Facility)),
        )
    iid = scenario_impact(client, w)
    base = run_baseline(client)
    sc = run_baseline(client, impact_analysis_id=iid)
    assert sc.status in (SimStatus.COMPLETED, SimStatus.PARTIAL), sc.error
    fb, fs = findings_of(client, base.id), findings_of(client, sc.id)
    k = (code(w, "p1"), code(w, "f1"))
    assert fs[k].scenario is not None
    assert fs[(code(w, "p1"), code(w, "f2"))].scenario is None or fs[(code(w, "p1"), code(w, "f2"))].change in (
        None,
        "UNCHANGED",
        "UNAFFECTED",
    )
    with Session(client.app.state.engine) as s:
        after = (
            s.scalar(select(func.count()).select_from(InventoryPosition)),
            s.scalar(select(func.count()).select_from(Product)),
            s.scalar(select(func.count()).select_from(Facility)),
        )
    assert before == after
    assert fb[k].id != fs[k].id


def test_new_snapshot_does_not_rewrite_old_analysis(client):
    w = world(client)
    populate(client, w)
    a = run_baseline(client)
    before = {k: (v.status, v.quantities) for k, v in findings_of(client, a.id).items()}
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(0.5), "EA", 5, 0, 0, 0, 0, "", ""]])
    assert {k: (v.status, v.quantities) for k, v in findings_of(client, a.id).items()} == before
    b = run_baseline(client)
    k = (code(w, "p1"), code(w, "f1"))
    assert findings_of(client, b.id)[k].quantities != before[k][1]
    assert findings_of(client, b.id)[k].projection != findings_of(client, a.id)[k].projection


def test_stale_and_missing_demand_stay_unknown(client):
    w = world(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(30), "EA", 100, 0, 0, 0, 0, "", ""]])
    load_demand(client, daily_demand(w, "p1", "f1", 10))
    a = run_baseline(client, cfg.InventoryConfig(as_of=NOW))
    f = findings_of(client, a.id)[(code(w, "p1"), code(w, "f1"))]
    assert f.status == "insufficient_data"
    assert f.projection is None or f.projection.get("status") != "projected"
