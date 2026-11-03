"""Analytics: metric semantics, comparison rules, cache isolation and CSV safety. Synthetic data only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analytics import registry as R
from app.analytics import service as S
from app.api.v1.analytics import _safe

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_every_metric_is_fully_defined():
    ids = [m["id"] for m in R.REGISTRY]
    assert len(ids) == len(set(ids))
    for m in R.REGISTRY:
        assert m["unit"] and m["calculation"] and m["time_basis"] and m["sources"] and m["missing"], m["id"]
        assert m["direction"] in ("min", "max", "neutral") and m["area"] in R.AREAS
        assert set(m["filters"]) <= {"region", "category", "risk_class"}
    assert R.BY_ID["RISK_HIGH_OR_CRITICAL"]["direction"] == "min"
    assert all(u["reason"] for u in R.UNAVAILABLE)
    assert "COMPOSITE_HEALTH_SCORE" in {u["id"] for u in R.UNAVAILABLE}  # no opaque score


def test_compare_zero_previous_has_no_percentage_and_neutral_is_never_good_or_bad():
    c = S.compare("RISK_HIGH_OR_CRITICAL", 3, 0)
    assert c["change"] == 3 and c["change_pct"] is None and c["verdict"] == "worse" and "undefined" in c["note"]
    assert S.compare("RISK_HIGH_OR_CRITICAL", 1, 2)["verdict"] == "better"
    assert (
        S.compare("RISK_HIGH_OR_CRITICAL", 2, 2) | {} == S.compare("RISK_HIGH_OR_CRITICAL", 2, 2)
        and S.compare("RISK_HIGH_OR_CRITICAL", 2, 2)["verdict"] == "same"
    )
    n = S.compare("SCN_RUNS", 9, 3)
    assert n["verdict"] == "changed" and n["change_pct"] == 200.0
    assert S.compare("LOG_DELIVERIES_ASSESSED", 5, 10)["verdict"] == "worse"  # direction max: fewer is worse
    assert S.compare("SCN_RUNS", 0, 0)["change_pct"] is None


def test_window_is_half_open_utc_and_previous_window_is_adjacent():
    s, e, ps = S.window(NOW, 30)
    assert e == NOW and s == NOW - timedelta(days=30) and ps == s - timedelta(days=30)
    assert s.tzinfo is not None and S.iso(s).endswith("+00:00")
    assert S.iso(datetime(2026, 1, 1, 5, 30)) == "2026-01-01T05:30:00+00:00"  # naive datetimes are treated as UTC


def test_metric_states_distinguish_unknown_from_zero():
    f = S.Filters()
    zero = S.metric("SCN_RUNS_FAILED", 0, f=f)
    none = S.metric("SCN_MAX_REACH", None, state="no_data", reason="none", f=f)
    assert zero["value"] == 0 and zero["state"] == "ok"
    assert none["value"] is None and none["state"] == "no_data"


def test_cache_expires_and_never_crosses_keys():
    t = [0.0]
    c = S.TTLCache(ttl=5, max_items=2, clock=lambda: t[0])
    c.put(("overview", "viewer", (30, None, None, None)), {"a": 1})
    assert c.get(("overview", "editor", (30, None, None, None))) is None
    assert c.get(("overview", "viewer", (30, "EU", None, None))) is None
    assert c.get(("overview", "viewer", (30, None, None, None))) == {"a": 1}
    t[0] = 6.0
    assert c.get(("overview", "viewer", (30, None, None, None))) is None
    for i in range(5):
        c.put(i, i)
    assert len(c._d) <= 2


def test_csv_formula_injection_is_neutralised():
    for bad in ("=1+1", "+cmd", "-2", "@SUM(A1)", "\tx", "\rx"):
        assert _safe(bad).startswith("'")
    assert _safe("Alder Parts") == "Alder Parts" and _safe(5) == 5 and _safe(None) is None


def test_filters_report_what_they_applied_and_ignored():
    f = S.Filters(30, "Germany", None, "HIGH")
    assert f.applied() == {"window_days": 30, "region": "Germany", "risk_class": "HIGH"}
    assert "risk_class" in S.ignored_filters("network", f) and "region" not in S.ignored_filters("network", f)
    assert S.ignored_filters("supplier_risk", f) == []
