"""Build and exercise a SYNTHETIC multi-facility demo world in its own database, using ATLAS's real services.

Usage (repo root, venv active):
    python scripts/demo_world.py                 # create/refresh data/demo_world/atlas_demo.db and run the pipeline
    python scripts/demo_world.py --reset         # delete that demo database file first (the only deletion it does)
    python scripts/demo_world.py --db PATH       # use another SQLite file (never the main application database)

Everything is invented and every record name starts with "DEMO ". The script writes only to the demo database,
refuses production-like environments, and prints what the engines actually persisted. Nothing here is a
prerecorded result: risk, scenario, impact, stockout and replenishment figures come from the platform's engines.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.db.migrations_check import alembic_config  # noqa: E402
from app.impact import runs as iruns  # noqa: E402
from app.inventory import config as icfg  # noqa: E402
from app.inventory import ingest, runs as invruns  # noqa: E402
from app.invopt import data as invdata, engine as invengine  # noqa: E402
from app.invopt.config import Limits, ReplenishRequest  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.enums import Provenance, RiskRunScope  # noqa: E402
from app.models.inventory import ImportKind  # noqa: E402
from app.risk import runs as riskruns  # noqa: E402
from app.risk.methodology import risk_config  # noqa: E402
from app.scenarios import runs as sruns  # noqa: E402
from app.scenarios import service  # noqa: E402

REG, GRAPH = "/api/v1/registry", "/api/v1/graph"


def _csv(header: list[str], rows: list[list]) -> bytes:
    return ("\n".join([",".join(header)] + [",".join("" if c is None else str(c) for c in r) for r in rows]) + "\n").encode()


def _v(x: object) -> object:
    """Enum value for printing (str() of a str-Enum differs between Python 3.10 and 3.11+)."""
    return getattr(x, "value", x)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "demo_world" / "atlas_demo.db"))
    ap.add_argument("--reset", action="store_true", help="delete the demo database file first")
    args = ap.parse_args()
    db_path = Path(args.db).resolve()
    if db_path.name == "atlas.db":
        print("Refusing to use a file named atlas.db: the demo must never share the application database.", file=sys.stderr)
        return 2
    if args.reset and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path.as_posix()}"
    settings = Settings(_env_file=None, environment="development", database_url=url, data_dir=db_path.parent,
                        log_level="WARNING", write_api_enabled=True, alerting_tick_seconds=0)
    if settings.production_like:
        print("Refusing to build demo data in a production-like environment.", file=sys.stderr)
        return 2
    command.upgrade(alembic_config(url), "head")
    app = create_app(settings)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out: dict[str, object] = {}

    with TestClient(app) as c:
        def mk(kind: str, **f):
            base = {"suppliers": {"country_or_region": "Germany", "category": "Electronics"},
                    "facilities": {"country_or_region": "Germany", "facility_type": "MANUFACTURING"},
                    "products": {"category": "Components", "unit_of_measure": "EA"}}[kind]
            r = c.post(f"{REG}/{kind}", json=base | f)
            assert r.status_code == 201, r.text
            return r.json() | {"type": {"suppliers": "SUPPLIER", "facilities": "FACILITY", "products": "PRODUCT"}[kind]}

        def rel(typ: str, s: dict, d: dict, **kw):
            body = {"type": typ, "source_type": s["type"], "source_id": s["id"], "destination_type": d["type"],
                    "destination_id": d["id"], "status": "CONFIRMED", "justification": "DEMO synthetic relationship"} | kw
            r = c.post(f"{GRAPH}/relationships", json=body)
            assert r.status_code == 201, r.text

        # ---- network: three suppliers (one is the available alternative), two plants, a hub, two products ----
        sup = {k: mk("suppliers", name=n, country_or_region=cn) for k, n, cn in [
            ("core", "DEMO Meridian Metals", "Germany"), ("alt", "DEMO Alt-Source Micro", "Poland"),
            ("elec", "DEMO Summit Electronics", "Vietnam")]}
        fac = {k: mk("facilities", name=n, facility_type=ft) for k, n, ft in [
            ("plant", "DEMO Plant Alpha", "MANUFACTURING"), ("assembly", "DEMO Assembly Beta", "MANUFACTURING"),
            ("hub", "DEMO Distribution Hub Gamma", "WAREHOUSE")]}
        prod = {k: mk("products", name=n) for k, n in [("ctl", "DEMO Controller Unit"), ("sen", "DEMO Sensor Module")]}
        rel("SUPPLIES", sup["core"], fac["plant"], supply_share_pct=70.0, criticality="HIGH", confidence=0.9)
        rel("SUPPLIES", sup["alt"], fac["plant"], supply_share_pct=30.0, confidence=0.8)
        # inbound shares per receiving facility must sum to 100%: Assembly Beta takes 40% from Summit, 60% from Plant Alpha
        rel("SUPPLIES", sup["elec"], fac["assembly"], supply_share_pct=40.0, confidence=0.9)
        rel("TRANSFERS_TO", fac["plant"], fac["assembly"], supply_share_pct=60.0)
        rel("TRANSFERS_TO", fac["assembly"], fac["hub"], supply_share_pct=100.0)
        rel("PRODUCES", fac["assembly"], prod["ctl"], supply_share_pct=100.0)
        rel("PRODUCES", fac["plant"], prod["sen"], supply_share_pct=100.0)
        rel("STORES", fac["hub"], prod["ctl"])
        # The registry API never lets a client choose provenance, so it records USER_PROVIDED. This script owns its
        # demo database: relabel what it just created as SYNTHETIC_DEMO before any engine reads it.
        from sqlalchemy import update

        from app.models import Facility, Product, Supplier, SupplyRelationship
        with app.state.session_factory() as s, s.begin():
            for model in (Supplier, Facility, Product, SupplyRelationship):
                s.execute(update(model).values(provenance=Provenance.SYNTHETIC_DEMO))
        net = c.get("/api/v1/network/summary").json()
        out["network"] = {k: net.get(k) for k in ("supplier_count", "facility_count", "relationship_count")}

        # ---- inventory, demand, inbound receipts (CSV through the real ingestion engine) ----
        def code(rec):
            return rec["code"]

        def d(days_ago: int) -> str:
            return (now.date() - timedelta(days=days_ago)).isoformat()

        def load(kind, header, rows):
            with app.state.session_factory() as s, s.begin():
                batch, _created, _p = ingest.commit(
                    s, kind, _csv(header, rows), source_label="DEMO synthetic", provenance=Provenance.SYNTHETIC_DEMO,
                    requested_by="demo", file_name="demo.csv", atomic=False, fmt="CSV", encoding=None, delimiter=",",
                    mapping=None, date_format="%Y-%m-%d", max_rows=10_000)
                return batch.id

        obs = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        inv_hdr = ["product_code", "facility_code", "observed_at", "unit", "on_hand", "reserved", "allocated",
                   "quarantined", "damaged", "in_transit", "backordered"]
        load(ImportKind.INVENTORY, inv_hdr, [
            [code(prod["ctl"]), code(fac["hub"]), obs, "EA", 140, 0, 0, 0, 0, "", ""],      # near stockout at 12/day
            [code(prod["sen"]), code(fac["plant"]), obs, "EA", 4000, 0, 0, 0, 0, "", ""]])  # comfortable cover
        dem_hdr = ["product_code", "facility_code", "kind", "period_start", "period_end", "quantity", "unit"]
        rows = []
        for i in range(90):  # rising demand for the controller: 8/day growing to ~14/day
            rows.append([code(prod["ctl"]), code(fac["hub"]), "CONSUMPTION", d(i + 1), d(i), round(14 - i * 0.065, 2), "EA"])
            rows.append([code(prod["sen"]), code(fac["plant"]), "CONSUMPTION", d(i + 1), d(i), 20, "EA"])
        load(ImportKind.DEMAND, dem_hdr, rows)
        # observed_at must be set: without it the import time is recorded, which is later than `now`, and every
        # as-of-`now` analysis would correctly treat the order as not yet known and leave it out.
        load(ImportKind.REPLENISHMENT,
             ["product_code", "facility_code", "observed_at", "reference", "status", "quantity", "unit", "expected_date",
              "supplier_code"],
             [[code(prod["ctl"]), code(fac["hub"]), obs, "DEMO-PO-1", "CONFIRMED", 150, "EA", d(-6), code(sup["elec"])]])

        # ---- risk assessment over the network (rule-based, versioned methodology) ----
        run = riskruns.assess_now(app.state.session_factory, RiskRunScope.NETWORK, None, "demo",
                                  risk_config(settings.risk_config_path))
        out["risk_run"] = {"id": run.id, "status": _v(run.status)}

        # ---- disruption scenario: the core supplier becomes unavailable for 21 days ----
        with app.state.session_factory() as s, s.begin():
            sc = service.create_scenario(s, "demo", name="DEMO Core supplier outage", description=None,
                                         scenario_type=service.ScenarioType.CUSTOM, assumptions=None, notes=None,
                                         template_key=None, max_scenarios=100)
            service.add_event(s, "demo", sc, {"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER",
                                              "target_id": sup["core"]["id"], "duration_days": 21}, None, 25)
            scenario_id = sc.id
        srun = sruns.run_now(app.state.session_factory, scenario_id, "demo", sruns.RunLimits())
        impact = iruns.analyze_now(app.state.session_factory, srun.id, "demo", iruns.AnalysisLimits(), None)
        out["scenario"] = {"scenario_id": scenario_id, "run_id": srun.id, "impact_analysis_id": impact.id}

        # ---- baseline stockout analysis and scenario-aware stockout analysis ----
        base = invruns.analyze_now(app.state.session_factory, "demo", invruns.Limits(), icfg.InventoryConfig(as_of=now))
        out["stockout_baseline_id"] = base.id

        # ---- replenishment engine for the controller at the hub ----
        req = ReplenishRequest(product_id=prod["ctl"]["id"], facility_id=fac["hub"]["id"], lead_time_days=6,
                               lead_time_std_days=1.0, replications=30, simulation_days=90, seed=7)
        with app.state.session_factory() as s:
            snap = invdata.load(s, req, now, Limits())
        res = invengine.compute(snap, req, Limits())
        rec = res.get("recommendation") or {}
        out["replenishment"] = {"result_status": res.get("result_status"), "status": rec.get("status"),
                                "order_now": rec.get("order_now"), "inventory_position": rec.get("inventory_position"),
                                "reorder_point": (rec.get("policy") or {}).get("reorder_point"),
                                "infeasibility": res.get("infeasibility")}

        # ---- alerting: two rules over the engines' own outputs, evaluated once through the API ----
        AL = "/api/v1/alerting"
        for body in (
            {"name": "DEMO High supplier risk", "detector": "supplier_risk_score", "severity": "HIGH",
             "params": {"operator": "gte", "threshold": 50}},
            {"name": "DEMO Projected stockout", "detector": "stockout_status", "severity": "MEDIUM",
             "params": {"statuses": ["current_shortage", "projected_stockout"]}},
        ):
            r = c.post(f"{AL}/rules", json=body)
            assert r.status_code == 201, r.text
        ev = c.post(f"{AL}/evaluate", json={})
        assert ev.status_code == 200, ev.text
        alerts = c.get(f"{AL}/alerts").json()["items"]
        out["alerts"] = [{"severity": a.get("severity"), "title": a.get("title"), "basis": a.get("basis")} for a in alerts]

        # ---- what was actually persisted ----
        from app.models import SupplyRelationship
        from app.models.impact import ImpactFinding
        from app.models.inventory import StockoutFinding
        from app.models.risk import RiskAssessment
        with Session(app.state.engine) as s:
            out["risk_assessments"] = [
                {"supplier_id": r.supplier_id, "score": r.risk_score, "class": _v(r.classification), "status": _v(r.status),
                 "reliability": _v(r.reliability_band)}
                for r in s.scalars(select(RiskAssessment).where(RiskAssessment.is_current.is_(True)).order_by(RiskAssessment.supplier_id))]
            out["stockout_findings"] = [
                {"product": f.product_name, "facility": f.facility_name, "status": _v(f.status), "coverage_days": f.coverage_days}
                for f in s.scalars(select(StockoutFinding).where(StockoutFinding.analysis_id == base.id))]
            out["persisted"] = {
                "relationships": s.scalar(select(func.count()).select_from(SupplyRelationship)),
                "impact_findings": s.scalar(select(func.count()).select_from(ImpactFinding)
                                            .where(ImpactFinding.analysis_id == impact.id)),
            }

    print(f"DEMO WORLD (synthetic) built in {db_path}")
    for k, v in out.items():
        print(f"  {k}: {v}")
    print("To clean up: delete that file (or rerun with --reset). The application database is never touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
