"""Detectors: read signals ATLAS already stores and return *matches*. They never write, call out, or evaluate user-supplied code.

Every detector reports how many subjects it evaluated, how many had no usable data and why, so "no alert" can be explained.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerting.config import LIMITS, OP_TEXT, OPERATORS
from app.models.alerting import AlertEvent
from app.models.delivery import DeliveryAssessment
from app.models.enums import EntityStatus, SimStatus
from app.models.forecast import ForecastAlert, ForecastRun
from app.models.impact import ImpactAnalysis
from app.models.ingestion import IngestionBatch
from app.models.inventory import StockoutAnalysis, StockoutFinding
from app.models.invopt import ReplenishmentRun
from app.models.resilience import ResilienceRun
from app.models.risk import RiskAssessment
from app.models.scenario import SimulationRun
from app.models.source import DataSource
from app.models.sourcing import SourcingRun
from app.models.supplier import Supplier


@dataclass
class Match:
    entity_type: str
    entity_id: str
    entity_label: str
    signal: str
    value: float | None
    unit: str | None
    operator: str | None
    threshold: float | None
    basis: str
    title: str
    summary: str
    evidence: dict[str, Any]
    link: str | None = None
    observed_at: datetime | None = None
    dims: str = ""  # extra deduplication dimension (e.g. a run uid); distinct conditions on one entity stay distinct
    event_id: int | None = None
    critical: bool = False
    confidence: str | None = None
    days_to_threshold: float | None = None
    extra_source: str | None = None


@dataclass
class Result:
    matches: list[Match] = field(default_factory=list)
    evaluated: int = 0
    no_data: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    entities: set[tuple[str, str]] = field(
        default_factory=set
    )  # subjects that had usable data (basis for auto-resolution)
    complete: bool = (
        False  # True: every subject is considered evaluated (absence of a match means the condition is clear)
    )

    @property
    def outcome(self) -> str:
        if self.matches:
            return "matched"
        return "no_data" if self.evaluated == 0 and self.no_data > 0 else ("no_match" if self.evaluated else "no_data")


def _aware(d: datetime | None) -> datetime | None:
    return None if d is None else (d if d.tzinfo else d.replace(tzinfo=timezone.utc))


def _cmp(op: str, a: float, b: float) -> bool:
    return OPERATORS[op](a, b)


def supplier_risk_score(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    op, thr = p["operator"], p["threshold"]
    rows = db.execute(
        select(RiskAssessment, Supplier)
        .join(Supplier, Supplier.id == RiskAssessment.supplier_id)
        .where(
            RiskAssessment.is_current.is_(True),
            Supplier.status == EntityStatus.ACTIVE,
            Supplier.merged_into_id.is_(None),
        )
        .order_by(RiskAssessment.risk_score.desc().nulls_last(), RiskAssessment.id)
        .limit(20001)
    ).all()
    for a, s in rows[:20000]:
        if a.risk_score is None:
            r.no_data += 1
            continue
        if a.evidence_coverage < p["min_evidence_coverage"]:
            r.skipped += 1
            continue
        r.evaluated += 1
        r.entities.add(("supplier", str(s.id)))
        if not _cmp(op, a.risk_score, thr):
            continue
        if len(r.matches) >= LIMITS["matches_per_rule"]:
            r.truncated = True
            continue
        cv = p.get("critical_value")
        r.matches.append(
            Match("supplier", str(s.id), f"{s.name} ({s.supplier_code})", "risk_score", round(a.risk_score, 2), "score", op, thr, "assessed",
                  f"Supplier risk {a.risk_score:.0f} at {s.name}",
                  f"Current rule-based risk score {a.risk_score:.1f}/100 is {OP_TEXT[op]} {thr:g}. Classification {a.classification.value if a.classification else 'unclassified'}; evidence coverage {a.evidence_coverage:.0%}.",
                  {"assessment_uid": a.assessment_uid, "methodology_version": a.methodology_version, "computed_at": a.computed_at.isoformat(), "evidence_coverage": a.evidence_coverage, "reliability_band": a.reliability_band.value if a.reliability_band else None, "heuristic": "Rule-based score over stored evidence; not a prediction of an incident."},
                  f"/risk?supplier={s.id}", _aware(a.computed_at), confidence=(a.reliability_band.value if a.reliability_band else None),
                  critical=cv is not None and _cmp(op, a.risk_score, cv)))  # fmt: skip
    if r.no_data:
        r.notes.append(
            f"{r.no_data} supplier(s) have no score (insufficient evidence) and were not evaluated; unknown is not treated as low or high."
        )
    if r.skipped:
        r.notes.append(
            f"{r.skipped} supplier(s) were skipped because evidence coverage is below {p['min_evidence_coverage']:.0%}."
        )
    return r


def stockout_status(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    an = db.scalar(
        select(StockoutAnalysis)
        .where(
            StockoutAnalysis.kind == "BASELINE", StockoutAnalysis.status.in_((SimStatus.COMPLETED, SimStatus.PARTIAL))
        )
        .order_by(StockoutAnalysis.id.desc())
        .limit(1)
    )
    if an is None:
        r.no_data = 1
        r.notes.append("No completed baseline stockout analysis exists.")
        return r
    age = now - (_aware(an.finished_at or an.as_of) or now)
    if age > timedelta(days=p["max_age_days"]):
        r.no_data = 1
        r.notes.append(
            f"The latest baseline analysis {an.analysis_uid} is {age.days} day(s) old (limit {p['max_age_days']}); stale findings do not raise alerts."
        )
        return r
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    rows = db.scalars(
        select(StockoutFinding)
        .where(StockoutFinding.analysis_id == an.id)
        .order_by(StockoutFinding.severity_rank)
        .limit(LIMITS["matches_per_rule"] * 4)
    ).all()
    for f in rows:
        r.evaluated += 1
        r.entities.add(("product_facility", f"{f.product_id}:{f.facility_id}"))
        if f.status not in p["statuses"]:
            continue
        if rank.get(f.confidence, 0) < rank[p["min_confidence"]]:
            r.skipped += 1
            continue
        if len(r.matches) >= LIMITS["matches_per_rule"]:
            r.truncated = True
            continue
        observed = f.status == "current_shortage"
        r.matches.append(
            Match("product_facility", f"{f.product_id}:{f.facility_id}", f"{f.product_name} at {f.facility_name}", "stockout_status", None, None, None, None,
                  "observed" if observed else "predicted", f"{f.status.replace('_', ' ').capitalize()}: {f.product_name} at {f.facility_name}",
                  f"{f.reason}. Confidence {f.confidence.lower()}; input coverage {f.coverage:.0%}. {'Observed from recorded positions.' if observed else 'A projection, not an observed shortage.'}",
                  {"analysis_uid": an.analysis_uid, "as_of": _aware(an.as_of).isoformat() if an.as_of else None, "status": f.status, "confidence": f.confidence, "coverage": f.coverage, "algorithm_version": an.algorithm_version},
                  "/inventory", _aware(an.as_of), confidence=f.confidence, critical=f.status == "current_shortage"))  # fmt: skip
    return r


def delivery_late_risk(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    since = now - timedelta(days=p["window_days"])
    seen: set[tuple[Any, ...]] = set()
    rows = db.scalars(
        select(DeliveryAssessment)
        .where(DeliveryAssessment.as_of >= since, DeliveryAssessment.as_of <= now)
        .order_by(DeliveryAssessment.id.desc())
        .limit(5000)
    ).all()
    for a in rows:
        key = (a.subject, a.delivery_ref, a.product_id, a.supplier_id, a.destination_facility_id)
        if key in seen:
            continue  # newest assessment per delivery or lane only
        seen.add(key)
        risk = a.risk or {}
        if a.status != "ASSESSED" or risk.get("score") is None:
            r.no_data += 1
            continue
        if risk.get("kind") == "historical_rate":
            r.skipped += 1  # a supplier's historical late rate is not a per-delivery prediction
            continue
        r.evaluated += 1
        if risk["score"] < p["threshold"]:
            continue
        if len(r.matches) >= LIMITS["matches_per_rule"]:
            r.truncated = True
            continue
        what = (
            f"delivery {a.delivery_ref}"
            if a.subject == "DELIVERY" and a.delivery_ref
            else f"lane (supplier {a.supplier_id} → facility {a.destination_facility_id})"
        )
        calibrated = risk.get("kind") == "calibrated_probability"
        r.matches.append(
            Match("delivery" if a.subject == "DELIVERY" else "lane", a.delivery_ref or f"{a.supplier_id}:{a.destination_facility_id}", what, "late_risk_score", round(float(risk["score"]), 4), "score", "gte", p["threshold"], "predicted",
                  f"Predicted late-delivery risk for {what}", f"Model late-risk score {risk['score']:.2f} ≥ {p['threshold']:g} ({'calibrated probability' if calibrated else 'uncalibrated score, not a probability'}). A prediction, not an observed delay.",
                  {"assessment_uid": a.assessment_uid, "score_kind": risk.get("kind"), "band": risk.get("band"), "as_of": _aware(a.as_of).isoformat(), "algorithm_version": a.algorithm_version, "supplier_id": a.supplier_id, "facility_id": a.destination_facility_id},
                  "/leadtime", _aware(a.as_of), confidence="HIGH" if calibrated else "LOW"))  # fmt: skip
    if r.skipped:
        r.notes.append(f"{r.skipped} assessment(s) carry only a historical late rate and were excluded.")
    return r


def scenario_impact(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    since = now - timedelta(days=p["window_days"])
    q = (
        select(ImpactAnalysis)
        .where(ImpactAnalysis.status == SimStatus.COMPLETED, ImpactAnalysis.finished_at >= since)
        .order_by(ImpactAnalysis.id.desc())
        .limit(500)
    )
    for a in db.scalars(q):
        v = (a.summary or {}).get(p["metric"])
        if not isinstance(v, int | float):
            r.no_data += 1
            continue
        r.evaluated += 1
        if v < p["threshold"] or len(r.matches) >= LIMITS["matches_per_rule"]:
            r.truncated = r.truncated or (v >= p["threshold"])
            continue
        r.matches.append(
            Match("scenario", str(a.scenario_id), f"Scenario {a.scenario_id} v{a.scenario_version}", p["metric"], float(v), "entities", "gte", float(p["threshold"]), "hypothetical",
                  f"Hypothetical: scenario {a.scenario_id} would reach {v} {p['metric'].replace('_', ' ')}",
                  f"This is a what-if analysis, not an active disruption. Structural reach: {p['metric'].replace('_', ' ')} = {v} (threshold {p['threshold']}). Reach is not a quantified loss.",
                  {"analysis_uid": a.analysis_uid, "scenario_id": a.scenario_id, "scenario_version": a.scenario_version, "finished_at": _aware(a.finished_at).isoformat() if a.finished_at else None, "reachability_note": (a.summary or {}).get("reachability_note")},
                  "/scenarios", _aware(a.finished_at), dims=a.analysis_uid))  # fmt: skip
    return r


def source_ingest_failures(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result(complete=True)
    since = now - timedelta(days=p["window_days"])
    rows = db.execute(
        select(IngestionBatch.source_id, func.count(), func.max(IngestionBatch.started_at))
        .where(IngestionBatch.status.in_(("FAILED", "REJECTED")), IngestionBatch.started_at >= since)
        .group_by(IngestionBatch.source_id)
    ).all()
    for sid, n, last in rows:
        r.evaluated += 1
        if n < p["min_failures"]:
            continue
        src = db.get(DataSource, sid)
        reasons = Counter(
            x
            for x in db.scalars(
                select(IngestionBatch.failure_reason)
                .where(
                    IngestionBatch.source_id == sid,
                    IngestionBatch.status.in_(("FAILED", "REJECTED")),
                    IngestionBatch.started_at >= since,
                )
                .limit(200)
            )
            if x
        )
        name = src.name if src else f"source {sid}"
        r.matches.append(
            Match("source", str(sid), name, "failed_batches", float(n), "batches", "gte", float(p["min_failures"]), "observed", f"Repeated ingestion failures: {name}",
                  f"{n} ingestion batch(es) failed or were rejected in the last {p['window_days']} day(s) (threshold {p['min_failures']}). One alert for the source, not one per row.",
                  {"failed_batches": n, "window_days": p["window_days"], "latest_failure_at": _aware(last).isoformat() if last else None, "top_reasons": [{"reason": k[:120], "count": v} for k, v in reasons.most_common(3)]},
                  "/ingestion", _aware(last), critical=False))  # fmt: skip
    return r


def source_stale(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    limit = timedelta(hours=p["max_age_hours"])
    for src in db.scalars(select(DataSource).where(DataSource.status == "ACTIVE")):
        last = _aware(
            db.scalar(
                select(func.max(IngestionBatch.finished_at)).where(
                    IngestionBatch.source_id == src.id,
                    IngestionBatch.status.in_(("COMMITTED", "COMPLETED_WITH_ERRORS")),
                )
            )
        )
        if last is None:
            r.no_data += 1
            continue
        r.evaluated += 1
        r.entities.add(("source", str(src.id)))
        age = now - last
        if age <= limit:
            continue
        hrs = age.total_seconds() / 3600
        r.matches.append(
            Match(
                "source",
                str(src.id),
                src.name,
                "hours_since_last_batch",
                round(hrs, 1),
                "hours",
                "gt",
                float(p["max_age_hours"]),
                "observed",
                f"Stale data source: {src.name}",
                f"No batch has been committed for {hrs:.0f} hours (limit {p['max_age_hours']}).",
                {"last_batch_at": last.isoformat(), "age_hours": round(hrs, 1)},
                "/ingestion",
                last,
                dims="stale",
            )  # fmt: skip
        )
    if r.no_data:
        r.notes.append(f"{r.no_data} active source(s) have never delivered a batch; staleness is unknown, not assumed.")
    return r


_RUNS: dict[str, tuple[Any, str]] = {
    "simulation": (SimulationRun, "run_uid"), "resilience": (ResilienceRun, "run_uid"), "sourcing": (SourcingRun, "run_uid"),
    "replenishment": (ReplenishmentRun, "run_uid"), "forecast": (ForecastRun, "run_uid"), "stockout": (StockoutAnalysis, "analysis_uid"),
    "impact": (ImpactAnalysis, "analysis_uid"),
}  # fmt: skip
INFEASIBLE = ("infeasible", "timed_out", "insufficient_data", "invalid_input")


def run_outcome(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    since = now - timedelta(days=p["window_days"])
    for kind in p["kinds"]:
        model, uid = _RUNS[kind]
        when = getattr(model, "queued_at", None) or model.id
        cond = model.status == SimStatus.FAILED
        if p["condition"] == "infeasible":
            if not hasattr(model, "result_status"):
                continue
            cond = (model.status == SimStatus.COMPLETED) & model.result_status.in_(INFEASIBLE)
        q = select(model).where(cond).order_by(model.id.desc()).limit(200)
        if hasattr(model, "queued_at"):
            q = q.where(when >= since)
        for run in db.scalars(q):
            r.evaluated += 1
            if len(r.matches) >= LIMITS["matches_per_rule"]:
                r.truncated = True
                break
            ident = getattr(run, uid)
            res = getattr(run, "result_status", None)
            err = getattr(run, "error", None) or getattr(run, "failure_reason", None)
            tech = p["condition"] == "failed"
            r.matches.append(
                Match(
                    "run",
                    f"{kind}:{ident}",
                    f"{kind} run {ident}",
                    "run_outcome",
                    None,
                    None,
                    None,
                    None,
                    "observed",
                    f"{kind.capitalize()} run {'failed' if tech else 'is infeasible'}: {ident}",
                    (
                        f"Technical failure: the run ended FAILED. {str(err)[:160] if err else 'No error text was recorded.'}"
                        if tech
                        else f"The run completed but its result status is '{res}': the plan is operationally infeasible or the data was insufficient. The software did not fail."
                    ),
                    {
                        "kind": kind,
                        "run": ident,
                        "status": run.status.value,
                        "result_status": res,
                        "failure_class": "technical" if tech else "operational",
                    },
                    f"/{ {'simulation': 'scenarios', 'impact': 'impact', 'stockout': 'inventory', 'forecast': 'forecast'}.get(kind, kind) }",
                    _aware(getattr(run, "finished_at", None) or getattr(run, "queued_at", None)),
                )  # fmt: skip
            )
    return r


def forecast_monitoring(db: Session, p: dict[str, Any], now: datetime) -> Result:
    r = Result()
    since = now - timedelta(days=p["window_days"])
    for a in db.scalars(
        select(ForecastAlert)
        .where(ForecastAlert.created_at >= since, ForecastAlert.severity.in_(p["severities"]))
        .order_by(ForecastAlert.id.desc())
        .limit(LIMITS["matches_per_rule"])
    ):
        r.evaluated += 1
        r.matches.append(
            Match(
                "product_facility",
                f"{a.product_id}:{a.facility_id}",
                f"Product {a.product_id} at facility {a.facility_id}",
                a.kind,
                None,
                None,
                None,
                None,
                "assessed",
                f"Forecast monitoring: {a.kind.replace('_', ' ')}",
                a.message[:500],
                {
                    "forecast_alert_id": a.id,
                    "fingerprint": a.fingerprint,
                    "run_id": a.run_id,
                    "severity": a.severity,
                    "evidence": a.evidence,
                },
                "/forecast",
                _aware(a.created_at),
                dims=a.fingerprint,
            )  # fmt: skip
        )
    return r


PULL = {
    "supplier_risk_score": supplier_risk_score, "stockout_status": stockout_status, "delivery_late_risk": delivery_late_risk,
    "scenario_impact": scenario_impact, "source_ingest_failures": source_ingest_failures, "source_stale": source_stale,
    "run_outcome": run_outcome, "forecast_monitoring": forecast_monitoring,
}  # fmt: skip


def event_threshold(ev: AlertEvent, p: dict[str, Any]) -> Result:
    r = Result()
    if ev.event_type != "metric.observation":
        return r
    pl = ev.payload or {}
    if pl.get("metric") != p["metric"]:
        return r
    if pl.get("unit") != p["unit"]:
        r.skipped += 1
        r.notes.append(
            f"Unit '{pl.get('unit')}' differs from the rule's '{p['unit']}'; no unit conversion is attempted."
        )
        return r
    v = pl.get("value")
    if isinstance(v, bool) or not isinstance(v, int | float):
        r.no_data += 1
        return r
    r.evaluated = 1
    if not _cmp(p["operator"], float(v), p["threshold"]):
        return r
    ent = (ev.entities or [{}])[0] if ev.entities else {}
    et, eid = ent.get("entity_type", "source"), str(ent.get("entity_id", ev.source))
    cv = p.get("critical_value")
    r.matches.append(
        Match(
            et,
            eid,
            f"{et} {eid}",
            p["metric"],
            float(v),
            p["unit"],
            p["operator"],
            p["threshold"],
            pl.get("basis") if pl.get("basis") in ("observed", "predicted") else "observed",
            f"{p['metric']} {OP_TEXT[p['operator']]} {p['threshold']:g} {p['unit']} on {et} {eid}",
            f"Reported value {v:g} {p['unit']} {OP_TEXT[p['operator']]} threshold {p['threshold']:g}. Source: {ev.source}.",
            {
                "event_uid": ev.event_uid,
                "source": ev.source,
                "source_record_id": ev.source_record_id,
                "provenance": ev.provenance,
                "occurred_at": _aware(ev.occurred_at).isoformat(),
            },
            None,
            _aware(ev.occurred_at),
            event_id=ev.id,
            critical=cv is not None and _cmp(p["operator"], float(v), cv),
        )  # fmt: skip
    )
    return r


def reported_disruption(ev: AlertEvent, p: dict[str, Any]) -> Result:
    r = Result()
    if ev.event_type != "disruption.reported":
        return r
    if ev.provenance == "SYNTHETIC_DEMO" and not p.get("allow_synthetic"):
        r.skipped += 1
        r.notes.append("Synthetic demo events are ignored by this rule.")
        return r
    ents = ev.entities or []
    r.evaluated = 1
    if len(ents) < p["min_affected"]:
        if not ents:
            r.no_data += 1
        return r
    first = ents[0]
    title = str((ev.payload or {}).get("title", "Reported disruption"))[:120]
    r.matches.append(
        Match(
            first["entity_type"],
            str(first["entity_id"]),
            f"{first['entity_type']} {first['entity_id']}",
            "affected_entities",
            float(len(ents)),
            "entities",
            "gte",
            float(p["min_affected"]),
            "observed",
            f"Reported disruption: {title}",
            f"A source reported a disruption affecting {len(ents)} named entit{'y' if len(ents) == 1 else 'ies'}. Reported by {ev.source} ({ev.provenance}); not independently verified by ATLAS.",
            {
                "event_uid": ev.event_uid,
                "source": ev.source,
                "provenance": ev.provenance,
                "occurred_at": _aware(ev.occurred_at).isoformat(),
                "affected": ents[:20],
                "expected_duration_days": (ev.payload or {}).get("expected_duration_days"),
            },
            None,
            _aware(ev.occurred_at),
            event_id=ev.id,
            dims=ev.event_uid,
            extra_source="disruption",
        )  # fmt: skip
    )
    return r


EVENT = {"event_threshold": event_threshold, "reported_disruption": reported_disruption}
