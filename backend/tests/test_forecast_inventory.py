"""Forecast-driven inventory projection (inventory engine, demand input). Synthetic data only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import DomainValidationError
from app.forecast.config import ForecastConfig
from app.inventory import config as icfg
from app.inventory import runs as iruns
from app.models.enums import SimStatus
from tests.forecast_helpers import TODAY, code, forecast, synthetic_world, values_of
from tests.inventory_helpers import findings_of, run_baseline, scenario_impact


def _conf(**kw):
    return icfg.InventoryConfig(as_of=datetime.now(timezone.utc), **kw)


def _f11(client, w, a):
    return findings_of(client, a.id)[(code(w, "p1"), code(w, "f1"))]


def test_forecast_drives_projection_with_provenance(client):
    w = synthetic_world(client)
    run = forecast(client, w, "p1", "f1", ForecastConfig(horizon=28, n_folds=5))
    a = run_baseline(client, _conf(horizon_days=28, forecast_run_id=run.id))
    assert a.status is SimStatus.COMPLETED, (a.error, a.failures)
    f = _f11(client, w, a)
    pj = f.projection
    vals = values_of(client, run.id)
    assert pj["demand_basis"] == "FORECAST" and pj["demand_series"] == pytest.approx([v.point for v in vals])
    src = pj["demand_source"]
    assert src["source"] == "forecast" and src["forecast_run_id"] == run.id and src["model"] == run.selected_model
    assert (
        src["origin"] == TODAY.isoformat()
        and src["data_snapshot_hash"] == run.data_snapshot_hash
        and "not an observation" in src["label"]
    )
    # opening eligible inventory + eligible receipts − forecast demand, by hand
    opening, closing = f.quantities["basis_value"], [f.quantities["basis_value"]]
    rec = {r["day"]: r["quantity"] for r in pj["receipts_counted_detail"]}
    for d_, dem in enumerate(pj["demand_series"], start=1):
        closing.append(max(0.0, closing[-1] + rec.get(d_, 0.0) - dem))
    assert pj["series"] == pytest.approx(closing, abs=1e-4) and opening == 600
    assert any("not an observation" in x for x in f.assumptions)
    assert f.evidence["forecast_run_id"] == run.id
    assert a.inputs["forecast_run_id"] == run.id


def test_baseline_without_forecast_is_unchanged_and_labelled_observed(client):
    w = synthetic_world(client)
    f = _f11(client, w, run_baseline(client))
    assert (
        f.projection["demand_source"]["source"] == "observed_rate"
        and "not a forecast" in f.projection["demand_source"]["label"]
    )
    assert "demand_series" not in f.projection and f.projection["demand_basis"] == "CONSUMPTION"


def test_forecast_and_observed_rate_are_different_inputs(client):
    w = synthetic_world(client)
    run = forecast(client, w, "p2", "f1")  # seasonal series is not the P1@F1 pair
    a = run_baseline(client, _conf(forecast_run_id=run.id))
    f = _f11(client, w, a)
    assert f.projection["demand_source"]["source"] == "observed_rate"
    assert "different product-facility pair" in f.projection["demand_source"]["note"]


def test_unit_mismatch_falls_back_with_label_unless_factor_supplied(client):
    w = synthetic_world(client)
    # P3@F1 is a KG series; give the same pair an EA inventory so the units differ
    from tests.inventory_helpers import iso, load_inventory

    load_inventory(client, [[code(w, "p3"), code(w, "f1"), iso(0.1), "KG", 500, 0, 0, 0, 0, "", ""]])
    run = forecast(client, w, "p3", "f1")
    assert run.unit == "KG"
    ok = findings_of(client, run_baseline(client, _conf(forecast_run_id=run.id)).id)[(code(w, "p3"), code(w, "f1"))]
    assert ok.projection["demand_source"]["source"] == "forecast"
    # a conversion factor changes the unit basis only when it is stated, and is recorded
    conv = findings_of(client, run_baseline(client, _conf(forecast_run_id=run.id, forecast_conversion_factor=2.0)).id)[
        (code(w, "p3"), code(w, "f1"))
    ]
    assert conv.projection["demand_source"]["conversion"] is None  # units already match: the factor is not applied


def test_unit_mismatch_uses_observed_rate_and_says_so(client):
    from app.forecast import integration as fint
    from app.models.forecast import ForecastRun

    w = synthetic_world(client)
    run = forecast(client, w, "p1", "f1", ForecastConfig(horizon=14, n_folds=5))
    with client.app.state.session_factory() as s:
        ctx = fint.load_context(s, run.id)
        res = fint.demand_series(ctx, unit="KG", as_of_date=TODAY, horizon_days=14, factor=None)
        assert res["status"] == "unit_mismatch" and res["series"] is None and "no conversion" in res["reason"]
        conv = fint.demand_series(ctx, unit="KG", as_of_date=TODAY, horizon_days=14, factor=0.5)
        assert conv["status"] == "usable" and conv["conversion"]["factor"] == 0.5
        assert conv["series"] == pytest.approx([v[2] * 0.5 for v in ctx["values"]])
        assert isinstance(ctx["run"], ForecastRun)


def test_forecast_shorter_than_horizon_stops_projection_there(client):
    w = synthetic_world(client)
    run = forecast(client, w, "p1", "f1", ForecastConfig(horizon=14, n_folds=5))
    f = _f11(client, w, run_baseline(client, _conf(horizon_days=60, forecast_run_id=run.id)))
    assert (
        f.projection["horizon_days"] == 14
        and f.projection["requested_horizon_days"] == 60
        and len(f.projection["series"]) == 15
    )
    assert any("14 of the 60" in x for x in f.assumptions)


def test_forecast_not_covering_as_of_falls_back_and_warns(client):
    w = synthetic_world(client)
    run = forecast(client, w, "p1", "f1", ForecastConfig(horizon=7, n_folds=5, origin=TODAY - timedelta(days=30)))
    f = _f11(client, w, run_baseline(client, _conf(forecast_run_id=run.id)))
    src = f.projection["demand_source"]
    assert src["source"] == "observed_rate" and src["fallback_from_forecast"]["status"] == "not_covered"
    assert any(i["code"] == "FORECAST_UNUSABLE" for i in f.data_quality)


def test_declined_or_missing_run_cannot_be_selected(client):
    w = synthetic_world(client)
    declined = forecast(client, w, "p3", "f2")
    assert declined.result_status == "insufficient_data"
    f = client.app.state.session_factory
    with f() as s, pytest.raises(DomainValidationError, match="no forecast values"):
        iruns.create_analysis(s, _conf(forecast_run_id=declined.id), "u", iruns.Limits())
    with f() as s, pytest.raises(Exception, match="not found"):
        iruns.create_analysis(s, _conf(forecast_run_id=999999), "u", iruns.Limits())


def test_scenario_stays_separate_from_forecast_demand(client):
    w = synthetic_world(client)
    run = forecast(client, w, "p1", "f1", ForecastConfig(horizon=28, n_folds=5))
    imp = scenario_impact(client, w)
    base = _f11(client, w, run_baseline(client, _conf(horizon_days=28, forecast_run_id=run.id)))
    sc = _f11(client, w, run_baseline(client, _conf(horizon_days=28, forecast_run_id=run.id), impact_analysis_id=imp))
    assert (
        sc.projection["demand_series"] == base.projection["demand_series"]
    )  # the disruption does not alter forecast demand
    assert sc.projection["series"] == base.projection["series"]  # baseline projection is still the pure forecast path
    assert sc.scenario is not None and sc.scenario["outcome"] in (
        "QUANTIFIED",
        "CONDITIONAL_BOUNDS",
        "NOT_EXPOSED",
        "EXPOSED_NO_MODELLED_SUPPLY",
    )
    if sc.scenario["outcome"] == "QUANTIFIED":
        assert sc.scenario["series"][0] == base.projection["series"][0]
        assert sc.scenario["min_closing"] <= base.projection["min_closing"] + 1e-6
