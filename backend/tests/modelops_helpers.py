"""SYNTHETIC fixtures for model-governance tests. Every number is invented; results verify software only."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leadtime import runs as lt_runs
from app.leadtime.assess import AssessRequest, assess
from app.leadtime.config import LeadTimeConfig, LeadTimeLimits
from app.modelops import registry
from app.models.facility import Facility
from app.models.modelops import ModelVersion
from app.models.product import Product
from tests.leadtime_helpers import NOW, load_world_deliveries

LIM = LeadTimeLimits()


def train(client, **kw):
    return lt_runs.train_now(client.app.state.session_factory, LeadTimeConfig(**kw), "tester", LIM)


def leadtime_env(client, n: int = 300):
    """Imports synthetic deliveries and trains both lead-time targets. Returns (code_map, lead_run, risk_run)."""
    _w, cm, _rows = load_world_deliveries(client, n=n)
    lead = train(client, target="LEAD_TIME")
    risk = train(client, target="LATE_RISK")
    return cm, lead, risk


def sync(client, actor: str = "tester") -> dict:
    with Session(client.app.state.engine) as s:
        out = registry.sync(s, actor)
        s.commit()
        return out


def version_of(client, source_ref: str) -> ModelVersion:
    with Session(client.app.state.engine) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == source_ref))
        s.expunge(mv)
        return mv


def ids(client, cm):
    from app.models.supplier import Supplier

    with Session(client.app.state.engine) as s:
        return (
            s.scalar(select(Product.id).where(Product.product_code == cm["P1"])),
            s.scalar(select(Facility.id).where(Facility.facility_code == cm["F1"])),
            s.scalar(select(Supplier.id).where(Supplier.supplier_code == cm["S1"])),
        )


def issue_assessments(client, cm, run, n: int = 5):
    """Issue assessments for deliveries that are already complete in the synthetic history (so outcomes exist)."""
    pid, _fid, _sid = ids(client, cm)
    out = []
    with Session(client.app.state.engine) as s:
        from app.models.delivery import DeliverySnapshot

        refs = [
            r
            for (r,) in s.execute(
                select(DeliverySnapshot.delivery_ref).where(DeliverySnapshot.product_id == pid).distinct().limit(n)
            )
        ]
        for ref in refs:
            a, _ = assess(
                s,
                AssessRequest(delivery_ref=ref, product_id=pid, leadtime_run_id=run.id, as_of=NOW - timedelta(days=1)),
                "tester",
                now=NOW,
            )
            out.append(a.assessment_uid)
        s.commit()
    return out


def mature_assessments(client, cm, lead, risk, n: int = 40):
    """Issue assessments for open synthetic orders, then record their arrivals (some late). Returns the assessment uids."""
    import random

    from app.models.delivery import DeliveryImportKind
    from tests.leadtime_helpers import csv_bytes, do_import, iso

    rng = random.Random(11)
    base_t = NOW - timedelta(days=60)
    base = {
        "product_code": cm["P1"],
        "destination_facility_code": cm["F1"],
        "supplier_code": cm["S1"],
        "quantity": 100.0,
        "unit": "EA",
    }
    opens = []
    for i in range(n):
        opens.append({**base, "delivery_ref": f"PO-M{i:03d}", "order_at": iso(base_t - timedelta(days=2)), "confirmed_at": iso(base_t - timedelta(days=2) + timedelta(hours=5)), "original_promised_at": iso(base_t + timedelta(days=9)), "status": "CONFIRMED", "observed_at": iso(base_t - timedelta(hours=1)), "transport_mode": "ROAD"})  # fmt: skip
    do_import(client, DeliveryImportKind.DELIVERIES, csv_bytes(opens))
    pid, _f, _sup = ids(client, cm)
    uids = []
    with Session(client.app.state.engine) as s:
        for o in opens:
            a, _ = assess(
                s,
                AssessRequest(
                    delivery_ref=o["delivery_ref"],
                    product_id=pid,
                    leadtime_run_id=lead.id,
                    risk_run_id=risk.id,
                    as_of=base_t,
                ),
                "tester",
                now=NOW,
            )
            if a.status == "ASSESSED":
                uids.append(a.assessment_uid)
        s.commit()
    done = []
    for o in opens:
        late = rng.random() < 0.4
        arrive = base_t + timedelta(days=rng.uniform(4, 8) if not late else rng.uniform(11, 16))
        done.append({**o, "dispatched_at": iso(base_t + timedelta(days=1)), "arrived_at": iso(arrive), "received_at": iso(arrive + timedelta(hours=3)), "status": "DELIVERED", "observed_at": iso(arrive + timedelta(hours=4))})  # fmt: skip
    do_import(client, DeliveryImportKind.DELIVERIES, csv_bytes(done))
    return uids
