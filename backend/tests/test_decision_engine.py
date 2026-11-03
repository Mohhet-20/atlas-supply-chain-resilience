"""Pure comparison engine: baselines, comparability, zero/missing baselines, trade-offs, probabilities, determinism, redaction."""

from __future__ import annotations

import copy

from app.decision import compare
from app.decision import metrics as M

N = "NORMAL"


def plan(key, role, **scope):
    s = {"product_id": 1, "destination_facility_id": 1, "quantity": 60, "unit": "kg", "currency": "USD"} | scope
    return {
        "key": key,
        "label": key,
        "role": role,
        "source": key,
        "scope": s,
        "allocation": [],
        "constraint_checks": [],
        "feasible": True,
        "reference_kind": None,
    }


def cell(v, unit="currency", **q):
    return {"value": v, "unit": unit, **q}


def snap(base_cells, alt_cells, conds=None, **kw):
    s = {"algorithm_version": "dc-1", "metric_registry_version": "mr-1", "horizon_days": None, "assumptions": [], "scenario_ids": [],
         "plans": [plan("B", "BASELINE"), plan("X", "ALTERNATIVE")], "conditions": conds or [{"key": N, "name": N, "mode": "NORMAL", "probability": None}],
         "cells": {"B": base_cells, "X": alt_cells}, "exposure": [], "probability_basis": None, "evidence": [], "warnings": [], "issues": []}  # fmt: skip
    return s | kw


def rows(res):
    out = {}
    for comp in res["comparisons"]:
        for c in comp["conditions"]:
            for r in c["rows"]:
                out[(c["condition"], r["metric"])] = r
    return out


def agg(res, plan_key, metric):
    p = next(a for a in res["aggregation"] if a["plan_key"] == plan_key)
    return next((m for m in p["metrics"] if m["metric"] == metric), None)


COST = {"currency": "USD", "basis": "PURCHASE_ONLY"}


def test_better_worse_tradeoff_classification():
    s = snap(
        {N: {"MODELLED_COST": cell(100, **COST), "UNMET_UNITS": cell(10, "units")}},
        {N: {"MODELLED_COST": cell(120, **COST), "UNMET_UNITS": cell(2, "units")}},
    )
    r = compare.evaluate(s)
    assert r["comparisons"][0]["classification"] == "TRADE_OFF"
    rr = rows(r)
    assert rr[(N, "MODELLED_COST")]["verdict"] == "worse" and rr[(N, "UNMET_UNITS")]["verdict"] == "better"
    assert (
        abs(rr[(N, "MODELLED_COST")]["difference"] - 20) < 1e-9
        and abs(rr[(N, "MODELLED_COST")]["relative_pct"] - 20.0) < 1e-9
    )


def test_zero_baseline_has_no_percentage_and_never_divides():
    s = snap({N: {"UNMET_UNITS": cell(0, "units")}}, {N: {"UNMET_UNITS": cell(5, "units")}})
    r = rows(compare.evaluate(s))[(N, "UNMET_UNITS")]
    assert r["relative_pct"] is None and r["verdict"] == "worse"


def test_missing_values_are_never_zero():
    s = snap({N: {"MODELLED_COST": cell(100, **COST)}}, {N: {}})
    res = compare.evaluate(s)
    rr = rows(res)
    assert (N, "MODELLED_COST") not in rr or rr[(N, "MODELLED_COST")]["status"] == "missing_value"
    s2 = snap({N: {}}, {N: {"MODELLED_COST": cell(100, **COST)}})
    assert rows(compare.evaluate(s2))[(N, "MODELLED_COST")]["status"] == "missing_baseline"


def test_unit_currency_and_basis_mismatch_are_incomparable():
    s = snap(
        {N: {"MODELLED_COST": cell(100, **COST)}},
        {N: {"MODELLED_COST": cell(90, currency="EUR", basis="PURCHASE_ONLY")}},
    )
    assert rows(compare.evaluate(s))[(N, "MODELLED_COST")]["status"] == "incomparable"
    s = snap(
        {N: {"MODELLED_COST": cell(100, **COST)}}, {N: {"MODELLED_COST": cell(90, currency="USD", basis="LANDED")}}
    )
    assert rows(compare.evaluate(s))[(N, "MODELLED_COST")]["status"] == "incomparable"


def test_scope_mismatch_makes_scope_dependent_metrics_incomparable():
    s = snap({N: {"UNMET_UNITS": cell(1, "units")}}, {N: {"UNMET_UNITS": cell(1, "units")}})
    s["plans"][1] = plan("X", "ALTERNATIVE", quantity=90)
    assert rows(compare.evaluate(s))[(N, "UNMET_UNITS")]["status"] == "incomparable"


def test_no_baseline_and_two_baselines():
    s = snap({}, {})
    s["plans"][0]["role"] = "ALTERNATIVE"
    r = compare.evaluate(s)
    assert r["result_status"] == "insufficient_data" and r["issues"][0]["code"] == "BASELINE_REQUIRED"
    s = snap({}, {})
    s["plans"][1]["role"] = "BASELINE"
    assert compare.evaluate(s)["issues"][0]["code"] == "MULTIPLE_BASELINES"


def test_baseline_only_is_insufficient():
    s = snap({N: {}}, {})
    s["plans"] = s["plans"][:1]
    r = compare.evaluate(s)
    assert r["result_status"] == "insufficient_data" and r["issues"][0]["code"] == "NOTHING_TO_COMPARE"


def test_probability_needs_every_disrupted_condition_and_basis():
    conds = [
        {"key": N, "name": N, "mode": "NORMAL", "probability": None},
        {"key": "out [FIXED]", "name": "out", "mode": "FIXED", "probability": 1.0},
    ]
    cells_b = {N: {"UNMET_UNITS": cell(0, "units")}, "out [FIXED]": {"UNMET_UNITS": cell(30, "units")}}
    cells_x = {N: {"UNMET_UNITS": cell(0, "units")}, "out [FIXED]": {"UNMET_UNITS": cell(10, "units")}}
    ok = compare.evaluate(snap(cells_b, cells_x, conds, probability_basis="EXPERT_DEFINED"))
    m = agg(ok, "X", "UNMET_UNITS")
    assert m["worst"]["value"] == 10 and m["best"]["value"] == 10 and abs(m["expected"]["value"] - 10) < 1e-9
    assert m["expected"]["basis"] == "EXPERT_DEFINED"
    assert agg(compare.evaluate(snap(cells_b, cells_x, conds)), "X", "UNMET_UNITS")["expected"] is None
    c2 = copy.deepcopy(conds)
    c2[1]["probability"] = None
    none_p = compare.evaluate(snap(cells_b, cells_x, c2, probability_basis="EXPERT_DEFINED"))
    m = agg(none_p, "X", "UNMET_UNITS")
    assert m["expected"] is None and m["worst"]["value"] == 10
    c3 = copy.deepcopy(conds)
    c3[1]["probability"] = 0.4
    assert (
        agg(compare.evaluate(snap(cells_b, cells_x, c3, probability_basis="EXPERT_DEFINED")), "X", "UNMET_UNITS")[
            "expected"
        ]
        is None
    )  # sums to 0.4


def test_alternatives_always_include_maintain_baseline_and_no_overall_winner():
    s = snap({N: {"MODELLED_COST": cell(100, **COST)}}, {N: {"MODELLED_COST": cell(80, **COST)}})
    r = compare.evaluate(s)
    keys = [a["key"] for a in r["alternatives"]]
    assert keys[0] == "MAINTAIN_BASELINE" and "X" in keys
    assert all("winner" not in a and "best_overall" not in a and "rank" not in a for a in r["alternatives"])


def test_evaluate_is_deterministic_and_hash_stable():
    s = snap({N: {"MODELLED_COST": cell(100, **COST)}}, {N: {"MODELLED_COST": cell(80, **COST)}})
    a, b = compare.evaluate(copy.deepcopy(s)), compare.evaluate(copy.deepcopy(s))
    assert a == b


def test_cost_hidden_removes_cost_and_flags():
    s = snap(
        {N: {"MODELLED_COST": cell(100, **COST), "UNMET_UNITS": cell(5, "units")}},
        {N: {"MODELLED_COST": cell(80, **COST), "UNMET_UNITS": cell(1, "units")}},
    )
    full = compare.evaluate(s)
    assert any("Modelled cost" in n for a in full["alternatives"] for n in a["cost_notes"])
    r = compare.cost_hidden(full)
    assert r["cost_redacted"] is True
    c = rows(r)[(N, "MODELLED_COST")]
    assert (
        c["value"] is None
        and c["baseline"] is None
        and c["difference"] is None
        and c["relative_pct"] is None
        and c["hidden"] is True
    )
    assert rows(r)[(N, "UNMET_UNITS")]["value"] == 1 and all(a["cost_notes"] == [] for a in r["alternatives"])
    assert rows(full)[(N, "MODELLED_COST")]["value"] == 80  # the original is untouched


def test_neutral_metric_verdict_is_changed_and_equal_is_same():
    neutral = [m["id"] for m in M.REGISTRY if m["direction"] == "neutral"]
    assert neutral and M.verdict(neutral[0], 3, 5) == "changed"
    assert M.verdict("MODELLED_COST", 100, 100) == "same" and M.verdict("MODELLED_COST", 100, 90) == "better"
    assert M.verdict("FULFILMENT", 0.9, 0.8) == "worse"
