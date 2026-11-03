"""Delivery-delay scenarios on the projection. Synthetic data only; a what-if, not a prediction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError
from app.inventory import config as icfg
from app.inventory import runs as iruns
from app.models.delivery import DeliveryAssessment
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.product import Product
from tests.inventory_helpers import code, findings_of, populate, run_baseline, world


def _conf(**kw):
    return icfg.InventoryConfig(as_of=datetime.now(timezone.utc), **kw)


def _f(client, w, a):
    return findings_of(client, a.id)[(code(w, "p1"), code(w, "f1"))]


def _assessment(client, w, *, est_days: float, status="ASSESSED", ref="PO-1", with_arrival=True):
    now = datetime.now(timezone.utc)
    with Session(client.app.state.engine) as s:
        pid = s.scalars(select(Product.id).where(Product.product_code == code(w, "p1"))).first()
        fid = s.scalars(select(Facility.id).where(Facility.facility_code == code(w, "f1"))).first()
        arr = {"status": "estimated", "estimated_arrival": (now + timedelta(days=est_days)).replace(tzinfo=None).isoformat(), "window": {"earliest": None, "latest": (now + timedelta(days=est_days + 3)).replace(tzinfo=None).isoformat(), "level": 0.8}} if with_arrival else {"status": "not_calculable"}  # fmt: skip
        a = DeliveryAssessment(
            assessment_uid=f"DA-T-{status}-{est_days}", subject="DELIVERY", delivery_ref=ref, product_id=pid,
            destination_facility_id=fid, prediction_point="DISPATCH", as_of=now, status=status, commitment="ORIGINAL",
            arrival=arr, input_hash="h" * 64, requested_by="tester", algorithm_version="lt-1",
        )  # fmt: skip
        s.add(a)
        s.commit()
        return a.id


def test_baseline_unchanged_without_scenario(client):
    w = world(client)
    populate(client, w)
    f = _f(client, w, run_baseline(client))
    assert "leadtime_effect" not in f.projection


def test_uniform_delay_reports_effect_beside_unchanged_baseline(client):
    w = world(client)
    populate(client, w)
    base = _f(client, w, run_baseline(client))
    a = run_baseline(client, _conf(receipt_delay_days=10))
    assert a.status is SimStatus.COMPLETED, (a.error, a.failures)
    f = _f(client, w, a)
    eff = f.projection["leadtime_effect"]
    assert eff["scenario"] == "UNIFORM_DELAY" and eff["delay_days"] == 10
    assert (f.status, f.reason) == (base.status, base.reason)  # baseline classification is not rewritten
    assert f.projection["series"] == base.projection["series"]
    assert eff["baseline"]["stockout_within_day"] == 30
    assert eff["with_delay"]["stockout_within_day"] == 10 and eff["comparison"]["potential_stockout"] is True
    dos = eff["comparison"]["days_out_of_stock"]
    assert (
        dos["with_delay"] > dos["baseline"]
    )  # same total shortfall at the horizon end, but 4+ extra days without stock
    assert eff["receipt_adjustments"][0]["shift_days"] == 10
    assert any("conditional what-if" in x for x in eff["statements"])


def test_fractional_delay_rounds_up_and_beyond_horizon_flagged(client):
    w = world(client)
    populate(client, w)
    f = _f(client, w, run_baseline(client, _conf(receipt_delay_days=2.2, horizon_days=30)))
    adj = f.projection["leadtime_effect"]["receipt_adjustments"][0]
    assert adj["shift_days"] == 3 and adj["beyond_horizon"] is False
    f = _f(client, w, run_baseline(client, _conf(receipt_delay_days=60, horizon_days=30)))
    assert f.projection["leadtime_effect"]["receipt_adjustments"][0]["beyond_horizon"] is True


def test_assessment_driven_shift_and_provenance(client):
    w = world(client)
    populate(client, w)
    aid = _assessment(client, w, est_days=12)
    a = run_baseline(client, _conf(delivery_assessment_ids=[aid]))
    assert a.status is SimStatus.COMPLETED, (a.error, a.failures)
    eff = _f(client, w, a).projection["leadtime_effect"]
    adj = eff["receipt_adjustments"][0]
    assert eff["scenario"] == "ASSESSED_ARRIVALS" and adj["assessment_id"] == aid and adj["shift_days"] >= 6
    assert eff["versions"]["assessment_ids"] == [aid] and eff["receipts_matched_to_assessments"] == 1
    up = _f(
        client, w, run_baseline(client, _conf(delivery_assessment_ids=[aid], delivery_assessment_basis="UPPER_BOUND"))
    )
    assert up.projection["leadtime_effect"]["receipt_adjustments"][0]["adjusted_day"] > adj["adjusted_day"]


def test_unmatched_receipt_is_left_unchanged(client):
    w = world(client)
    populate(client, w)
    aid = _assessment(client, w, est_days=12, ref="PO-OTHER")
    eff = _f(client, w, run_baseline(client, _conf(delivery_assessment_ids=[aid]))).projection["leadtime_effect"]
    assert eff["receipt_adjustments"][0]["shift_days"] == 0 and eff["receipts_matched_to_assessments"] == 0
    assert "no matching assessment" in eff["receipt_adjustments"][0]["basis"]


def test_validation(client):
    w = world(client)
    populate(client, w)
    with pytest.raises(ValidationError):
        _conf(receipt_delay_days=3, delivery_assessment_ids=[1])
    with pytest.raises(ValidationError):
        _conf(receipt_delay_days=-1)
    bad = _assessment(client, w, est_days=1, status="UNAVAILABLE", with_arrival=False)
    with Session(client.app.state.engine) as s:
        with pytest.raises(DomainValidationError):
            iruns.create_analysis(s, _conf(delivery_assessment_ids=[bad]), "tester", iruns.Limits())
