"""Factor evaluators. Each returns a FactorResult that is SCORED only when real evidence supports it.

UNAVAILABLE means required evidence is missing (never low risk). NOT_APPLICABLE means the question does not arise for
this supplier and the factor leaves the coverage calculation. Nothing here reads a clock: ``now`` is passed in.
"""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean

from app.models.enums import (
    EdgeStatus,
    EvidenceType,
    FactorBasis,
    FactorState,
    Freshness,
)
from app.models.enums import (
    RiskCategory as C,
)
from app.risk.analysis import (
    SHARE_COUNT,
    NodeView,
    crit_weight,
    freshness_of,
    node_views,
    worst_freshness,
)
from app.risk.methodology import CATEGORY_DOCS, RELIABILITY_ROLE_CATEGORIES, RiskConfig
from app.risk.types import EdgeFact, Evidence, FactorResult, NetworkFacts, ReachFact, SupplierFacts

MAX_EVIDENCE = 40


def _role(cat: C) -> str:
    return "reliability" if cat in RELIABILITY_ROLE_CATEGORIES else "risk"


def _weight(cat: C, cfg: RiskConfig) -> float:
    return 0.0 if cat in RELIABILITY_ROLE_CATEGORIES else cfg.weights[cat.value]


def _new(cat: C, cfg: RiskConfig, state: FactorState, basis: FactorBasis, explanation: str, **kw) -> FactorResult:
    return FactorResult(
        category=cat, role=_role(cat), state=state, basis=basis, weight=_weight(cat, cfg), explanation=explanation, **kw
    )


def unavailable(cat: C, cfg: RiskConfig, reason: str, missing: list[str] | None = None, **kw) -> FactorResult:
    return _new(
        cat, cfg, FactorState.UNAVAILABLE, FactorBasis.UNKNOWN,
        f"Not scored: {reason} Unknown is not treated as low risk.", unavailable_reason=reason,
        missing_inputs=missing or [], **kw,
    )  # fmt: skip


def not_applicable(cat: C, cfg: RiskConfig, reason: str) -> FactorResult:
    return _new(
        cat,
        cfg,
        FactorState.NOT_APPLICABLE,
        FactorBasis.UNKNOWN,
        f"Not applicable: {reason}",
        unavailable_reason=reason,
    )


def external_unavailable(cat: C, cfg: RiskConfig) -> FactorResult:
    doc = CATEGORY_DOCS[cat]
    return unavailable(
        cat, cfg, f"ATLAS has no verified data source for {doc.label.lower()}.",
        [doc.data_dependency or "a verified source"], details={"data_dependency": doc.data_dependency, "kind": "no_source"},
    )  # fmt: skip


# ----------------------------------------------------------------------------------------------- shared helpers


def edge_evidence(e: EdgeFact, reasoning: str) -> Evidence:
    return Evidence(
        EvidenceType.RELATIONSHIP, e.ref, f"{e.type} {e.code} ({e.status}, criticality {e.criticality or 'unknown'})", reasoning,
        observed_at=e.updated_at, provenance=e.provenance, source_namespace=e.source_namespace,
    )  # fmt: skip


def _dedupe(evidence: list[Evidence]) -> tuple[list[Evidence], bool]:
    seen: dict[tuple[str, str], Evidence] = {}
    for ev in evidence:
        seen.setdefault(ev.key, ev)
    items = sorted(seen.values(), key=lambda e: e.key)
    return items[:MAX_EVIDENCE], len(items) > MAX_EVIDENCE


def _quality_value(sf: SupplierFacts, refs: list[str]) -> tuple[float, dict]:
    """Quality term in [0,1] for the records behind a factor. Unknown quality uses a stated discount, never 1.0."""
    scores = []
    if sf.quality is not None and sf.quality.score is not None:
        scores.append(sf.quality.score)
    for r in refs:
        q = sf.edge_quality.get(r)
        if q is not None and q.score is not None:
            scores.append(q.score)
    ctx = {
        "supplier_quality_score": sf.quality.score if sf.quality else None,
        "supplier_quality_state": sf.quality.state if sf.quality else "NOT_EVALUATED",
        "relationship_scores_used": len(scores) - (1 if sf.quality and sf.quality.score is not None else 0),
        "open_findings": len(sf.findings),
    }
    return (mean(scores) / 100.0 if scores else -1.0), ctx


def finish(f: FactorResult, sf: SupplierFacts, cfg: RiskConfig, now: datetime, evidence: list[Evidence], refs: list[str],
           basis: FactorBasis, observation_like: bool = False, extra_confidence: float = 1.0) -> FactorResult:  # fmt: skip
    """Attach evidence, freshness, quality context and the confidence of a SCORED factor."""
    items, truncated = _dedupe(evidence)
    f.evidence = items
    stamps = [e.observed_at for e in evidence if e.observed_at is not None]
    f.evidence_observed_at = max(stamps) if stamps else None
    f.freshness = worst_freshness([freshness_of(s, now, cfg) for s in stamps]) if stamps else Freshness.UNKNOWN
    f.basis = basis
    q, ctx = _quality_value(sf, refs)
    ctx["quality_known"] = q >= 0
    f.quality_context = ctx
    qv = cfg.unknown_quality_factor if q < 0 else q
    provs = [cfg.provenance_reliability.get(e.provenance or "", 0.5) for e in evidence if e.provenance]
    p = min(provs) if provs else 0.5
    fr = cfg.freshness_confidence.get(f.freshness.value, 0.3 if f.freshness is Freshness.STALE else 0.5)
    if f.freshness is Freshness.STALE and not observation_like:
        fr = 0.5  # stale structural records still describe the network; stale observations are never scored
        f.requires_review = True
    f.confidence = round(p * fr * cfg.basis_confidence[basis.value] * (0.5 + 0.5 * qv) * extra_confidence, 4)
    f.details["evidence_truncated"] = truncated
    f.details["observed_at_meaning"] = (
        "observation time" if observation_like else "record last updated (no separate observation date)"
    )
    f.details["provenance_floor"] = round(p, 2)
    return f


def _score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _node_summary(v: NodeView, **extra) -> dict:
    return {
        "node": f"{v.key[0].value}:{v.key[1]}", "code": v.code, "name": v.name, "confirmed_suppliers": v.n,
        "unverified_alternatives": len(v.proposed), "criticality": v.subject_edge.criticality, **extra,
    }  # fmt: skip


# ---------------------------------------------------------------------------------------------------- factors


def concentration(sf: SupplierFacts, net: NetworkFacts, today: date, now: datetime, cfg: RiskConfig) -> FactorResult:
    cat = C.CONCENTRATION
    views = node_views(sf, net, today, cfg)
    if not views:
        return unavailable(cat, cfg, "the supplier has no confirmed, currently effective dependents recorded.",
                           ["confirmed SUPPLIES / SUPPLIES_PRODUCT relationships"])  # fmt: skip
    multi = [v for v in views if v.n >= 2]
    sole = [v for v in views if v.n < 2]
    if not multi:
        r = not_applicable(
            cat,
            cfg,
            "every node the supplier serves has no other confirmed supplier; this is scored under single-source dependency.",
        )
        r.details = {"excluded_as_single_source": [_node_summary(v) for v in sole]}
        return r
    rows, evidence, refs, used = [], [], [], []
    approximated = False
    skipped = []
    for v in multi:
        if v.share_basis == SHARE_COUNT and not cfg.count_based_equal_share_approximation:
            skipped.append(v.code)
            continue
        share = v.shares[sf.info.id]
        w, known = crit_weight(v.subject_edge, cfg)
        approximated = approximated or v.share_basis == SHARE_COUNT or not known
        used.append((share, w))
        rows.append(
            _node_summary(
                v, share=round(share, 4), share_basis=v.share_basis, hhi=v.hhi, weight=w, criticality_known=known
            )
        )
        refs.append(v.subject_edge.ref)
        why = f"The supplier's {v.share_basis.replace('_', '-')} share of {v.code} among {v.n} confirmed suppliers is {share:.0%}."
        evidence += [edge_evidence(e, why) for e in v.edges]
    if not used:
        r = unavailable(
            cat,
            cfg,
            "supply share data is missing and the equal-share approximation is disabled.",
            ["supply_share_pct"],
        )
        r.details = {"skipped_nodes": skipped}
        return r
    tw = sum(w for _, w in used)
    score = _score(100 * sum(s * w for s, w in used) / tw) if tw else 0.0
    basis = FactorBasis.INFERRED if approximated else FactorBasis.OBSERVED
    r = _new(cat, cfg, FactorState.SCORED, basis, "", score=score)
    r.explanation = (
        f"Across {len(used)} node(s) with two or more confirmed suppliers, the supplier holds a criticality-weighted mean share "
        f"of {score:.0f}%."
        + (
            " Shares use an equal-share approximation where supply_share_pct is missing."
            if approximated
            else " Shares are documented values."
        )
    )
    r.details = {
        "nodes": rows, "excluded_as_single_source": [_node_summary(v) for v in sole], "skipped_nodes": skipped,
        "hhi_available": any(x["hhi"] is not None for x in rows),
        "hhi_note": "HHI is reported only for nodes whose documented shares total 100% within tolerance.",
    }  # fmt: skip
    return finish(r, sf, cfg, now, evidence, refs, basis)


def dependency(
    sf: SupplierFacts, net: NetworkFacts, today: date, now: datetime, cfg: RiskConfig, reach: ReachFact | None
) -> FactorResult:
    cat = C.DEPENDENCY
    views = node_views(sf, net, today, cfg)
    if not views:
        return unavailable(cat, cfg, "the supplier has no confirmed, currently effective dependents recorded.",
                           ["confirmed SUPPLIES / SUPPLIES_PRODUCT relationships"])  # fmt: skip
    exposure, unknown, evidence, refs, rows = 0.0, 0, [], [], []
    by_crit: dict[str, int] = {}
    for v in views:
        w, known = crit_weight(v.subject_edge, cfg)
        exposure += w
        unknown += 0 if known else 1
        by_crit[v.subject_edge.criticality or "UNKNOWN"] = by_crit.get(v.subject_edge.criticality or "UNKNOWN", 0) + 1
        refs.append(v.subject_edge.ref)
        evidence.append(
            edge_evidence(
                v.subject_edge, f"{v.code} depends on this supplier; its criticality weighs {w:g} toward importance."
            )
        )
        rows.append(_node_summary(v, weight=w, criticality_known=known))
    score = _score(100 * exposure / cfg.dependency_saturation)
    basis = FactorBasis.INFERRED if unknown else FactorBasis.OBSERVED
    products = sum(1 for v in views if v.is_product)
    r = _new(cat, cfg, FactorState.SCORED, basis, "", score=score)
    r.explanation = (
        f"{products} product(s) and {len(views) - products} facility(ies) depend on this supplier; criticality-weighted importance "
        f"{exposure:g} of {cfg.dependency_saturation:g}."
        + (f" {unknown} relationship(s) have no criticality and use a default weight." if unknown else "")
    )
    r.details = {
        "dependent_products": products, "dependent_facilities": len(views) - products, "criticality_counts": dict(sorted(by_crit.items())),
        "exposure": round(exposure, 4), "saturation": cfg.dependency_saturation, "unknown_criticality": unknown, "nodes": rows,
        "downstream_reach": None if reach is None else {
            "depth": reach.depth, "nodes_by_type": reach.nodes_by_type, "edges": reach.edges, "truncated": reach.truncated,
            "reasons": list(reach.reasons), "scored": False,
        },
        "reach_note": "Downstream reach is bounded context and is not part of the score in rm-1.",
    }  # fmt: skip
    r.requires_review = unknown > 0 and any(v.subject_edge.criticality is None for v in views) and len(views) >= 3
    return finish(r, sf, cfg, now, evidence, refs, basis)


def single_source(sf: SupplierFacts, net: NetworkFacts, today: date, now: datetime, cfg: RiskConfig) -> FactorResult:
    cat = C.SINGLE_SOURCE
    views = node_views(sf, net, today, cfg)
    if not views:
        return unavailable(cat, cfg, "the supplier has no confirmed, currently effective dependents recorded.",
                           ["confirmed SUPPLIES / SUPPLIES_PRODUCT relationships"])  # fmt: skip
    sole = [v for v in views if v.n == 1]
    weight_sum, unknown, evidence, refs, rows = 0.0, 0, [], [], []
    would_end = 0
    for v in sole:
        w, known = crit_weight(v.subject_edge, cfg)
        weight_sum += w
        unknown += 0 if known else 1
        refs.append(v.subject_edge.ref)
        why = f"{v.code} has no other confirmed supplier in the observed dataset" + (
            f" ({len(v.proposed)} unverified alternative(s) not counted)." if v.proposed else "."
        )
        evidence.append(edge_evidence(v.subject_edge, why))
        would_end += 1 if v.proposed else 0
        rows.append(_node_summary(
            v, weight=w, criticality_known=known, claim="ONLY_CONFIRMED_SUPPLIER_IN_OBSERVED_DATASET", exclusive_supplier=None,
            low_confidence=v.subject_edge.confidence is not None and v.subject_edge.confidence < cfg.relationship_low_confidence,
        ))  # fmt: skip
    score = _score(100 * weight_sum / cfg.single_source_saturation) if sole else 0.0
    basis = FactorBasis.INFERRED if unknown else FactorBasis.OBSERVED
    r = _new(cat, cfg, FactorState.SCORED, basis, "", score=score)
    r.explanation = (
        f"{len(sole)} of {len(views)} dependent node(s) have this supplier as the only confirmed supplier in the observed dataset."
        if sole
        else f"None of the {len(views)} dependent node(s) is sole-sourced among confirmed suppliers."
    ) + " Exclusivity is not modelled and is not claimed."
    r.details = {
        "sole_sourced_nodes": rows, "dependent_nodes": len(views), "sole_sourced_count": len(sole),
        "ends_if_unverified_alternatives_confirmed": would_end, "saturation": cfg.single_source_saturation,
        "unknown_criticality": unknown, "claim_note": "ONLY_CONFIRMED_SUPPLIER_IN_OBSERVED_DATASET is not 'exclusive' and not 'only verified'.",
    }  # fmt: skip
    r.requires_review = any(v.subject_edge.criticality in cfg.alerts.critical_edge_levels for v in sole)
    # evidence for sole nodes plus the supplier's other edges is the full dependent set; keep it to the sole nodes only
    return finish(
        r,
        sf,
        cfg,
        now,
        evidence or [edge_evidence(views[0].subject_edge, "No sole-sourced node; the dependent set was checked.")],
        refs or [views[0].subject_edge.ref],
        basis,
    )


def geographic(sf: SupplierFacts, net: NetworkFacts, today: date, now: datetime, cfg: RiskConfig) -> FactorResult:
    cat = C.GEOGRAPHIC
    mine = net.regions.get(sf.info.id, frozenset())
    conflicts, incomplete = location_flags(sf)
    if not mine:
        return unavailable(cat, cfg, "the supplier has no recorded region.", ["supplier country_or_region"],
                           details={"conflicts": conflicts, "incomplete": incomplete})  # fmt: skip
    views = [v for v in node_views(sf, net, today, cfg) if v.n >= 2]
    if not views:
        r = not_applicable(
            cat,
            cfg,
            "no served node has another confirmed supplier, so regional overlap between suppliers is undefined.",
        )
        r.details = {"conflicts": conflicts, "incomplete": incomplete, "regions": sorted(mine)}
        return r
    rows, evidence, refs, used = [], [], [], []
    unknown_regions = 0
    for v in views:
        others = [s for s in v.confirmed if s != sf.info.id]
        known = [s for s in others if net.regions.get(s)]
        unknown_regions += len(others) - len(known)
        if not known:
            continue
        same = [s for s in known if net.regions[s] & mine]
        frac = len(same) / len(known)
        w, _ = crit_weight(v.subject_edge, cfg)
        used.append((frac, w))
        refs.append(v.subject_edge.ref)
        why = f"{len(same)} of {len(known)} other confirmed supplier(s) of {v.code} share a region label with this supplier."
        evidence.append(edge_evidence(v.subject_edge, why))
        rows.append(
            _node_summary(v, other_suppliers_known_region=len(known), sharing_region=len(same), fraction=round(frac, 4))
        )
    if not used:
        r = unavailable(
            cat,
            cfg,
            "no other supplier of a served node has a recorded region.",
            ["other suppliers' country_or_region"],
        )
        r.details = {"conflicts": conflicts, "incomplete": incomplete}
        return r
    tw = sum(w for _, w in used)
    score = _score(100 * sum(f * w for f, w in used) / tw) if tw else 0.0
    evidence.append(Evidence(EvidenceType.REGISTRY_FIELD, f"SUPPLIER:{sf.info.id}.country_or_region",
                             f"Registered region '{sf.info.country}'", "Registered region labels are compared as normalised text.",
                             observed_at=sf.info.updated_at, provenance=sf.info.provenance, source_namespace=sf.info.source_namespace))  # fmt: skip
    for fac in sf.owned_facilities:
        evidence.append(Evidence(EvidenceType.REGISTRY_FIELD, f"FACILITY:{fac.id}.country_or_region", f"Owned facility {fac.code} region '{fac.country}'",
                                 "Owned-facility regions extend the supplier's footprint.", observed_at=fac.updated_at, provenance=fac.provenance))  # fmt: skip
    r = _new(cat, cfg, FactorState.SCORED, FactorBasis.OBSERVED, "", score=score)
    r.explanation = (
        f"On average {score:.0f}% of the other known suppliers of the products and facilities this supplier serves share a region label "
        "with it. Region labels are registry text, not geocodes; no hazard exposure is implied."
    )
    r.details = {
        "regions": sorted(mine),
        "nodes": rows,
        "unknown_region_suppliers": unknown_regions,
        "conflicts": conflicts,
        "incomplete": incomplete,
    }
    r.requires_review = bool(conflicts)
    return finish(r, sf, cfg, now, evidence, refs, FactorBasis.OBSERVED)


def location_flags(sf: SupplierFacts) -> tuple[list[str], list[str]]:
    """Conflicting and incomplete location records (reported, not scored)."""
    from app.registry.normalize import normalize_name

    conflicts, incomplete = [], []
    sup = normalize_name(sf.info.country)
    if not sup:
        incomplete.append("supplier region missing")
    for fac in sf.owned_facilities:
        fr = normalize_name(fac.country)
        if not fr:
            incomplete.append(f"{fac.code}: region missing")
        elif sup and fr != sup:
            conflicts.append(
                f"registered region '{sf.info.country}' differs from owned facility {fac.code} region '{fac.country}'"
            )
        if fac.latitude is None or fac.longitude is None:
            incomplete.append(f"{fac.code}: no coordinates")
        elif fac.latitude == 0 and fac.longitude == 0:
            incomplete.append(f"{fac.code}: placeholder coordinates (0, 0)")
        elif fac.location_status != "VERIFIED":
            incomplete.append(f"{fac.code}: coordinates {fac.location_status.lower()}")
    return conflicts, incomplete


def delivery(sf: SupplierFacts, now: datetime, cfg: RiskConfig) -> FactorResult:
    cat = C.DELIVERY
    d = cfg.delivery
    obs = [o for o in sf.observations if o.metric == d.metric]
    if not obs:
        return unavailable(cat, cfg, f"no '{d.metric}' observations are recorded for this supplier.", [f"{d.metric} observations"],
                           details={"kind": "no_records"})  # fmt: skip
    units = {o.unit for o in obs}
    if units != {"percent"}:
        return unavailable(
            cat, cfg, "on-time-delivery observations are not all in percent and cannot be compared.", ["percent unit"]
        )
    usable = [o for o in obs if freshness_of(o.observed_at, now, cfg) is not Freshness.STALE]
    newest = max(o.observed_at for o in obs)
    if len(usable) < d.min_observations:
        return unavailable(cat, cfg, f"only stale observations exist (newest {newest.date()}, older than {cfg.freshness.aging_days} days).",
                           ["a recent on-time-delivery observation"], details={"kind": "stale", "newest_observation": newest.isoformat()})  # fmt: skip
    usable.sort(key=lambda o: (o.observed_at, o.code))
    latest = usable[-1]
    risk = (d.target_pct - latest.value) / (d.target_pct - d.floor_pct) * 100
    score = _score(risk)
    trend = round(latest.value - usable[-2].value, 2) if len(usable) >= 2 else None
    lead = [o for o in sf.observations if o.metric == "quoted_lead_time"]
    evidence = [
        Evidence(EvidenceType.OBSERVATION, o.code, f"{o.metric} = {o.value:g} {o.unit} on {o.observed_at.date()}",
                 "On-time-delivery performance observation used to score delivery risk.", observed_at=o.observed_at,
                 provenance=o.provenance, source_namespace=o.source_label[:80])
        for o in usable[-10:]
    ]  # fmt: skip
    r = _new(cat, cfg, FactorState.SCORED, FactorBasis.OBSERVED, "", score=score)
    r.explanation = (
        f"Latest on-time delivery {latest.value:g}% ({latest.observed_at.date()}), mapped between a {d.floor_pct:g}% floor (risk 100) and a "
        f"{d.target_pct:g}% target (risk 0)."
        + (
            f" Change since the previous observation: {trend:+g} points."
            if trend is not None
            else " Only one usable observation."
        )
    )
    r.details = {
        "latest_value": latest.value, "latest_observation": latest.code, "latest_observed_at": latest.observed_at.isoformat(),
        "usable_observations": len(usable), "stale_observations_excluded": len(obs) - len(usable), "trend_points": trend,
        "target_pct": d.target_pct, "floor_pct": d.floor_pct,
        "quoted_lead_time_context": ([{"code": o.code, "value": o.value, "unit": o.unit, "observed_at": o.observed_at.isoformat()} for o in lead[-3:]]),
        "lead_time_note": "Quoted lead times are promises, not delivery performance; shown as context only.",
    }  # fmt: skip
    sample = min(1.0, 0.6 + 0.2 * len(usable))
    return finish(r, sf, cfg, now, evidence, [], FactorBasis.OBSERVED, observation_like=True, extra_confidence=sample)


def data_quality(sf: SupplierFacts, now: datetime, cfg: RiskConfig) -> FactorResult:
    cat = C.DATA_QUALITY
    parts: list[tuple[str, float, float]] = []
    if sf.quality is not None and sf.quality.score is not None:
        parts.append((sf.quality.subject, sf.quality.score, 2.0))
    for _ref, q in sorted(sf.edge_quality.items()):
        if q.score is not None:
            parts.append((q.subject, q.score, 1.0))
    sev_counts: dict[str, int] = {}
    for f in sf.findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
    if not parts:
        return unavailable(cat, cfg, "no scored quality evaluation exists for the supplier or its relationships; quality is unknown, not good.",
                           ["a quality evaluation run"], details={"open_findings": sev_counts, "kind": "not_evaluated"})  # fmt: skip
    tw = sum(w for _, _, w in parts)
    quality = sum(s * w for _, s, w in parts) / tw
    concern = _score(100 - quality)
    stamps_by_subject = {q.subject: q.evaluated_at for q in [sf.quality, *sf.edge_quality.values()] if q is not None}
    evidence = [
        Evidence(EvidenceType.QUALITY_SCORE, subj, f"Quality score {score:g} for {subj}", "quality score of a record behind this assessment.",
                 observed_at=stamps_by_subject.get(subj))
        for subj, score, _ in parts
    ]  # fmt: skip
    top = sorted(sf.findings, key=lambda f: ({"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}[f.severity], f.id))[
        :10
    ]
    evidence += [
        Evidence(EvidenceType.QUALITY_FINDING, f"FINDING:{f.id}", f"{f.rule_id} {f.severity}: {f.message[:200]}", "Open data-quality finding on a record behind this assessment.",
                 observed_at=f.first_detected_at)
        for f in top
    ]  # fmt: skip
    r = _new(cat, cfg, FactorState.SCORED, FactorBasis.OBSERVED, "", score=concern)
    r.explanation = (
        f"Weighted mean quality of the records behind this assessment is {quality:.0f}/100 (concern {concern:.0f}); "
        f"{len(sf.findings)} open finding(s). This lowers reliability and never changes the risk score."
    )
    r.details = {"quality_mean": round(quality, 2), "records_scored": len(parts), "open_findings": sev_counts}
    r.requires_review = concern >= 50 or sev_counts.get("CRITICAL", 0) > 0
    items, trunc = _dedupe(evidence)
    r.evidence = items
    stamps = [e.observed_at for e in evidence if e.observed_at is not None]
    r.evidence_observed_at = max(stamps) if stamps else None
    r.freshness = worst_freshness([freshness_of(s, now, cfg) for s in stamps]) if stamps else Freshness.UNKNOWN
    r.confidence = None
    r.quality_context = {"self_referential": True}
    r.details["evidence_truncated"] = trunc
    return r


def relationship_uncertainty(
    sf: SupplierFacts, today: date, now: datetime, cfg: RiskConfig, net: NetworkFacts
) -> FactorResult:
    cat = C.RELATIONSHIP_UNCERTAINTY
    live = [e for e in sf.edges if e.status in (EdgeStatus.CONFIRMED.value, EdgeStatus.PROPOSED.value)]
    if not live:
        return unavailable(
            cat, cfg, "the supplier has no confirmed or proposed relationships recorded.", ["supplier relationships"]
        )
    total = unc = 0.0
    evidence, rows = [], []
    reasons_by_ref: dict[str, list[str]] = {}
    for e in sorted(live, key=lambda x: x.ref):
        w, _ = crit_weight(e, cfg)
        why = []
        if e.status == EdgeStatus.PROPOSED.value:
            why.append("proposed (not reviewed)")
        t = e.timing(today)
        if e.status == EdgeStatus.CONFIRMED.value and t != "CURRENT":
            why.append("expired" if t == "EXPIRED" else "not yet effective")
        if e.confidence is not None and e.confidence < cfg.relationship_low_confidence:
            why.append(f"low recorded confidence ({e.confidence:g})")
        total += w
        if why:
            unc += w
            reasons_by_ref[e.ref] = why
            evidence.append(
                edge_evidence(
                    e, "Relationship is " + ", ".join(why) + "; excluded from or weakened in dependency metrics."
                )
            )
        rows.append(
            {
                "ref": e.ref,
                "status": e.status,
                "timing": t,
                "criticality": e.criticality,
                "weight": w,
                "uncertain": bool(why),
                "reasons": why,
            }
        )
    if not evidence:  # nothing uncertain: still cite one relationship so the factor is traceable
        e0 = sorted(live, key=lambda x: x.ref)[0]
        evidence.append(
            edge_evidence(e0, "All the supplier's live relationships are confirmed, effective and not low-confidence.")
        )
    score = _score(100 * unc / total) if total else 0.0
    # sensitivity: what the unverified relationships would change if confirmed
    proposed_nodes = {e.node for e in sf.edges if e.status == EdgeStatus.PROPOSED.value}
    sole_if_confirmed = 0
    for v in node_views(sf, net, today, cfg):
        if v.n == 1 and v.proposed:
            sole_if_confirmed += 1
    r = _new(cat, cfg, FactorState.SCORED, FactorBasis.OBSERVED, "", score=score)
    r.explanation = (
        f"{len(reasons_by_ref)} of {len(live)} relationship(s) are proposed, expired, not yet effective or low-confidence; weighted uncertainty {score:.0f}%. "
        "This lowers reliability and never changes the risk score."
    )
    r.details = {
        "relationships": rows, "uncertain_count": len(reasons_by_ref), "total": len(live),
        "sensitivity": {
            "proposed_relationships_not_counted": sum(1 for e in sf.edges if e.status == EdgeStatus.PROPOSED.value),
            "nodes_that_would_gain_this_supplier_if_confirmed": len(proposed_nodes),
            "sole_sourced_nodes_with_unverified_alternatives": sole_if_confirmed,
        },
    }  # fmt: skip
    r.requires_review = score >= 40 or any(
        e.criticality in cfg.alerts.critical_edge_levels for e in live if e.ref in reasons_by_ref
    )
    items, trunc = _dedupe(evidence)
    r.evidence = items
    stamps = [e.observed_at for e in evidence if e.observed_at is not None]
    r.evidence_observed_at = max(stamps) if stamps else None
    r.freshness = worst_freshness([freshness_of(s, now, cfg) for s in stamps]) if stamps else Freshness.UNKNOWN
    r.details["evidence_truncated"] = trunc
    return r


def evaluate_all(
    sf: SupplierFacts, net: NetworkFacts, today: date, now: datetime, cfg: RiskConfig, reach: ReachFact | None = None
) -> dict[C, FactorResult]:
    out = {
        C.CONCENTRATION: concentration(sf, net, today, now, cfg),
        C.DEPENDENCY: dependency(sf, net, today, now, cfg, reach),
        C.SINGLE_SOURCE: single_source(sf, net, today, now, cfg),
        C.GEOGRAPHIC: geographic(sf, net, today, now, cfg),
        C.OPERATIONAL: external_unavailable(C.OPERATIONAL, cfg),
        C.DELIVERY: delivery(sf, now, cfg),
        C.FINANCIAL: external_unavailable(C.FINANCIAL, cfg),
        C.COMPLIANCE: external_unavailable(C.COMPLIANCE, cfg),
        C.DATA_QUALITY: data_quality(sf, now, cfg),
        C.RELATIONSHIP_UNCERTAINTY: relationship_uncertainty(sf, today, now, cfg, net),
    }
    return out
