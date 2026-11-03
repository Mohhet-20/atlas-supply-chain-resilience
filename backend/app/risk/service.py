"""Persistence and read models for supplier risk: store assessments, history, review state, effective overrides."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import EntityMergedError, NotFoundError
from app.models.enums import (
    AssessmentStatus,
    ReliabilityBand,
    ReviewAction,
    RiskAlertStatus,
    RiskCategory,
    RiskClass,
)
from app.models.risk import RiskAlert, RiskAssessment, RiskEvidence, RiskFactor, RiskReview, RiskRun
from app.models.supplier import Supplier
from app.risk import alerts as alert_rules
from app.risk.methodology import CATEGORY_DOCS, RiskConfig
from app.risk.types import AssessmentResult

INTERPRETATION = {
    "meaning": "A rule-based indicator of supported supply-chain vulnerability, 0 (lowest) to 100 (highest), from recorded evidence only.",
    "not_a_probability": True,
    "not_a_prediction": "The score is not a probability of supplier failure and was not trained or validated against outcomes.",
    "missing_data": "Missing evidence is shown as unavailable and is never treated as low risk.",
}


def new_uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


# --------------------------------------------------------------------------------------------------- persistence


def _current(session: Session, supplier_id: int) -> RiskAssessment | None:
    return session.scalar(
        select(RiskAssessment).where(RiskAssessment.supplier_id == supplier_id, RiskAssessment.is_current.is_(True))
    )


def store(session: Session, run: RiskRun, supplier_id: int, res: AssessmentResult, now: datetime, cfg: RiskConfig,
          counters: alert_rules.AlertCounters) -> tuple[RiskAssessment, str]:  # fmt: skip
    """Insert a new assessment when the result differs from the current one; otherwise confirm the current one.

    Returns (assessment, "CREATED" | "UNCHANGED"). History is never overwritten: a changed result supersedes the
    previous row. The (run, supplier) unique constraint plus this check make a repeated execution harmless.
    """
    same_run = session.scalar(
        select(RiskAssessment).where(RiskAssessment.run_id == run.id, RiskAssessment.supplier_id == supplier_id)
    )
    if same_run is not None:
        return same_run, "UNCHANGED"
    cur = _current(session, supplier_id)
    if cur is not None and cur.fingerprint == res.fingerprint and cur.methodology_hash == res.methodology_hash:
        cur.last_confirmed_at, cur.last_confirmed_run_id = now, run.id
        return cur, "UNCHANGED"
    prev_factors: dict[str, RiskFactor] = {}
    if cur is not None:
        prev_factors = {
            f.category.value: f for f in session.scalars(select(RiskFactor).where(RiskFactor.assessment_id == cur.id))
        }
        cur.is_current, cur.superseded_at = False, now
        session.flush()  # free the "one current assessment" slot before inserting the replacement
    row = RiskAssessment(
        assessment_uid=new_uid("RA"), supplier_id=supplier_id, run_id=run.id, computed_at=now, status=AssessmentStatus(res.status),
        methodology_version=res.methodology_version, methodology_hash=res.methodology_hash, risk_score=res.score,
        classification=RiskClass(res.classification) if res.classification else None, evidence_coverage=res.coverage,
        effective_weight=res.effective_weight, reliability=res.reliability,
        reliability_band=ReliabilityBand(res.reliability_band) if res.reliability_band else None, scored_factors=res.scored_factors,
        contributions=res.contributions, missing=res.missing, actions=res.actions, data_basis=res.data_basis, fingerprint=res.fingerprint,
        evidence_latest_at=res.evidence_latest_at, evidence_earliest_at=res.evidence_earliest_at, requires_review=res.requires_review,
        review_state="UNREVIEWED", is_current=True, supersedes_id=cur.id if cur else None, last_confirmed_at=now, last_confirmed_run_id=run.id,
    )  # fmt: skip
    session.add(row)
    session.flush()
    for f in res.factors:
        fr = RiskFactor(
            assessment_id=row.id, category=f.category, role=f.role, state=f.state, basis=f.basis, score=f.score,
            severity=RiskClass(f.severity) if f.severity else None, weight=f.weight, explanation=f.explanation[:800],
            unavailable_reason=(f.unavailable_reason or None) and f.unavailable_reason[:400], missing_inputs=f.missing_inputs,
            evidence_observed_at=f.evidence_observed_at, freshness=f.freshness, confidence=f.confidence, quality_context=f.quality_context,
            requires_review=f.requires_review, details=f.details, method_version=res.methodology_version,
        )  # fmt: skip
        session.add(fr)
        session.flush()
        for e in f.evidence:
            session.add(RiskEvidence(
                assessment_id=row.id, factor_id=fr.id, evidence_type=e.etype, ref=e.ref[:80], summary=e.summary[:400], observed_at=e.observed_at,
                provenance=e.provenance, source_namespace=(e.source_namespace or None) and e.source_namespace[:80], reasoning=e.reasoning[:400],
            ))  # fmt: skip
    for cand in alert_rules.candidates(cur, prev_factors, res, cfg):
        alert_rules.upsert(session, supplier_id, row.id, run.id, cand, counters, cfg.alerts.max_alerts_per_run)
    return row, "CREATED"


# ------------------------------------------------------------------------------------------------------ read models


def get_supplier(session: Session, supplier_id: int) -> Supplier:
    s = session.get(Supplier, supplier_id)
    if s is None:
        raise NotFoundError("Supplier not found.")
    if s.merged_into_id is not None:
        raise EntityMergedError(f"Supplier {supplier_id} was merged into {s.merged_into_id}; use the surviving record.")
    return s


def evidence_out(e: RiskEvidence) -> dict[str, Any]:
    return {
        "evidence_type": e.evidence_type.value, "ref": e.ref, "summary": e.summary, "observed_at": e.observed_at, "provenance": e.provenance,
        "source_namespace": e.source_namespace, "reasoning": e.reasoning,
    }  # fmt: skip


def factor_out(f: RiskFactor, evidence: list[RiskEvidence] | None = None) -> dict[str, Any]:
    doc = CATEGORY_DOCS[f.category]
    out = {
        "id": f.id, "category": f.category.value, "label": doc.label, "role": f.role, "state": f.state.value, "basis": f.basis.value,
        "score": f.score, "severity": f.severity.value if f.severity else None, "weight": f.weight, "explanation": f.explanation,
        "unavailable_reason": f.unavailable_reason, "missing_inputs": f.missing_inputs or [], "evidence_observed_at": f.evidence_observed_at,
        "freshness": f.freshness.value, "confidence": f.confidence, "quality_context": f.quality_context or {},
        "requires_review": f.requires_review, "details": f.details or {}, "method_version": f.method_version,
        "data_available": doc.data_available, "evidence": None,
    }  # fmt: skip
    if evidence is not None:
        out["evidence"] = [evidence_out(e) for e in evidence]
    return out


def assessment_out(a: RiskAssessment) -> dict[str, Any]:
    return {
        "id": a.id, "assessment_uid": a.assessment_uid, "supplier_id": a.supplier_id, "run_id": a.run_id, "computed_at": a.computed_at,
        "status": a.status.value, "methodology_version": a.methodology_version, "methodology_hash": a.methodology_hash, "risk_score": a.risk_score,
        "classification": a.classification.value if a.classification else None, "evidence_coverage": a.evidence_coverage,
        "effective_weight": a.effective_weight, "reliability": a.reliability, "reliability_band": a.reliability_band.value if a.reliability_band else None,
        "scored_factors": a.scored_factors, "contributions": a.contributions or [], "missing": a.missing or [], "actions": a.actions or [],
        "data_basis": a.data_basis or {}, "evidence_latest_at": a.evidence_latest_at, "evidence_earliest_at": a.evidence_earliest_at,
        "requires_review": a.requires_review, "review_state": a.review_state, "is_current": a.is_current, "superseded_at": a.superseded_at,
        "supersedes_id": a.supersedes_id, "last_confirmed_at": a.last_confirmed_at, "rank_comparable": a.status is AssessmentStatus.ASSESSED,
        "interpretation": INTERPRETATION,
    }  # fmt: skip


def load_factors(session: Session, assessment_id: int, with_evidence: bool = True) -> list[dict[str, Any]]:
    facs = list(
        session.scalars(select(RiskFactor).where(RiskFactor.assessment_id == assessment_id).order_by(RiskFactor.id))
    )
    ev: dict[int, list[RiskEvidence]] = {}
    if with_evidence:
        for e in session.scalars(
            select(RiskEvidence).where(RiskEvidence.assessment_id == assessment_id).order_by(RiskEvidence.id)
        ):
            ev.setdefault(e.factor_id, []).append(e)
    order = {c: i for i, c in enumerate(RiskCategory)}
    return [
        factor_out(f, ev.get(f.id, []) if with_evidence else None)
        for f in sorted(facs, key=lambda x: order[x.category])
    ]


def current_assessment(session: Session, supplier_id: int) -> RiskAssessment | None:
    return _current(session, supplier_id)


def history(session: Session, supplier_id: int, limit: int, offset: int) -> tuple[list[RiskAssessment], int]:
    from sqlalchemy import func

    base = select(RiskAssessment).where(RiskAssessment.supplier_id == supplier_id)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(session.scalars(base.order_by(RiskAssessment.id.desc()).limit(limit).offset(offset)))
    return rows, total


def history_changes(rows: list[RiskAssessment]) -> list[dict[str, Any]]:
    """Newest-first list annotated with the change against the previous (older) assessment in the same list."""
    out = []
    for i, a in enumerate(rows):
        older = rows[i + 1] if i + 1 < len(rows) else None
        d = assessment_out(a)
        d["score_change"] = None
        d["change_comparable"] = None
        if older is not None:
            same = older.methodology_hash == a.methodology_hash
            d["change_comparable"] = same
            if a.risk_score is not None and older.risk_score is not None:
                d["score_change"] = round(a.risk_score - older.risk_score, 2)
            d["previous_assessment_id"] = older.id
        out.append(d)
    return out


def compare_assessments(session: Session, a: RiskAssessment, b: RiskAssessment) -> dict[str, Any]:
    """Factor-level difference between two assessments of one supplier (older -> newer is the caller's choice)."""
    fa = {f["category"]: f for f in load_factors(session, a.id, with_evidence=False)}
    fb = {f["category"]: f for f in load_factors(session, b.id, with_evidence=False)}
    rows = []
    for cat in [c.value for c in RiskCategory]:
        x, y = fa.get(cat), fb.get(cat)
        rows.append({
            "category": cat, "from_state": x["state"] if x else None, "to_state": y["state"] if y else None,
            "from_score": x["score"] if x else None, "to_score": y["score"] if y else None,
            "change": round(y["score"] - x["score"], 2) if x and y and x["score"] is not None and y["score"] is not None else None,
            "changed": (x or {}).get("state") != (y or {}).get("state") or (x or {}).get("score") != (y or {}).get("score"),
        })  # fmt: skip
    return {
        "from_assessment_id": a.id, "to_assessment_id": b.id, "comparable": a.methodology_hash == b.methodology_hash,
        "reason": None if a.methodology_hash == b.methodology_hash else "Different methodology versions or parameters: scores are not directly comparable.",
        "score_change": round(b.risk_score - a.risk_score, 2) if a.risk_score is not None and b.risk_score is not None else None,
        "coverage_change": round(b.evidence_coverage - a.evidence_coverage, 4), "factors": rows,
    }  # fmt: skip


# -------------------------------------------------------------------------------------------- reviews and overrides


def reviews_for(session: Session, supplier_id: int, limit: int = 50) -> list[RiskReview]:
    return list(
        session.scalars(
            select(RiskReview).where(RiskReview.supplier_id == supplier_id).order_by(RiskReview.id.desc()).limit(limit)
        )
    )


def review_out(r: RiskReview) -> dict[str, Any]:
    return {
        "id": r.id, "supplier_id": r.supplier_id, "assessment_id": r.assessment_id, "action": r.action.value, "factor_category": r.factor_category,
        "actor": r.actor, "actor_role": r.actor_role, "created_at": r.created_at, "note": r.note,
        "override_classification": r.override_classification.value if r.override_classification else None,
        "override_expires_at": r.override_expires_at, "target_review_id": r.target_review_id, "original_status": r.original_status,
        "original_classification": r.original_classification, "original_score": r.original_score,
    }  # fmt: skip


def active_override(session: Session, supplier_id: int, now: datetime, current_id: int | None) -> dict[str, Any] | None:
    """The latest unrevoked override. The automated result stays on the assessment; this is shown beside it."""
    rows = list(session.scalars(
        select(RiskReview)
        .where(RiskReview.supplier_id == supplier_id, RiskReview.action.in_([ReviewAction.OVERRIDE, ReviewAction.REVOKE_OVERRIDE]))
        .order_by(RiskReview.id)
    ))  # fmt: skip
    revoked = {r.target_review_id for r in rows if r.action is ReviewAction.REVOKE_OVERRIDE}
    live = [r for r in rows if r.action is ReviewAction.OVERRIDE and r.id not in revoked]
    if not live:
        return None
    r = live[-1]
    expired = r.override_expires_at is not None and r.override_expires_at < now
    out = review_out(r)
    out |= {
        "expired": expired, "stale": current_id is not None and r.assessment_id != current_id,
        "effective": not expired and (current_id is None or r.assessment_id == current_id),
        "note_on_staleness": "The assessment was recalculated after this override; re-review it." if current_id and r.assessment_id != current_id else None,
    }  # fmt: skip
    return out


def alerts_for(session: Session, supplier_id: int, only_active: bool = True, limit: int = 50) -> list[RiskAlert]:
    q = select(RiskAlert).where(RiskAlert.supplier_id == supplier_id)
    if only_active:
        q = q.where(RiskAlert.status.in_([RiskAlertStatus.OPEN, RiskAlertStatus.ACKNOWLEDGED]))
    return list(session.scalars(q.order_by(RiskAlert.id.desc()).limit(limit)))


def alert_out(a: RiskAlert, supplier_name: str | None = None) -> dict[str, Any]:
    return {
        "id": a.id, "supplier_id": a.supplier_id, "supplier_name": supplier_name, "assessment_id": a.assessment_id, "alert_type": a.alert_type.value,
        "severity": a.severity.value, "status": a.status.value, "reason": a.reason, "evidence": a.evidence or {}, "run_id": a.run_id,
        "created_at": a.created_at, "updated_at": a.updated_at, "last_seen_at": a.last_seen_at, "occurrences": a.occurrences,
        "resolved_at": a.resolved_at, "resolved_by": a.resolved_by, "resolution_note": a.resolution_note, "version": a.version,
        "delivery": "in-application review queue only; no email or external notification is sent",
    }  # fmt: skip


def run_out(r: RiskRun) -> dict[str, Any]:
    completed = r.assessed + r.partial + r.insufficient
    return {
        "id": r.id, "run_uid": r.run_uid, "scope_type": r.scope_type.value, "scope_ref": r.scope_ref or {}, "status": r.status.value, "trigger": r.trigger,
        "reason": r.reason, "requested_by": r.requested_by, "idempotency_key": r.idempotency_key, "queued_at": r.queued_at, "started_at": r.started_at,
        "finished_at": r.finished_at, "duration_ms": r.duration_ms, "methodology_version": r.methodology_version, "methodology_hash": r.methodology_hash,
        "graph_snapshot": r.graph_snapshot or {}, "examined": r.examined, "completed_assessments": completed, "assessed": r.assessed, "partial": r.partial,
        "insufficient_evidence": r.insufficient, "created": r.created, "unchanged": r.unchanged, "failed": r.failed, "skipped": r.skipped,
        "alerts_created": r.alerts_created, "alerts_updated": r.alerts_updated, "failures": r.failures or [], "truncated": r.truncated,
        "truncation_reasons": r.truncation_reasons or [], "error": r.error,
    }  # fmt: skip
