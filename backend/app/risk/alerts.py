"""Deterministic review-queue alerts raised when a new assessment differs materially from the previous one.

Alerts are stored in the application (``risk_alerts``). ATLAS sends no email or external notification. Repeated
detections of the same condition bump ``occurrences`` on the live alert instead of inserting another one, and small
score movements or changes in coverage (which make scores incomparable) raise nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import AssessmentStatus, FactorState, Freshness, RiskAlertStatus, RiskAlertType, Severity
from app.models.risk import RiskAlert, RiskAssessment, RiskFactor
from app.models.types import utcnow
from app.risk.methodology import RiskConfig
from app.risk.types import AssessmentResult

ACTIVE = (RiskAlertStatus.OPEN, RiskAlertStatus.ACKNOWLEDGED)
SCORED_STATES = (AssessmentStatus.ASSESSED.value, AssessmentStatus.PARTIAL.value)


@dataclass
class AlertCounters:
    created: int = 0
    updated: int = 0
    suppressed: int = 0
    keys: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    type: RiskAlertType
    disc: str
    severity: Severity
    reason: str
    evidence: dict[str, Any]


def candidates(
    prev: RiskAssessment | None, prev_factors: dict[str, RiskFactor], new: AssessmentResult, cfg: RiskConfig
) -> list[Candidate]:
    """What changed between two consecutive assessments of one supplier. No previous assessment: nothing to compare."""
    if prev is None:
        return []
    out: list[Candidate] = []
    a = cfg.alerts
    same_method = prev.methodology_hash == new.methodology_hash
    prev_ok, new_ok = prev.status in SCORED_STATES, new.status in SCORED_STATES
    if prev_ok and new_ok and prev.risk_score is not None and new.score is not None:
        delta = round(new.score - prev.risk_score, 2)
        if not same_method:
            if abs(delta) >= a.methodology_change_points:
                out.append(Candidate(
                    RiskAlertType.METHODOLOGY_CHANGE, new.methodology_hash[:12], Severity.WARNING,
                    f"The methodology changed ({prev.methodology_version} -> {new.methodology_version}) and moved this supplier's score by {delta:+g} points; the two scores are not directly comparable.",
                    {"previous_score": prev.risk_score, "new_score": new.score, "previous_method": prev.methodology_version, "new_method": new.methodology_version},
                ))  # fmt: skip
        elif delta >= a.score_increase_points and abs(new.coverage - prev.evidence_coverage) <= a.max_coverage_delta:
            sev = (
                Severity.CRITICAL
                if new.score >= cfg.thresholds.critical
                else Severity.ERROR
                if delta >= 2.5 * a.score_increase_points
                else Severity.WARNING
            )
            out.append(Candidate(
                RiskAlertType.SCORE_INCREASE, "score", sev,
                f"Supported risk score rose by {delta:g} points ({prev.risk_score:g} -> {new.score:g}) with comparable evidence coverage.",
                {"previous_score": prev.risk_score, "new_score": new.score, "previous_coverage": prev.evidence_coverage, "new_coverage": new.coverage,
                 "drivers": [c for c in new.contributions if c["counted"]][:3]},
            ))  # fmt: skip
    if prev_ok and new.status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value:
        lost = sorted(
            f.category for f in prev_factors.values() if f.state is FactorState.SCORED and f.role == "risk"
            and next((m for m in new.missing if m["category"] == f.category.value and m["kind"] != "not_applicable"), None)
        )  # fmt: skip
        lost_names = [c.value if hasattr(c, "value") else str(c) for c in lost]
        out.append(Candidate(
            RiskAlertType.ASSESSMENT_UNAVAILABLE, "assessment", Severity.ERROR,
            "The supplier could previously be assessed but now has insufficient evidence"
            + (f" (no longer available: {', '.join(lost_names)})." if lost_names else "."),
            {"previous_status": prev.status, "lost_categories": lost_names},
        ))  # fmt: skip
    prev_crit = set(prev.data_basis.get("single_source_critical", [])) if prev.data_basis else set()
    now_crit = set(new.data_basis.get("single_source_critical", []))
    for node in sorted(now_crit - prev_crit):
        out.append(Candidate(
            RiskAlertType.NEW_SINGLE_SOURCE, node, Severity.ERROR,
            f"{node} (a high- or critical-criticality dependency) is now served by this supplier as its only confirmed supplier in the observed dataset.",
            {"node": node, "claim": "ONLY_CONFIRMED_SUPPLIER_IN_OBSERVED_DATASET"},
        ))  # fmt: skip
    for f in new.factors:
        pf = prev_factors.get(f.category.value)
        if pf is None or f.role != "risk":
            continue
        became_stale = (
            f.state is FactorState.SCORED and f.freshness is Freshness.STALE and pf.freshness is not Freshness.STALE
        )
        stale_excluded = (
            pf.state is FactorState.SCORED and f.state is FactorState.UNAVAILABLE and f.details.get("kind") == "stale"
        )
        if became_stale or stale_excluded:
            out.append(Candidate(
                RiskAlertType.EVIDENCE_STALE, f.category.value, Severity.WARNING,
                f"Evidence behind {f.category.value} is now stale" + (" and was excluded from scoring." if stale_excluded else "."),
                {"category": f.category.value, "previous_freshness": pf.freshness.value, "excluded": stale_excluded},
            ))  # fmt: skip
    prev_edges = (prev.data_basis or {}).get("critical_edges", {})
    for ref, now_e in sorted(new.data_basis.get("critical_edges", {}).items()):
        was = prev_edges.get(ref)
        if was and was["status"] == "CONFIRMED" and now_e["status"] != "CONFIRMED":
            out.append(Candidate(
                RiskAlertType.RELATIONSHIP_UNVERIFIED, ref, Severity.CRITICAL if now_e["criticality"] == "CRITICAL" else Severity.ERROR,
                f"Relationship {ref} ({now_e['criticality']} criticality) is no longer confirmed (now {now_e['status']}).",
                {"ref": ref, "previous_status": was["status"], "status": now_e["status"], "criticality": now_e["criticality"]},
            ))  # fmt: skip
    ru_new = next((f for f in new.factors if f.category.value == "RELATIONSHIP_UNCERTAINTY"), None)
    ru_prev = prev_factors.get("RELATIONSHIP_UNCERTAINTY")
    if ru_new and ru_prev and ru_new.state is FactorState.SCORED and ru_prev.state is FactorState.SCORED:
        d = (ru_new.score or 0) - (ru_prev.score or 0)
        if d >= a.uncertainty_increase_points:
            out.append(Candidate(
                RiskAlertType.RELATIONSHIP_UNVERIFIED, "uncertainty", Severity.WARNING,
                f"The share of unverified relationships rose by {d:g} points.", {"previous": ru_prev.score, "new": ru_new.score},
            ))  # fmt: skip
    return out


def upsert(session: Session, supplier_id: int, assessment_id: int, run_id: int | None, cand: Candidate, counters: AlertCounters,
           cap: int) -> None:  # fmt: skip
    key = f"{supplier_id}:{cand.type.value}:{cand.disc}"[:120]
    now = utcnow()
    live = session.scalar(select(RiskAlert).where(RiskAlert.alert_key == key, RiskAlert.status.in_(ACTIVE)))
    if live is not None:
        live.last_seen_at, live.updated_at, live.occurrences = now, now, live.occurrences + 1
        live.assessment_id, live.reason, live.evidence, live.run_id = (
            assessment_id,
            cand.reason[:400],
            cand.evidence,
            run_id,
        )
        counters.updated += 1
        return
    if counters.created >= cap:
        counters.suppressed += 1
        return
    session.add(RiskAlert(
        alert_key=key, supplier_id=supplier_id, assessment_id=assessment_id, alert_type=cand.type, severity=cand.severity,
        status=RiskAlertStatus.OPEN, reason=cand.reason[:400], evidence=cand.evidence, run_id=run_id, created_at=now, updated_at=now,
        last_seen_at=now, occurrences=1,
    ))  # fmt: skip
    counters.created += 1
    counters.keys.append(key)
