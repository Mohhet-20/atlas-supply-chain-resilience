"""Detectors read signals ATLAS already stores. These tests build the signals through the real engines wherever one exists. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.alerting import detectors as D
from app.alerting import service as S
from app.models.alerting import Alert, AlertRuleRun
from app.models.delivery import DeliveryAssessment
from app.models.enums import BatchStatus, EntityKind, IngestPolicy, Provenance, SourceFormat, SourceStatus, SourceType
from app.models.forecast import ForecastAlert
from app.models.ingestion import IngestionBatch
from app.models.source import DataSource
from tests.alerting_helpers import ctx, rule_body
from tests.decision_helpers import alt, run
from tests.forecast_helpers import forecast, synthetic_world
from tests.inventory_helpers import populate, run_baseline, world
from tests.supplier_risk_helpers import assess_all

NOW = S.utcnow()
M = timedelta(minutes=1)


@pytest.fixture
def db(client):
    with Session(client.app.state.engine) as s:
        yield s


def live(db: Session) -> list[Alert]:
    return list(db.scalars(select(Alert).order_by(Alert.id)))


def make_rule(db: Session, detector: str, params: dict, **kw):
    return S.create_rule(
        db,
        "rev",
        rule_body(
            name=f"{detector}-{len(params)}-{kw.get('name', '')}",
            detector=detector,
            params=params,
            **{k: v for k, v in kw.items() if k != "name"},
        ),
        ctx(),
    )


# ---------- supplier risk (real assessment) ----------


def test_supplier_risk_alerts_come_from_the_real_assessment_and_say_it_is_a_heuristic(client, db):
    world(client)
    assess_all(client)
    r = make_rule(db, "supplier_risk_score", {"threshold": 20}, severity="MEDIUM")
    out = S.evaluate(db, "rev", ctx(), NOW)
    a = live(db)[0]
    assert out["created"] == 1 and a.basis == "assessed" and a.detector == "supplier_risk_score"
    assert a.condition["value"] == 25.0 and a.condition["threshold"] == 20.0 and a.condition["unit"] == "score"
    assert a.evidence["assessment_uid"].startswith("RA") or a.evidence["assessment_uid"]
    assert "heuristic" in a.evidence["heuristic"].lower() or "rule-based" in a.evidence["heuristic"].lower()
    assert a.rule_version == r.version
    # below the evidence coverage the rule requires: skipped and counted, not matched
    make_rule(db, "supplier_risk_score", {"threshold": 20, "min_evidence_coverage": 0.9}, name="strict")
    S.evaluate(db, "rev", ctx(), NOW + M)
    run_row = db.scalars(select(AlertRuleRun).order_by(AlertRuleRun.id.desc())).first()
    assert run_row.detail["skipped"] == 1 and "coverage" in run_row.detail["notes"][0]


def test_supplier_without_a_score_is_unknown_never_matched_and_auto_resolve_respects_it(client, db):
    w = world(client)
    assess_all(client)
    r = make_rule(db, "supplier_risk_score", {"threshold": 20}, auto_resolve=True, severity="MEDIUM")
    S.evaluate(db, "rev", ctx(), NOW)
    assert len(live(db)) == 1
    db.execute(
        text(
            "update risk_assessments set risk_score = null, status = 'INSUFFICIENT_EVIDENCE', classification = null where is_current = TRUE"
        )
    )
    db.commit()
    out = S.evaluate(db, "rev", ctx(), NOW + 5 * M)
    a = live(db)[0]
    assert out["rules"][0]["outcome"] == "no_data" and a.status == "open"  # unknown does not clear a live alert
    db.execute(
        text(
            "update risk_assessments set risk_score = 10, status = 'ASSESSED', classification = 'LOW' where is_current = TRUE"
        )
    )
    db.commit()
    S.evaluate(db, "rev", ctx(), NOW + 10 * M)
    db.refresh(a)
    assert (a.status, a.resolution_mode, a.resolved_by) == (
        "resolved",
        "auto",
        "system",
    )  # now measured below the threshold
    assert w and r


# ---------- inventory (real stockout analysis) ----------


def test_stockout_alerts_label_observed_versus_predicted_and_refuse_stale_analyses(client, db):
    w = world(client)
    populate(client, w)
    an = run_baseline(client)
    make_rule(
        db, "stockout_status", {"statuses": ["current_shortage", "projected_stockout", "low_coverage"]}, severity="HIGH"
    )
    S.evaluate(db, "rev", ctx(), NOW)
    al = live(db)
    assert al, "the populated baseline should produce at least one finding"
    for a in al:
        assert a.evidence["analysis_uid"] == an.analysis_uid
        assert a.basis == ("observed" if a.evidence["status"] == "current_shortage" else "predicted")
        assert ("Observed" in a.summary) if a.basis == "observed" else ("projection" in a.summary)
    out = S.evaluate(db, "rev", ctx(), NOW + 30 * 24 * 60 * M)  # a month later the analysis is stale
    assert out["rules"][0]["outcome"] == "no_data" and "stale" in out["rules"][0]["notes"][0]
    assert len(live(db)) == len(al)  # nothing new


def test_no_baseline_analysis_is_no_data_not_a_clean_bill_of_health(client, db):
    world(client)
    make_rule(db, "stockout_status", {})
    out = S.evaluate(db, "rev", ctx(), NOW)
    assert out["rules"][0]["outcome"] == "no_data" and live(db) == []


# ---------- processing: technical failure versus infeasible plan ----------


def test_run_outcome_separates_technical_failure_from_an_infeasible_plan(client, db):
    run(db, "RR-OK", [alt("A", 10.0, 1.0)])
    run(db, "RR-INF", [alt("A", 10.0, 1.0)], status="infeasible")
    db.execute(
        text(
            "insert into resilience_runs (run_uid,status,result_status,requested_by,product_id,destination_facility_id,strategy,scenario_mode,request,request_hash,algorithm_version,alternatives,comparison,provenance,scenarios,queued_at,error) values ('RR-BAD','FAILED',null,'t',1,1,'PARETO','FIXED','{}','hb','x','[]','{}','{}','{}',:q,'solver crashed')"
        ),
        {"q": NOW - M},
    )
    db.commit()
    make_rule(db, "run_outcome", {"condition": "failed", "kinds": ["resilience"]}, name="tech")
    make_rule(db, "run_outcome", {"condition": "infeasible", "kinds": ["resilience"]}, name="infeasible")
    S.evaluate(db, "rev", ctx(), NOW)
    by = {a.evidence["run"]: a for a in live(db)}
    assert set(by) == {"RR-BAD", "RR-INF"}  # the completed feasible run raised nothing
    assert by["RR-BAD"].evidence["failure_class"] == "technical" and "solver crashed" in by["RR-BAD"].summary
    assert by["RR-INF"].evidence["failure_class"] == "operational" and "software did not fail" in by["RR-INF"].summary


# ---------- data quality and ingestion ----------


def _source(db: Session, ns: str = "erp") -> DataSource:
    s = DataSource(
        namespace=ns,
        name=f"{ns.upper()} feed",
        source_type=SourceType.API,
        provenance=Provenance.IMPORTED_EXTERNAL,
        status=SourceStatus.ACTIVE,
    )
    db.add(s)
    db.flush()
    return s


def _batch(db: Session, src: DataSource, n: int, status: BatchStatus, at, reason: str | None = None) -> None:
    db.add(IngestionBatch(batch_uid=f"B-{src.namespace}-{n}", fingerprint=f"f-{src.namespace}-{n}", source_id=src.id, entity_type=EntityKind.SUPPLIER, source_format=SourceFormat.CSV, mapping_profile_ref="canonical:1", policy=IngestPolicy.ATOMIC, status=status, content_sha256="0" * 64, content_bytes=10, normalization_version="n1", started_at=at, finished_at=at, failure_reason=reason))  # fmt: skip


def test_ingestion_failures_are_one_alert_per_source_not_per_batch_and_stale_unknown_is_not_stale(client, db):
    s1, s2 = _source(db, "erp"), _source(db, "wms")
    for i in range(3):
        _batch(db, s1, i, BatchStatus.FAILED, NOW - i * 60 * M, "bad header")
    _batch(db, s2, 0, BatchStatus.FAILED, NOW - M, "one-off")  # a single failure is below the threshold of 2
    _source(db, "never")  # never delivered a batch
    _batch(db, s2, 1, BatchStatus.COMMITTED, NOW - 48 * 60 * M)
    db.commit()
    make_rule(db, "source_ingest_failures", {"min_failures": 2}, name="fails")
    make_rule(db, "source_stale", {"max_age_hours": 24}, name="stale")
    out = S.evaluate(db, "rev", ctx(), NOW)
    al = live(db)
    fails = [a for a in al if a.detector == "source_ingest_failures"]
    stale = [a for a in al if a.detector == "source_stale"]
    assert (
        len(fails) == 1
        and fails[0].condition["value"] == 3
        and fails[0].evidence["top_reasons"][0]["reason"] == "bad header"
    )
    assert len(stale) == 1 and stale[0].entity_label == "WMS feed"  # the never-synced source is unknown, not stale
    assert any("never delivered" in n for r in out["rules"] for n in r["notes"])


# ---------- delivery risk ----------


def test_delivery_alerts_are_predictions_and_exclude_historical_rates(client, db):
    world(client)

    def add(i: int, risk: dict | None, status: str = "ASSESSED") -> None:
        db.add(DeliveryAssessment(assessment_uid=f"DA-{i}", subject="DELIVERY", delivery_ref=f"PO-{i}", product_id=1, supplier_id=1, destination_facility_id=1, prediction_point="AT_ORDER", as_of=NOW - M, status=status, commitment="PROMISED", risk=risk, input_hash="h", requested_by="t", algorithm_version="lt-test"))  # fmt: skip

    add(1, {"score": 0.8, "kind": "uncalibrated_score"})
    add(2, {"score": 0.3, "kind": "uncalibrated_score"})
    add(3, {"score": 0.9, "kind": "historical_rate"})
    add(4, None, "UNAVAILABLE")
    add(5, {"score": 0.7, "kind": "calibrated_probability"})
    db.commit()
    make_rule(db, "delivery_late_risk", {"threshold": 0.6}, severity="MEDIUM")
    out = S.evaluate(db, "rev", ctx(), NOW)
    al = {a.entity_id: a for a in live(db)}
    assert set(al) == {"PO-1", "PO-5"} and all(a.basis == "predicted" for a in al.values())
    assert "uncalibrated" in al["PO-1"].summary and "calibrated probability" in al["PO-5"].summary
    assert out["rules"][0]["no_data"] == 1 and "historical late rate" in out["rules"][0]["notes"][0]
    assert (
        al["PO-1"].priority >= al["PO-5"].priority
    )  # low-confidence prediction is never more urgent than a calibrated one


# ---------- forecast monitoring (existing signals) ----------


def test_forecast_monitoring_mirrors_existing_signals_and_dedups_by_fingerprint(client, db):
    w = synthetic_world(client)
    names = [k for k in w if k.startswith("p")][:1] + [k for k in w if k.startswith("f")][:1]
    run_row = forecast(client, w, names[0], names[1])
    db.add(
        ForecastAlert(
            fingerprint="fp-1",
            kind="sustained_bias",
            severity="WARNING",
            product_id=run_row.product_id,
            facility_id=run_row.facility_id,
            run_id=run_row.id,
            message="Forecast has over-predicted for 6 periods",
            evidence={"periods": 6},
        )
    )
    db.add(
        ForecastAlert(
            fingerprint="fp-2",
            kind="stale_forecast",
            severity="INFO",
            product_id=run_row.product_id,
            facility_id=run_row.facility_id,
            run_id=run_row.id,
            message="info only",
            evidence={},
        )
    )
    db.commit()
    make_rule(db, "forecast_monitoring", {"severities": ["WARNING"]})
    S.evaluate(db, "rev", ctx(), NOW)
    S.evaluate(db, "rev", ctx(), NOW + 5 * M)  # evaluated again: same signal, same alert
    al = live(db)
    assert (
        len(al) == 1
        and al[0].occurrence_count == 2
        and al[0].evidence["fingerprint"] == "fp-1"
        and al[0].basis == "assessed"
    )


def test_the_capacity_detector_is_unavailable_and_says_why(client, db):
    from app.core.errors import DomainValidationError

    with pytest.raises(DomainValidationError, match="no capacity-utilisation"):
        S.create_rule(db, "rev", rule_body(name="cap", detector="capacity_pressure", params={}), ctx())
    assert D.PULL.get("capacity_pressure") is None


def test_scenario_alerts_are_labelled_hypothetical_from_a_real_impact_analysis(client, db):
    from tests.impact_helpers import analyze, linear, make_run, sup_event

    net = linear(client)
    an = analyze(client, make_run(client, [sup_event(net["a"])]))
    reached = an.summary["unique_entities"]
    assert reached >= 3
    make_rule(db, "scenario_impact", {"metric": "unique_entities", "threshold": reached}, severity="CRITICAL")
    make_rule(db, "scenario_impact", {"metric": "unique_entities", "threshold": reached + 1}, name="above")
    S.evaluate(db, "rev", ctx(), NOW)
    (a,) = live(db)
    assert (
        a.basis == "hypothetical" and a.evidence["analysis_uid"] == an.analysis_uid and a.condition["value"] == reached
    )
    assert "what-if" in a.summary and "not an active disruption" in a.summary and "not a quantified loss" in a.summary
    assert (
        a.severity == "CRITICAL" and a.priority == 3
    )  # severity is the rule's, but a what-if is capped at P3 and says so
    assert any(x["factor"] == "hypothetical_scenario" for x in a.explanation["priority"])
