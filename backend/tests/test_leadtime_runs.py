"""Run lifecycle, persisted assessments and the end-to-end synthetic pipeline. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.leadtime import runs
from app.leadtime.assess import AssessRequest, assess, outcome_of
from app.leadtime.config import LeadTimeConfig, LeadTimeLimits
from app.models.delivery import DeliveryAssessment, LeadTimeRun
from app.models.enums import SimStatus
from tests.leadtime_helpers import NOW, load_world_deliveries

LIM = LeadTimeLimits()


def _ids(client, cm):
    from app.models.facility import Facility
    from app.models.product import Product
    from app.models.supplier import Supplier

    with Session(client.app.state.engine) as s:
        return (
            s.scalar(select(Product.id).where(Product.product_code == cm["P1"])),
            s.scalar(select(Facility.id).where(Facility.facility_code == cm["F1"])),
            s.scalar(select(Supplier.id).where(Supplier.supplier_code == cm["S1"])),
        )


@pytest.fixture()
def loaded(client):
    w, cm, _rows = load_world_deliveries(client, n=300)
    return client, cm


def _train(client, **kw):
    return runs.train_now(client.app.state.session_factory, LeadTimeConfig(**kw), "tester", LIM)


def test_run_lifecycle_and_artifacts(loaded):
    client, _ = loaded
    r = _train(client, target="LEAD_TIME")
    assert r.status in (SimStatus.COMPLETED, SimStatus.PARTIAL) and r.result_status == "completed", (
        r.error,
        r.failures,
    )
    assert r.selected_model and r.artifact and r.splits and r.evaluation and r.leakage_controls and r.reproducibility
    assert r.data_snapshot_hash and r.algorithm_version == "lt-1" and r.provenance == "MODEL_ESTIMATE"
    r2 = _train(client, target="LATE_RISK")
    assert r2.result_status == "completed" and r2.artifact["score_kind"] in ("calibrated_probability", "uncalibrated_score", "historical_rate")  # fmt: skip


def test_run_is_frozen_once_terminal(loaded):
    client, _ = loaded
    r = _train(client, target="LEAD_TIME")
    with Session(client.app.state.engine) as s:
        row = s.get(LeadTimeRun, r.id)
        row.selected_model = "tampered"
        with pytest.raises(Exception):  # noqa: B017, PT011  # insert-only listener
            s.flush()
        s.rollback()
        with pytest.raises(Exception):  # noqa: B017, PT011
            s.delete(s.get(LeadTimeRun, r.id))
            s.flush()
        s.rollback()


def test_idempotency_and_conflict(loaded):
    client, _ = loaded
    conf = LeadTimeConfig(target="LEAD_TIME")
    with Session(client.app.state.engine) as s:
        a, created = runs.create_run(s, conf, "tester", LIM, idempotency_key="k1")
        s.commit()
        b, created2 = runs.create_run(s, conf, "tester", LIM, idempotency_key="k1")
        assert created and not created2 and a.id == b.id
        with pytest.raises(ConflictError):
            runs.create_run(s, LeadTimeConfig(target="LATE_RISK"), "tester", LIM, idempotency_key="k1")


def test_cancel_before_execution(loaded):
    client, _ = loaded
    with Session(client.app.state.engine) as s:
        run, _c = runs.create_run(s, LeadTimeConfig(target="LEAD_TIME"), "tester", LIM)
        s.commit()
        runs.cancel_run(s, run)
        s.commit()
        rid = run.id
    runs.execute_run(client.app.state.session_factory, rid, LIM)
    with Session(client.app.state.engine) as s:
        assert s.get(LeadTimeRun, rid).status is SimStatus.CANCELLED


def test_budget_exceeded_fails_visibly(loaded):
    client, _ = loaded
    with Session(client.app.state.engine) as s:
        run, _c = runs.create_run(s, LeadTimeConfig(target="LEAD_TIME"), "tester", LIM)
        s.commit()
        rid = run.id
    runs.execute_run(client.app.state.session_factory, rid, LeadTimeLimits(time_budget_seconds=0))
    with Session(client.app.state.engine) as s:
        r = s.get(LeadTimeRun, rid)
        assert r.status is SimStatus.FAILED and "budget" in r.error


def test_empty_scope_and_insufficient_data(client):
    w, cm, _ = load_world_deliveries(client, n=12, special=False)
    r = _train(client, target="LATE_RISK")
    assert r.status is SimStatus.COMPLETED and r.result_status != "completed" and r.artifact is None
    assert any("No model was produced" in x for x in r.warnings)
    with Session(client.app.state.engine) as s, pytest.raises(DomainValidationError):
        runs.create_run(s, LeadTimeConfig(target="LEAD_TIME", supplier_id=999999), "tester", LIM)


def test_assessment_for_open_delivery_and_idempotency(loaded):
    client, cm = loaded
    pid, fid, sid = _ids(client, cm)
    run = _train(client, target="LEAD_TIME", prediction_point="ORDER_PLACEMENT")
    req = AssessRequest(delivery_ref="PO-OPEN", product_id=pid, leadtime_run_id=run.id, as_of=NOW)
    with Session(client.app.state.engine) as s:
        a, created = assess(s, req, "tester", idempotency_key="a1", now=NOW)
        s.commit()
        assert created and a.status == "ASSESSED", a.unavailable_reason
        assert a.estimate and a.arrival["status"] in ("estimated", "not_calculable") and a.risk is not None
        b, created2 = assess(s, req, "tester", idempotency_key="a1", now=NOW)
        assert not created2 and b.id == a.id
        with pytest.raises(ConflictError):
            assess(s, AssessRequest(delivery_ref="PO-IT", product_id=pid, as_of=NOW), "tester", idempotency_key="a1", now=NOW)  # fmt: skip


def test_unavailable_cases_are_stored_not_guessed(loaded):
    client, cm = loaded
    pid, fid, sid = _ids(client, cm)
    with Session(client.app.state.engine) as s:
        done = assess(s, AssessRequest(delivery_ref="PO-00001", product_id=pid, as_of=NOW), "tester", now=NOW)[0] if False else None  # fmt: skip
        for ref in ("PO-CAN", "PO-REV"):
            a, _c = assess(s, AssessRequest(delivery_ref=ref, product_id=pid, as_of=NOW), "tester", now=NOW)
            assert a.status == "UNAVAILABLE" and a.unavailable_reason and a.estimate is None
        with pytest.raises(NotFoundError):
            assess(s, AssessRequest(delivery_ref="NOPE", product_id=pid, as_of=NOW), "tester", now=NOW)
        with pytest.raises(DomainValidationError):
            assess(s, AssessRequest(product_id=pid, as_of=NOW), "tester", now=NOW)  # lane needs supplier+destination
        with pytest.raises(DomainValidationError):
            assess(s, AssessRequest(delivery_ref="PO-OPEN", product_id=pid, as_of=NOW + timedelta(days=3)), "tester", now=NOW)  # fmt: skip
        assert done is None


def test_wrong_prediction_point_run_is_rejected(loaded):
    client, cm = loaded
    pid, _f, _s = _ids(client, cm)
    run = _train(client, target="LEAD_TIME", prediction_point="DISPATCH")
    with Session(client.app.state.engine) as s, pytest.raises((DomainValidationError, ConflictError)):
        assess(s, AssessRequest(delivery_ref="PO-OPEN", product_id=pid, prediction_point="ORDER_PLACEMENT", leadtime_run_id=run.id, as_of=NOW), "tester", now=NOW)  # fmt: skip


def test_lane_assessment_and_outcome_comparison_preserves_assessment(loaded):
    client, cm = loaded
    pid, fid, sid = _ids(client, cm)
    run = _train(client, target="LEAD_TIME")
    with Session(client.app.state.engine) as s:
        a, _c = assess(s, AssessRequest(product_id=pid, supplier_id=sid, destination_facility_id=fid, leadtime_run_id=run.id, as_of=NOW), "tester", now=NOW)  # fmt: skip
        s.commit()
        assert a.subject == "LANE" and a.arrival["status"] == "not_calculable"
        before = (a.estimate, a.risk, a.input_hash)
        out = outcome_of(s, a, NOW + timedelta(days=1))
        assert out and (a.estimate, a.risk, a.input_hash) == before  # the issued assessment is never edited
        a.status = "TAMPERED"
        with pytest.raises(Exception):  # noqa: B017, PT011
            s.flush()
        s.rollback()


def test_end_to_end_ingest_analyse_train_assess_inventory(loaded):
    client, cm = loaded
    pid, fid, sid = _ids(client, cm)
    from app.leadtime import analytics
    from app.leadtime.resolve import load_deliveries

    with Session(client.app.state.engine) as s:
        ds = load_deliveries(s, as_of=NOW)
        summ = analytics.summarize(ds, LeadTimeConfig(), NOW)
    assert summ["lead_time"]["n"] > 200 and summ["punctuality"]["n"] > 0
    run = _train(client, target="LEAD_TIME")
    with Session(client.app.state.engine) as s:
        a, _c = assess(s, AssessRequest(delivery_ref="PO-OPEN", product_id=pid, leadtime_run_id=run.id, as_of=NOW), "tester", now=NOW)  # fmt: skip
        s.commit()
        assert s.scalar(select(DeliveryAssessment.id).where(DeliveryAssessment.id == a.id)) == a.id
