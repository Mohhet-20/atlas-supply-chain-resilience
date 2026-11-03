"""Scoring engine: determinism, weights, coverage, insufficient evidence, freshness, quality, structure (no HTTP writes of risk data)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import DomainValidationError
from app.models.enums import EvidenceType, FactorBasis, FactorState, Freshness, RiskCategory
from app.risk.analysis import freshness_of, hhi_from_shares
from app.risk.methodology import DEFAULT_HASH, RiskConfig, classify, config_hash, risk_config
from app.risk.scoring import assemble
from app.risk.types import Evidence, FactorResult, SupplierFacts, SupplierInfo
from tests.graph_helpers import node, ok
from tests.supplier_risk_helpers import NOW, TODAY, engine_result, factor, two_supplier_site

C = RiskCategory


def fr(
    cat: C, score: float | None, weight: float, state: FactorState = FactorState.SCORED, confidence: float = 1.0
) -> FactorResult:
    return FactorResult(
        category=cat,
        role="risk",
        state=state if score is not None else FactorState.UNAVAILABLE,
        basis=FactorBasis.OBSERVED,
        weight=weight,
        explanation="x",
        score=score,
        confidence=confidence if score is not None else None,
        freshness=Freshness.FRESH,
    )


# ------------------------------------------------------------------------------------------------ combination rules


def sf() -> SupplierFacts:
    return SupplierFacts(info=SupplierInfo(1, "S-1", "Synthetic", "Germany", "ACTIVE", "SYNTHETIC_DEMO", None, None))


def full(scores: dict[C, float | None]) -> dict[C, FactorResult]:
    """Every category present (as the engine always produces); categories not named in ``scores`` are unavailable."""
    cfg = RiskConfig()
    out: dict[C, FactorResult] = {}
    for cat in C:
        if cat in (C.DATA_QUALITY, C.RELATIONSHIP_UNCERTAINTY):
            out[cat] = FactorResult(cat, "reliability", FactorState.UNAVAILABLE, FactorBasis.UNKNOWN, 0.0, "x")
        else:
            out[cat] = fr(cat, scores.get(cat), cfg.weights[cat.value])
    return out


def build(scores: dict[C, float | None]):
    return assemble(sf(), full(scores), TODAY, NOW, RiskConfig())


def test_score_is_weighted_mean_of_available_factors_only():
    r = build({C.DELIVERY: 80, C.OPERATIONAL: 20, C.GEOGRAPHIC: 60, C.FINANCIAL: None})
    # (0.15*80 + 0.10*20 + 0.10*60) / 0.35; unavailable categories contribute neither 0 nor 100
    assert r.score == pytest.approx(20.0 / 0.35, abs=0.01)
    assert r.coverage == pytest.approx(0.35)
    assert r.scored_factors == 3


def test_missing_data_is_not_low_risk_and_lowers_coverage_only():
    full_cov = build({C.DELIVERY: 50, C.OPERATIONAL: 50, C.FINANCIAL: 50, C.GEOGRAPHIC: 50})
    part = build({C.DELIVERY: 50, C.OPERATIONAL: 50, C.FINANCIAL: None, C.GEOGRAPHIC: 50})
    assert full_cov.score == part.score == 50.0
    assert part.coverage < full_cov.coverage
    assert {m["category"] for m in part.missing} >= {"FINANCIAL", "COMPLIANCE"}


def test_correlated_factors_count_once_via_the_highest_member():
    r = build({C.CONCENTRATION: 40, C.SINGLE_SOURCE: 90, C.DELIVERY: 10, C.OPERATIONAL: 10})
    counted = {c["category"]: c["counted"] for c in r.contributions}
    assert counted["SINGLE_SOURCE"] is True and counted["CONCENTRATION"] is False and counted["DELIVERY"] is True
    dropped = next(c for c in r.contributions if c["category"] == "CONCENTRATION")
    assert dropped["points"] == 0 and "not added again" in dropped["note"]
    # the group keeps the combined weight of its available members (0.15 + 0.10) and the score of its highest member (90)
    assert r.score == pytest.approx((0.25 * 90 + 0.15 * 10 + 0.10 * 10) / 0.50)


def test_scoring_needs_minimum_factors_and_coverage():
    one = build({C.DELIVERY: 70})
    assert one.status == "INSUFFICIENT_EVIDENCE" and one.score is None and one.classification is None  # one factor
    low = build({C.DELIVERY: 70, C.OPERATIONAL: 70})
    assert low.coverage == pytest.approx(0.25)
    assert low.status == "INSUFFICIENT_EVIDENCE" and low.score is None  # 0.25 coverage < 0.30


def test_classification_thresholds():
    t = RiskConfig().thresholds
    assert [classify(x, t) for x in (0, 29.99, 30, 54.99, 55, 74.99, 75, 100)] == [
        "LOW", "LOW", "MODERATE", "MODERATE", "HIGH", "HIGH", "CRITICAL", "CRITICAL",
    ]  # fmt: skip


def test_partial_band_has_a_score_but_no_classification():
    r = build({C.DELIVERY: 90, C.OPERATIONAL: 90, C.GEOGRAPHIC: 90})  # coverage 0.35: scored, not classified
    assert r.status == "PARTIAL" and r.score == 90.0 and r.classification is None
    assessed = build({C.DELIVERY: 90, C.OPERATIONAL: 90, C.GEOGRAPHIC: 90, C.FINANCIAL: 90})
    assert assessed.status == "ASSESSED" and assessed.classification == "CRITICAL"


def test_equal_inputs_give_identical_fingerprints_and_changed_inputs_do_not():
    a = build({C.DELIVERY: 50, C.OPERATIONAL: 50, C.FINANCIAL: 50})
    b = build({C.DELIVERY: 50, C.OPERATIONAL: 50, C.FINANCIAL: 50})
    c = build({C.DELIVERY: 51, C.OPERATIONAL: 50, C.FINANCIAL: 50})
    assert a.fingerprint == b.fingerprint and a.fingerprint != c.fingerprint


# ------------------------------------------------------------------------------------------------ methodology


def test_weights_must_name_the_risk_categories_and_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        RiskConfig(weights={**RiskConfig().weights, "DELIVERY": 0.5})
    with pytest.raises(ValueError, match="exactly the risk-role categories"):
        RiskConfig(weights={"DELIVERY": 1.0})
    with pytest.raises(ValueError):
        RiskConfig(min_coverage_for_score=0.6, min_coverage_for_classification=0.5)


def test_hash_tracks_parameters_and_a_changed_file_needs_a_new_version(tmp_path):
    assert config_hash(RiskConfig()) == DEFAULT_HASH
    assert config_hash(RiskConfig(dependency_saturation=7.0)) != DEFAULT_HASH
    same_label = tmp_path / "m.json"
    same_label.write_text(RiskConfig(dependency_saturation=7.0).model_dump_json())
    with pytest.raises(DomainValidationError, match="new version label"):
        risk_config(same_label)
    relabelled = tmp_path / "m2.json"
    relabelled.write_text(RiskConfig(version="rm-x", dependency_saturation=7.0).model_dump_json())
    assert risk_config(relabelled).version == "rm-x"
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    with pytest.raises(DomainValidationError):
        risk_config(bad)


def test_freshness_boundaries():
    cfg = RiskConfig()
    f = cfg.freshness
    assert freshness_of(None, NOW, cfg) is Freshness.UNKNOWN
    assert freshness_of(NOW - timedelta(days=f.fresh_days), NOW, cfg) is Freshness.FRESH
    assert freshness_of(NOW - timedelta(days=f.fresh_days + 1), NOW, cfg) is Freshness.AGING
    assert freshness_of(NOW - timedelta(days=f.aging_days + 1), NOW, cfg) is Freshness.STALE


def test_hhi_needs_shares_that_total_one_hundred():
    assert hhi_from_shares([60, 40]) == pytest.approx(0.52)
    assert hhi_from_shares([100]) == 1.0
    assert hhi_from_shares([60, 30]) is None  # totals 90%
    assert hhi_from_shares([]) is None


# ------------------------------------------------------------------------------------------------ evidence over real data


def test_no_relationships_means_insufficient_evidence_not_low_risk(client):
    s = node(client, "suppliers", "Isolated Supplier")
    res = engine_result(client, s["id"])
    assert res.status == "INSUFFICIENT_EVIDENCE" and res.score is None and res.classification is None
    assert res.coverage == 0 and res.scored_factors == 0
    assert all(f.state is not FactorState.SCORED or f.role != "risk" for f in res.factors)
    assert res.actions  # tells the analyst what would help


def test_unbuilt_categories_are_unavailable_with_a_named_dependency(client):
    a, _b, _f = two_supplier_site(client)
    res = engine_result(client, a["id"])
    for cat in ("FINANCIAL", "COMPLIANCE", "OPERATIONAL"):
        f = factor(res, cat)
        assert f.state is FactorState.UNAVAILABLE and f.score is None and f.unavailable_reason
    assert res.coverage <= 0.65 + 1e-9  # financial + compliance + operational weights can never be reached


def test_single_source_detected_only_when_no_other_confirmed_supplier(client):
    solo = node(client, "suppliers", "Solo Supplier")
    f1 = node(client, "facilities", "Solo Plant")
    ok(client, "SUPPLIES", solo, f1)
    res = engine_result(client, solo["id"])
    ss = factor(res, "SINGLE_SOURCE")
    assert ss.state is FactorState.SCORED and (ss.score or 0) > 0
    a, _b, _f = two_supplier_site(client)
    shared = factor(engine_result(client, a["id"]), "SINGLE_SOURCE")
    assert shared.state is not FactorState.SCORED or (shared.score or 0) == 0


def test_proposed_relationships_are_not_counted_and_are_listed_as_unverified(client):
    a = node(client, "suppliers", "Primary Supplier")
    b = node(client, "suppliers", "Maybe Supplier")
    f = node(client, "facilities", "Contested Plant")
    ok(client, "SUPPLIES", a, f)
    ok(client, "SUPPLIES", b, f, status="PROPOSED")
    res = engine_result(client, a["id"])
    ss = factor(res, "SINGLE_SOURCE")
    assert (ss.score or 0) > 0  # the proposed edge does not rescue it
    assert ss.details["sole_sourced_count"] == 1
    assert ss.details[
        "ends_if_unverified_alternatives_confirmed"
    ]  # reported as an unverified alternative, not silently dropped
    other = engine_result(client, b["id"])
    assert other.status == "INSUFFICIENT_EVIDENCE"  # the proposed supplier has no confirmed evidence of its own


def test_concentration_with_documented_shares_is_observed_and_matches_the_share(client):
    a, b, _f = two_supplier_site(client, share_a=90, share_b=10, crit="HIGH")
    ra, rb = engine_result(client, a["id"]), engine_result(client, b["id"])
    ca, cb = factor(ra, "CONCENTRATION"), factor(rb, "CONCENTRATION")
    assert ca.state is cb.state is FactorState.SCORED
    assert ca.basis is FactorBasis.OBSERVED
    assert ca.score > cb.score  # the 90% supplier is more concentrated than the 10% supplier
    assert ca.details["nodes"][0]["hhi"] == pytest.approx(0.82)


def test_concentration_without_shares_is_an_inferred_equal_share_approximation(client):
    a, _b, _f = two_supplier_site(client)
    c = factor(engine_result(client, a["id"]), "CONCENTRATION")
    assert c.state is FactorState.SCORED and c.basis is FactorBasis.INFERRED
    assert c.details["nodes"][0]["share_basis"] == "count_based" and c.details["nodes"][0]["hhi"] is None
    assert c.confidence is not None and c.confidence < 1


def test_concentration_is_unavailable_without_shares_when_the_approximation_is_off(client):
    a, _b, _f = two_supplier_site(client)
    cfg = RiskConfig(version="rm-strict", count_based_equal_share_approximation=False)
    c = factor(engine_result(client, a["id"], cfg), "CONCENTRATION")
    assert c.state is FactorState.UNAVAILABLE and c.score is None and c.unavailable_reason


def test_shares_that_do_not_total_one_hundred_are_not_used_for_hhi(client):
    a, _b, _f = two_supplier_site(client, share_a=60, share_b=20)
    c = factor(engine_result(client, a["id"]), "CONCENTRATION")
    assert c.details["nodes"][0]["hhi"] is None
    assert c.basis is FactorBasis.INFERRED


def test_criticality_raises_dependency_and_unknown_criticality_lowers_confidence(client):
    a, _b, _f = two_supplier_site(client, crit="CRITICAL")
    c, d, _g = two_supplier_site(client)
    dep_crit, dep_unknown = (
        factor(engine_result(client, a["id"]), "DEPENDENCY"),
        factor(engine_result(client, c["id"]), "DEPENDENCY"),
    )
    assert dep_crit.score >= dep_unknown.score
    assert dep_crit.confidence >= dep_unknown.confidence
    assert d["id"] != c["id"]


def test_expired_and_future_relationships_are_ignored(client):
    a, _b, f = two_supplier_site(client)
    solo = node(client, "suppliers", "Expired Supplier")
    ok(client, "SUPPLIES", solo, f, effective_from="2020-01-01", effective_to="2020-12-31")
    res = engine_result(client, solo["id"])
    assert res.status == "INSUFFICIENT_EVIDENCE"  # nothing currently effective to assess
    assert a["id"] != solo["id"]


def test_no_duplicate_evidence_within_an_assessment(client):
    a, _b, _f = two_supplier_site(client, share_a=60, share_b=40, crit="HIGH")
    res = engine_result(client, a["id"])
    for f in res.factors:
        keys = [e.key for e in f.evidence]
        assert len(keys) == len(set(keys)), f.category
        assert all(isinstance(e, Evidence) and isinstance(e.etype, EvidenceType) for e in f.evidence)


def test_poor_quality_lowers_reliability_but_never_the_score(client):
    from sqlalchemy.orm import Session

    from app.models.enums import EntityKind
    from app.models.quality import QualityScore
    from app.risk import inputs
    from app.risk.scoring import assess
    from app.risk.types import QualityFact

    a, _b, _f = two_supplier_site(client, share_a=60, share_b=40)
    with Session(client.app.state.engine) as s:
        facts, net = inputs.load_facts(s, [a["id"]], TODAY)
    cfg = risk_config()
    base = assess(facts[a["id"]], net, TODAY, NOW, cfg)
    facts[a["id"]].quality = QualityFact(f"SUPPLIER:{a['id']}", "SCORED", 20.0, NOW)
    poor = assess(facts[a["id"]], net, TODAY, NOW, cfg)
    facts[a["id"]].quality = QualityFact(f"SUPPLIER:{a['id']}", "SCORED", 98.0, NOW)
    good = assess(facts[a["id"]], net, TODAY, NOW, cfg)
    assert base.score == poor.score == good.score
    assert poor.reliability < good.reliability
    assert EntityKind.SUPPLIER and QualityScore  # model import sanity
    data_quality = factor(poor, "DATA_QUALITY")
    assert data_quality.role == "reliability" and data_quality.quality_context


def test_engine_is_deterministic_for_the_same_database_and_time(client):
    a, _b, _f = two_supplier_site(client, share_a=70, share_b=30, crit="HIGH")
    first, second = engine_result(client, a["id"]), engine_result(client, a["id"])
    assert first.fingerprint == second.fingerprint and first.score == second.score
    assert [(f.category, f.state, f.score) for f in first.factors] == [
        (f.category, f.state, f.score) for f in second.factors
    ]


def test_a_later_clock_only_changes_the_fingerprint_when_freshness_bands_change(client):
    a, _b, _f = two_supplier_site(client)
    near = engine_result(client, a["id"], now=NOW)
    later = engine_result(client, a["id"], now=NOW + timedelta(days=1))
    assert near.fingerprint == later.fingerprint
