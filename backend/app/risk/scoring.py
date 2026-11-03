"""Combine factors into one assessment: score, coverage, reliability, classification, status and actions.

score       = sum(W_i x s_i) / sum(W_i) over the AVAILABLE risk-role factors, with correlated factors reduced to their
              highest member so the same relationships are not added up repeatedly
coverage    = sum(weights of available risk-role factors) / sum(weights of applicable risk-role factors)
reliability = weighted mean factor confidence x (1 - penalty x relationship uncertainty)
Missing categories never contribute 0: they leave the numerator AND count against coverage.
Reliability-role factors (data quality, relationship uncertainty) never change the score.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from app.models.enums import AssessmentStatus, FactorState, ReliabilityBand
from app.models.enums import RiskCategory as C
from app.risk.factors import evaluate_all
from app.risk.methodology import (
    CATEGORY_DOCS,
    CORRELATION_GROUPS,
    RiskConfig,
    classify,
    config_hash,
)
from app.risk.types import AssessmentResult, FactorResult, NetworkFacts, ReachFact, SupplierFacts

SYNTHETIC = {"SYNTHETIC_DEMO", "SIMULATED"}


def _group_of(cat: C) -> str | None:
    for name, members in CORRELATION_GROUPS.items():
        if cat in members:
            return name
    return None


def combine(factors: dict[C, FactorResult], cfg: RiskConfig) -> dict:
    risk = [f for f in factors.values() if f.role == "risk" and f.state is not FactorState.NOT_APPLICABLE]
    total_w = sum(f.weight for f in risk)
    avail = [f for f in risk if f.state is FactorState.SCORED]
    avail_w = sum(f.weight for f in avail)
    coverage = round(avail_w / total_w, 4) if total_w > 0 else 0.0
    # correlated groups: highest member drives the group; the group keeps the combined weight of its available members
    units: list[
        tuple[str, float, float, list[FactorResult], FactorResult]
    ] = []  # (key, weight, score, members, driver)
    grouped: dict[str, list[FactorResult]] = {}
    for f in avail:
        g = _group_of(f.category)
        if g:
            grouped.setdefault(g, []).append(f)
        else:
            units.append((f.category.value, f.weight, f.score or 0.0, [f], f))
    for g, members in sorted(grouped.items()):
        driver = max(members, key=lambda m: (m.score or 0.0, m.weight, m.category.value))
        units.append((g, sum(m.weight for m in members), driver.score or 0.0, members, driver))
    eff = sum(u[1] for u in units)
    score = round(sum(w * s for _, w, s, _, _ in units) / eff, 2) if eff > 0 else None
    return {
        "total_w": total_w,
        "avail_w": avail_w,
        "coverage": coverage,
        "units": units,
        "eff": eff,
        "score": score,
        "avail": avail,
    }


def _reliability(c: dict, factors: dict[C, FactorResult], cfg: RiskConfig) -> tuple[float | None, str | None]:
    avail = [f for f in c["avail"] if f.confidence is not None]
    w = sum(f.weight for f in avail)
    if not avail or w <= 0:
        return None, None
    base = sum(f.weight * (f.confidence or 0.0) for f in avail) / w
    ru = factors[C.RELATIONSHIP_UNCERTAINTY]
    u = (ru.score or 0.0) / 100.0 if ru.state is FactorState.SCORED else 0.0
    r = round(max(0.0, min(1.0, base * (1 - cfg.uncertainty_reliability_penalty * u))), 4)
    band = (
        ReliabilityBand.HIGH
        if r >= cfg.reliability_high
        else ReliabilityBand.MODERATE
        if r >= cfg.reliability_moderate
        else ReliabilityBand.LOW
    )
    return r, band.value


def _fingerprint(
    status: str, score: float | None, factors: dict[C, FactorResult], mhash: str, now: datetime, cfg: RiskConfig
) -> str:
    payload = {
        "m": mhash, "status": status, "score": score,
        "f": [
            {
                "c": f.category.value, "st": f.state.value, "sc": f.score, "b": f.basis.value, "fr": f.freshness.value,
                "cf": f.confidence, "ev": [[e.etype.value, e.ref, e.observed_at.isoformat() if e.observed_at else None, e.provenance] for e in f.evidence],
                "rr": f.requires_review,
            }
            for f in sorted(factors.values(), key=lambda x: x.category.value)
        ],
    }  # fmt: skip
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _actions(status: str, classification: str | None, band: str | None, score: float | None, factors: dict[C, FactorResult],
             synthetic: bool, cfg: RiskConfig) -> list[str]:  # fmt: skip
    out: list[str] = []
    if status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value:
        out.append(
            "Collect the missing evidence listed under 'Missing information' before relying on this supplier's risk status; no risk level is implied."
        )
    if status == AssessmentStatus.PARTIAL.value:
        out.append(
            "The score covers only part of the required evidence; treat it as indicative and do not compare it with fully assessed suppliers."
        )
    ss = factors[C.SINGLE_SOURCE]
    if ss.state is FactorState.SCORED and (ss.score or 0) > 0:
        out.append("Review sole-sourced dependents: identify alternative suppliers, or confirm that none exists.")
    ru = factors[C.RELATIONSHIP_UNCERTAINTY]
    if ru.state is FactorState.SCORED and ru.requires_review:
        out.append(
            "Confirm or reject proposed, expired or low-confidence relationships; they are excluded from the risk metrics."
        )
    dq = factors[C.DATA_QUALITY]
    if dq.state is FactorState.UNAVAILABLE:
        out.append(
            "Run a data-quality evaluation for this supplier and its relationships; quality is currently unknown."
        )
    elif dq.requires_review:
        out.append("Resolve open data-quality findings behind this assessment.")
    if factors[C.GEOGRAPHIC].requires_review:
        out.append("Reconcile the conflicting location records for this supplier and its facilities.")
    if factors[C.DEPENDENCY].requires_review:
        out.append("Record criticality on the supplier's relationships; unrecorded criticality uses a default weight.")
    if classification in ("HIGH", "CRITICAL") and band == ReliabilityBand.LOW.value:
        out.append("Verify the evidence behind the high-scoring factors before acting: assessment reliability is low.")
    for cat in (C.FINANCIAL, C.COMPLIANCE, C.OPERATIONAL):
        out.append(
            f"{CATEGORY_DOCS[cat].label} is not assessed: it needs {CATEGORY_DOCS[cat].data_dependency[0].lower() + CATEGORY_DOCS[cat].data_dependency[1:]}"
        )
    if factors[C.DELIVERY].state is FactorState.UNAVAILABLE:
        out.append(
            "Request recent on-time-delivery observations; a missing delivery history is not evidence of unreliable delivery."
        )
    if synthetic:
        out.append(
            "Some evidence is SYNTHETIC_DEMO or SIMULATED: results illustrate the method and are not real-world intelligence."
        )
    seen, final = set(), []
    for a in out:
        if a not in seen:
            seen.add(a)
            final.append(a)
    return final[:12]


def assemble(
    sf: SupplierFacts, factors: dict[C, FactorResult], today: date, now: datetime, cfg: RiskConfig
) -> AssessmentResult:
    mhash = config_hash(cfg)
    c = combine(factors, cfg)
    n_scored = len(c["avail"])
    if c["score"] is None or c["coverage"] < cfg.min_coverage_for_score or n_scored < cfg.min_scored_factors:
        status, score, cls = AssessmentStatus.INSUFFICIENT_EVIDENCE.value, None, None
    elif c["coverage"] < cfg.min_coverage_for_classification:
        status, score, cls = AssessmentStatus.PARTIAL.value, c["score"], None
    else:
        status, score, cls = AssessmentStatus.ASSESSED.value, c["score"], classify(c["score"], cfg.thresholds)
    for f in factors.values():
        if f.state is FactorState.SCORED and f.score is not None:
            f.severity = classify(f.score, cfg.thresholds)
    reliability, band = (
        _reliability(c, factors, cfg) if status != AssessmentStatus.INSUFFICIENT_EVIDENCE.value else (None, None)
    )
    contributions = []
    for _key, w, s, members, driver in c["units"]:
        for m in members:
            counted = m is driver
            contributions.append({
                "category": m.category.value, "score": m.score, "weight": m.weight, "group": _group_of(m.category), "counted": counted,
                "points": round((w * s / c["eff"]) if counted and c["eff"] > 0 else 0.0, 2),
                "note": None if counted else f"Correlated with {driver.category.value}: not added again (the group uses its highest member).",
            })  # fmt: skip
    contributions.sort(key=lambda x: (-x["points"], x["category"]))
    missing = []
    for f in sorted(factors.values(), key=lambda x: x.category.value):
        if f.state is FactorState.UNAVAILABLE:
            kind = f.details.get("kind") or (
                "no_source" if not CATEGORY_DOCS[f.category].data_available else "no_records"
            )
            missing.append({"category": f.category.value, "role": f.role, "reason": f.unavailable_reason, "kind": kind,
                            "missing_inputs": f.missing_inputs, "data_dependency": CATEGORY_DOCS[f.category].data_dependency, "weight": f.weight})  # fmt: skip
        elif f.state is FactorState.NOT_APPLICABLE:
            missing.append({"category": f.category.value, "role": f.role, "reason": f.unavailable_reason, "kind": "not_applicable",
                            "missing_inputs": [], "data_dependency": None, "weight": f.weight})  # fmt: skip
    all_ev = [e for f in factors.values() for e in f.evidence]
    stamps = [e.observed_at for e in all_ev if e.observed_at is not None]
    prov: dict[str, int] = {}
    for e in {e.key: e for e in all_ev}.values():
        prov[e.provenance or "SYSTEM"] = prov.get(e.provenance or "SYSTEM", 0) + 1
    synthetic = any(k in SYNTHETIC for k in prov)
    crit_edges = {
        e.ref: {"status": e.status, "criticality": e.criticality}
        for e in sorted(sf.edges, key=lambda x: x.ref)
        if e.criticality in cfg.alerts.critical_edge_levels
    }
    ss = factors[C.SINGLE_SOURCE]
    data_basis = {
        "provenance_counts": dict(sorted(prov.items())), "contains_synthetic_or_simulated": synthetic,
        "all_evidence_synthetic": bool(prov) and all(k in SYNTHETIC for k in prov),
        "critical_edges": crit_edges, "single_source_nodes": sorted(r["node"] for r in ss.details.get("sole_sourced_nodes", [])),
        "single_source_critical": sorted(r["node"] for r in ss.details.get("sole_sourced_nodes", []) if r.get("criticality") in cfg.alerts.critical_edge_levels),
        "factor_freshness": {f.category.value: f.freshness.value for f in factors.values() if f.state is FactorState.SCORED},
        "unavailable_categories": [m["category"] for m in missing if m["kind"] != "not_applicable"],
        "max_possible_coverage": round(sum(w for k, w in cfg.weights.items() if CATEGORY_DOCS[C(k)].data_available) / max(1e-9, sum(cfg.weights.values())), 4),
    }  # fmt: skip
    requires = any(f.requires_review for f in factors.values()) or cls in ("HIGH", "CRITICAL")
    return AssessmentResult(
        status=status, score=score, classification=cls, coverage=c["coverage"], effective_weight=round(c["eff"], 4), reliability=reliability,
        reliability_band=band, scored_factors=n_scored, factors=sorted(factors.values(), key=lambda f: f.category.value), contributions=contributions,
        missing=missing, actions=_actions(status, cls, band, score, factors, synthetic, cfg), data_basis=data_basis,
        fingerprint=_fingerprint(status, score, factors, mhash, now, cfg), evidence_latest_at=max(stamps) if stamps else None,
        evidence_earliest_at=min(stamps) if stamps else None, requires_review=requires, methodology_version=cfg.version, methodology_hash=mhash,
    )  # fmt: skip


def assess(
    sf: SupplierFacts, net: NetworkFacts, today: date, now: datetime, cfg: RiskConfig, reach: ReachFact | None = None
) -> AssessmentResult:
    """Pure: identical facts, ``now`` and configuration always give an identical result."""
    return assemble(sf, evaluate_all(sf, net, today, now, cfg, reach), today, now, cfg)
