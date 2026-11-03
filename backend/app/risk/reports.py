"""Read models for the Supplier Risk Intelligence workspace: portfolio overview, filtered list, comparison, geography."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError
from app.models.enums import (
    AssessmentStatus,
    EntityStatus,
    EvalStatus,
    FactorState,
    ReviewAction,
    RiskAlertStatus,
    RiskCategory,
    RiskClass,
)
from app.models.facility import Facility
from app.models.risk import RiskAlert, RiskAssessment, RiskFactor, RiskReview, RiskRun
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.registry.normalize import normalize_name
from app.risk import inputs, service
from app.risk.analysis import node_views
from app.risk.methodology import CATEGORY_DOCS, RiskConfig, config_hash
from app.risk.types import SupplierFacts

NOT_ASSESSED = "NOT_ASSESSED"
SORTS = ("score_desc", "score_asc", "name", "coverage_desc", "reliability_asc", "computed_desc", "code")


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters(*, q, supplier_status, region, classification, assessment_status, review_state, requires_review, min_coverage, max_coverage,
             reliability_band, category, category_state, min_category_score, cfg):  # fmt: skip
    where: list[Any] = [Supplier.merged_into_id.is_(None)]
    if supplier_status:
        where.append(Supplier.status == supplier_status)
    else:
        where.append(Supplier.status.in_([EntityStatus.ACTIVE, EntityStatus.ON_HOLD]))
    if q:
        where.append(
            or_(Supplier.name.ilike(_like(q), escape="\\"), Supplier.supplier_code.ilike(_like(q), escape="\\"))
        )
    if region:
        where.append(func.lower(Supplier.country_or_region) == region.strip().lower())
    if assessment_status == NOT_ASSESSED:
        where.append(RiskAssessment.id.is_(None))
    elif assessment_status:
        where.append(RiskAssessment.status == AssessmentStatus(assessment_status))
    if classification:
        where.append(RiskAssessment.classification == classification)
    if review_state:
        where.append(RiskAssessment.review_state == review_state)
    if requires_review is not None:
        where.append(RiskAssessment.requires_review.is_(requires_review))
        if requires_review:
            where.append(RiskAssessment.review_state == "UNREVIEWED")
    if min_coverage is not None:
        where.append(RiskAssessment.evidence_coverage >= min_coverage)
    if max_coverage is not None:
        where.append(RiskAssessment.evidence_coverage <= max_coverage)
    if reliability_band:
        where.append(RiskAssessment.reliability_band == reliability_band)
    if category:
        cond = [RiskFactor.assessment_id == RiskAssessment.id, RiskFactor.category == category]
        if category_state:
            cond.append(RiskFactor.state == category_state)
        if min_category_score is not None:
            cond += [RiskFactor.state == FactorState.SCORED, RiskFactor.score >= min_category_score]
        where.append(exists().where(and_(*cond)))
    return where


def _order(sort: str):
    unscored_last = case(
        (RiskAssessment.risk_score.is_(None), 1), else_=0
    )  # unknown never sorts as safest or as riskiest
    return {
        "score_desc": [unscored_last, RiskAssessment.risk_score.desc(), Supplier.id],
        "score_asc": [unscored_last, RiskAssessment.risk_score.asc(), Supplier.id],
        "name": [func.lower(Supplier.name), Supplier.id],
        "coverage_desc": [RiskAssessment.evidence_coverage.desc(), Supplier.id],
        "reliability_asc": [
            case((RiskAssessment.reliability.is_(None), 1), else_=0),
            RiskAssessment.reliability.asc(),
            Supplier.id,
        ],
        "computed_desc": [RiskAssessment.computed_at.desc(), Supplier.id],
        "code": [Supplier.supplier_code, Supplier.id],
    }[sort]


def list_suppliers(
    session: Session, cfg: RiskConfig, *, limit: int, offset: int, sort: str = "score_desc", **flt: Any
) -> dict[str, Any]:
    if sort not in SORTS:
        raise DomainValidationError("Unknown sort.")
    if flt.get("category"):
        try:
            RiskCategory(flt["category"])
        except ValueError:
            raise DomainValidationError("Unknown risk category.") from None
    where = _filters(cfg=cfg, **flt)
    base = (
        select(Supplier, RiskAssessment)
        .outerjoin(RiskAssessment, and_(RiskAssessment.supplier_id == Supplier.id, RiskAssessment.is_current.is_(True)))
        .where(*where)
    )
    total = session.scalar(select(func.count()).select_from(base.with_only_columns(Supplier.id).subquery())) or 0
    rows = session.execute(base.order_by(*_order(sort)).limit(limit).offset(offset)).all()
    ids = [s.id for s, _ in rows]
    overrides: dict[int, dict] = {}
    if ids:
        now = utcnow()
        cur = {s.id: a.id if a else None for s, a in rows}
        by_sup: dict[int, list[RiskReview]] = defaultdict(list)
        for r in session.scalars(
            select(RiskReview)
            .where(
                RiskReview.supplier_id.in_(ids),
                RiskReview.action.in_([ReviewAction.OVERRIDE, ReviewAction.REVOKE_OVERRIDE]),
            )
            .order_by(RiskReview.id)
        ):
            by_sup[r.supplier_id].append(r)
        for sid, lst in by_sup.items():
            revoked = {r.target_review_id for r in lst if r.action is ReviewAction.REVOKE_OVERRIDE}
            live = [r for r in lst if r.action is ReviewAction.OVERRIDE and r.id not in revoked]
            if live:
                r = live[-1]
                expired = r.override_expires_at is not None and r.override_expires_at < now
                overrides[sid] = {
                    "classification": r.override_classification.value if r.override_classification else None, "actor": r.actor, "created_at": r.created_at,
                    "expired": expired, "effective": not expired and cur.get(sid) == r.assessment_id,
                }  # fmt: skip
    items = []
    for s, a in rows:
        item: dict[str, Any] = {
            "supplier_id": s.id, "supplier_code": s.supplier_code, "name": s.name, "country_or_region": s.country_or_region, "category": s.category,
            "supplier_status": s.status.value, "provenance": s.provenance.value, "assessment_status": a.status.value if a else NOT_ASSESSED,
            "risk_score": a.risk_score if a else None, "classification": a.classification.value if a and a.classification else None,
            "evidence_coverage": a.evidence_coverage if a else None, "reliability": a.reliability if a else None,
            "reliability_band": a.reliability_band.value if a and a.reliability_band else None, "assessment_id": a.id if a else None,
            "computed_at": a.computed_at if a else None, "last_confirmed_at": a.last_confirmed_at if a else None,
            "review_state": a.review_state if a else None, "requires_review": bool(a and a.requires_review),
            "methodology_version": a.methodology_version if a else None, "rank_comparable": bool(a and a.status is AssessmentStatus.ASSESSED),
            "top_factors": [c for c in (a.contributions or []) if c.get("counted") and c.get("points", 0) > 0][:3] if a else [],
            "contains_synthetic": bool(a and (a.data_basis or {}).get("contains_synthetic_or_simulated")),
            "override": overrides.get(s.id),
        }  # fmt: skip
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset, "sort": sort,
            "ordering_note": "Suppliers without a score sort after scored suppliers in either direction; they are never ranked safer or riskier."}  # fmt: skip


def overview(session: Session, cfg: RiskConfig) -> dict[str, Any]:
    now = utcnow()
    active = [Supplier.merged_into_id.is_(None), Supplier.status.in_([EntityStatus.ACTIVE, EntityStatus.ON_HOLD])]
    total_suppliers = session.scalar(select(func.count()).select_from(Supplier).where(*active)) or 0
    joined = and_(RiskAssessment.supplier_id == Supplier.id, RiskAssessment.is_current.is_(True))
    status_rows = session.execute(
        select(RiskAssessment.status, func.count())
        .select_from(Supplier)
        .join(RiskAssessment, joined)
        .where(*active)
        .group_by(RiskAssessment.status)
    ).all()
    by_status = {s.value: n for s, n in status_rows}
    assessed_total = sum(by_status.values())
    cls_rows = session.execute(
        select(RiskAssessment.classification, func.count()).select_from(Supplier).join(RiskAssessment, joined)
        .where(*active, RiskAssessment.classification.is_not(None)).group_by(RiskAssessment.classification)
    ).all()  # fmt: skip
    by_class = {c.value: 0 for c in RiskClass} | {c.value: n for c, n in cls_rows}
    unreviewed = session.scalar(
        select(func.count()).select_from(Supplier).join(RiskAssessment, joined)
        .where(*active, RiskAssessment.requires_review.is_(True), RiskAssessment.review_state == "UNREVIEWED")
    ) or 0  # fmt: skip

    def with_factor(cond: Any) -> int:
        return session.scalar(
            select(func.count(func.distinct(Supplier.id))).select_from(Supplier).join(RiskAssessment, joined)
            .join(RiskFactor, RiskFactor.assessment_id == RiskAssessment.id).where(*active, cond)
        ) or 0  # fmt: skip

    high_dep = with_factor(
        and_(
            RiskFactor.category == RiskCategory.DEPENDENCY,
            RiskFactor.state == FactorState.SCORED,
            RiskFactor.score >= cfg.thresholds.high,
        )
    )
    single = with_factor(
        and_(
            RiskFactor.category == RiskCategory.SINGLE_SOURCE,
            RiskFactor.state == FactorState.SCORED,
            RiskFactor.score > 0,
        )
    )
    stale_cut = now - timedelta(days=cfg.assessment_stale_days)
    aging = session.scalar(
        select(func.count()).select_from(Supplier).join(RiskAssessment, joined).where(*active, RiskAssessment.last_confirmed_at < stale_cut)
    ) or 0  # fmt: skip
    week = now - timedelta(days=7)
    recent_new = (
        session.scalar(
            select(func.count())
            .select_from(RiskAssessment)
            .where(RiskAssessment.computed_at >= week, RiskAssessment.supersedes_id.is_not(None))
        )
        or 0
    )
    alerts_open = dict(
        session.execute(
            select(RiskAlert.alert_type, func.count())
            .where(RiskAlert.status.in_([RiskAlertStatus.OPEN, RiskAlertStatus.ACKNOWLEDGED]))
            .group_by(RiskAlert.alert_type)
        ).all()
    )
    alerts_recent = session.scalar(select(func.count()).select_from(RiskAlert).where(RiskAlert.created_at >= week)) or 0
    runs = list(session.scalars(select(RiskRun).order_by(RiskRun.id.desc()).limit(10)))
    run_counts = Counter(r.status.value for r in runs)
    last = runs[0] if runs else None
    method_counts = dict(session.execute(
        select(RiskAssessment.methodology_version, func.count()).select_from(Supplier).join(RiskAssessment, joined).where(*active).group_by(RiskAssessment.methodology_version)
    ).all())  # fmt: skip
    cov_mean = session.scalar(
        select(func.avg(RiskAssessment.evidence_coverage))
        .select_from(Supplier)
        .join(RiskAssessment, joined)
        .where(*active)
    )
    unavailable = [
        {"category": c.value, "label": d.label, "data_dependency": d.data_dependency}
        for c, d in CATEGORY_DOCS.items()
        if not d.data_available
    ]
    return {
        "generated_at": now, "methodology_version": cfg.version, "methodology_hash": config_hash(cfg),
        "counts_note": "All counts are exact counts of persisted rows for active and on-hold suppliers; none is estimated.",
        "suppliers_in_scope": total_suppliers, "assessed_suppliers": assessed_total, "not_assessed": max(0, total_suppliers - assessed_total),
        "by_status": {s.value: by_status.get(s.value, 0) for s in AssessmentStatus}, "by_classification": by_class,
        "classified": sum(by_class.values()), "insufficient_evidence": by_status.get(AssessmentStatus.INSUFFICIENT_EVIDENCE.value, 0),
        "requires_review_unreviewed": unreviewed, "high_dependency_exposure": high_dep, "with_single_source_dependency": single,
        "aging_assessments": aging, "assessment_stale_days": cfg.assessment_stale_days, "new_assessments_last_7_days_with_prior": recent_new,
        "alerts_open_by_type": {k.value: v for k, v in alerts_open.items()}, "alerts_created_last_7_days": alerts_recent,
        "methodology_versions_in_use": method_counts, "mixed_methodology": len(method_counts) > 1,
        "mean_evidence_coverage": round(cov_mean, 4) if cov_mean is not None else None,
        "max_possible_coverage": round(sum(w for k, w in cfg.weights.items() if CATEGORY_DOCS[RiskCategory(k)].data_available), 4),
        "run_health": {
            "recent_runs": len(runs), "by_status": dict(run_counts), "last_run": service.run_out(last) if last else None,
            "last_run_incomplete": bool(last and last.status in (EvalStatus.PARTIAL, EvalStatus.FAILED)),
        },
        "unavailable_categories": unavailable,
    }  # fmt: skip


def compare(session: Session, cfg: RiskConfig, supplier_ids: list[int]) -> dict[str, Any]:
    ids = list(dict.fromkeys(supplier_ids))
    if not 2 <= len(ids) <= 6:
        raise DomainValidationError("Compare between 2 and 6 distinct suppliers.")
    items = []
    for sid in ids:
        sup = service.get_supplier(session, sid)
        cur = service.current_assessment(session, sid)
        factors = service.load_factors(session, cur.id, with_evidence=False) if cur else []
        items.append({"supplier": sup, "assessment": cur, "factors": {f["category"]: f for f in factors}})
    scored = [i for i in items if i["assessment"] is not None and i["assessment"].risk_score is not None]
    reasons: list[str] = []
    missing_assessment = [i["supplier"].name for i in items if i["assessment"] is None]
    if missing_assessment:
        reasons.append("Not assessed yet: " + ", ".join(missing_assessment))
    no_score = [i["supplier"].name for i in items if i["assessment"] is not None and i["assessment"].risk_score is None]
    if no_score:
        reasons.append("Insufficient evidence (no score): " + ", ".join(no_score))
    hashes = {i["assessment"].methodology_hash for i in items if i["assessment"] is not None}
    if len(hashes) > 1:
        reasons.append("Assessments use different methodology versions or parameters.")
    if len(scored) >= 2:
        covs = [i["assessment"].evidence_coverage for i in scored]
        if max(covs) - min(covs) > cfg.compare.max_coverage_spread:
            reasons.append(f"Evidence coverage differs by more than {cfg.compare.max_coverage_spread:.0%}.")
    if any(i["assessment"] is not None and i["assessment"].status is AssessmentStatus.PARTIAL for i in items):
        reasons.append("At least one assessment is PARTIAL (coverage below the classification threshold).")
    comparable = len(scored) == len(items) and not reasons
    cats = [c.value for c in RiskCategory]
    caveats = []
    sets = [
        frozenset(c for c, f in i["factors"].items() if f["state"] == "SCORED" and f["role"] == "risk")
        for i in items
        if i["assessment"] is not None
    ]
    if len(set(sets)) > 1:
        caveats.append(
            "The suppliers were scored on different sets of risk categories; compare the per-category rows rather than the totals."
        )
    out_items = []
    for i in items:
        a = i["assessment"]
        out_items.append({
            "supplier_id": i["supplier"].id, "name": i["supplier"].name, "supplier_code": i["supplier"].supplier_code,
            "assessment_id": a.id if a else None, "status": a.status.value if a else NOT_ASSESSED, "risk_score": a.risk_score if a else None,
            "classification": a.classification.value if a and a.classification else None, "evidence_coverage": a.evidence_coverage if a else None,
            "reliability": a.reliability if a else None, "reliability_band": a.reliability_band.value if a and a.reliability_band else None,
            "methodology_version": a.methodology_version if a else None, "methodology_hash": a.methodology_hash if a else None,
            "computed_at": a.computed_at if a else None,
        })  # fmt: skip
    matrix = [{
        "category": c, "label": CATEGORY_DOCS[RiskCategory(c)].label, "role": CATEGORY_DOCS[RiskCategory(c)].role,
        "cells": [{"supplier_id": i["supplier"].id, "state": (i["factors"].get(c) or {}).get("state"), "score": (i["factors"].get(c) or {}).get("score"),
                   "confidence": (i["factors"].get(c) or {}).get("confidence"), "basis": (i["factors"].get(c) or {}).get("basis")} for i in items],
    } for c in cats]  # fmt: skip
    ranking = sorted(
        (x for x in out_items if x["risk_score"] is not None), key=lambda x: (-x["risk_score"], x["supplier_id"])
    )
    return {
        "comparable": comparable, "incomparable_reasons": reasons, "caveats": caveats, "suppliers": out_items, "matrix": matrix,
        "ranking": [x["supplier_id"] for x in ranking] if comparable else None,
        "ranking_note": None if comparable else "No ranking is shown: the scores above are not directly comparable. Per-category rows remain valid.",
    }  # fmt: skip


def geography(session: Session, region: str | None = None) -> dict[str, Any]:
    """Suppliers and facilities by registry region LABEL. Labels are text; nothing is geocoded or mapped to hazards."""
    sups = list(
        session.scalars(
            select(Supplier).where(
                Supplier.merged_into_id.is_(None), Supplier.status.in_([EntityStatus.ACTIVE, EntityStatus.ON_HOLD])
            )
        )
    )
    facs = list(
        session.scalars(
            select(Facility).where(
                Facility.merged_into_id.is_(None), Facility.status.in_([EntityStatus.ACTIVE, EntityStatus.ON_HOLD])
            )
        )
    )
    cur = {a.supplier_id: a for a in session.scalars(select(RiskAssessment).where(RiskAssessment.is_current.is_(True)))}
    regions: dict[str, dict[str, Any]] = {}

    def slot(label: str) -> dict[str, Any]:
        key = normalize_name(label) or "(no region recorded)"
        return regions.setdefault(key, {"region": label.strip() if label and label.strip() else "(no region recorded)", "key": key, "suppliers": 0, "facilities": 0,
                                        "high_or_critical": 0, "unscored": 0, "verified_coordinates": 0, "missing_or_placeholder_coordinates": 0})  # fmt: skip

    for s in sups:
        r = slot(s.country_or_region or "")
        r["suppliers"] += 1
        a = cur.get(s.id)
        if a is None or a.risk_score is None:
            r["unscored"] += 1
        elif a.classification in (RiskClass.HIGH, RiskClass.CRITICAL):
            r["high_or_critical"] += 1
    for f in facs:
        r = slot(f.country_or_region or "")
        r["facilities"] += 1
        if f.latitude is None or f.longitude is None or (f.latitude == 0 and f.longitude == 0):
            r["missing_or_placeholder_coordinates"] += 1
        elif f.location_status.value == "VERIFIED":
            r["verified_coordinates"] += 1
    total_s, total_f = len(sups), len(facs)
    rows = sorted(regions.values(), key=lambda r: (-r["suppliers"] - r["facilities"], r["key"]))
    for r in rows:
        r["supplier_share"] = round(r["suppliers"] / total_s, 4) if total_s else None
        r["facility_share"] = round(r["facilities"] / total_f, 4) if total_f else None
    if region:
        key = normalize_name(region)
        rows = [r for r in rows if r["key"] == key]
    return {
        "regions": rows, "total_suppliers": total_s, "total_facilities": total_f,
        "top_region_supplier_share": max((r["supplier_share"] or 0 for r in regions.values()), default=None),
        "caveats": [
            "Regions are the free-text country_or_region labels in the registry, compared after normalisation; no country-to-region mapping is applied.",
            "A supplier's registered region is not assumed to be where its production happens.",
            "No hazard (earthquake, flood, conflict, sanctions) exposure is stated: no authorised hazard data source exists in ATLAS.",
            "Facility coordinates are summarised for completeness only; unverified or placeholder coordinates are not used for exposure.",
        ],
    }  # fmt: skip


def dependency_view(session: Session, supplier_id: int, cfg: RiskConfig, now: datetime | None = None) -> dict[str, Any]:
    """Live (not persisted) dependency and concentration indicators for one supplier, from current graph data."""
    now = now or utcnow()
    service.get_supplier(session, supplier_id)
    facts, net = inputs.load_facts(session, [supplier_id], now.date())
    sf: SupplierFacts = facts[supplier_id]
    views = node_views(sf, net, now.date(), cfg)
    reach = inputs.load_reach(
        session, supplier_id, cfg.traversal.depth, cfg.traversal.max_nodes, cfg.traversal.max_edges
    )
    nodes = []
    for v in views:
        suppliers = [
            {
                "supplier_id": sid,
                "name": net.suppliers[sid].name if sid in net.suppliers else None,
                "share": round(v.shares.get(sid, 0), 4),
            }
            for sid in v.confirmed
        ]
        nodes.append({
            "node_type": v.key[0].value, "node_id": v.key[1], "code": v.code, "name": v.name, "confirmed_supplier_count": v.n, "unverified_alternatives": len(v.proposed),
            "sole_source_in_observed_dataset": v.n == 1, "criticality": v.subject_edge.criticality, "share_basis": v.share_basis,
            "supplier_share": round(v.shares.get(supplier_id, 0), 4), "hhi": v.hhi,
            "hhi_note": None if v.hhi is not None else "Not available: documented supply shares are missing or do not total 100%.",
            "suppliers": suppliers, "ignored_relationships": v.ignored, "relationship_ref": v.subject_edge.ref,
        })  # fmt: skip
    statuses = Counter(e.status for e in sf.edges)
    return {
        "supplier_id": supplier_id, "computed_at": now, "nodes": nodes, "dependent_products": sum(1 for v in views if v.is_product),
        "dependent_facilities": sum(1 for v in views if not v.is_product), "sole_sourced_count": sum(1 for v in views if v.n == 1),
        "own_relationships_by_status": dict(sorted(statuses.items())), "downstream_reach": reach,
        "treatment": {
            "CONFIRMED (currently effective)": "counted", "PROPOSED": "not counted; shown as unverified alternatives and in the sensitivity", 
            "REJECTED / INACTIVE": "ignored", "CONFIRMED but expired or not yet effective": "ignored and flagged",
        },
        "notes": ["Only recorded relationships are visible; an incomplete dataset can make a node look single-sourced.", "Exclusivity is not modelled and never claimed."],
    }  # fmt: skip
