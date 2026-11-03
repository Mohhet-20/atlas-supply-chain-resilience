"""Monitoring foundations: data-quality, drift and (only with labelled outcomes) performance diagnostics for a model version.

Drift diagnostics compare a *reference* period with a *comparison* period of the same stored inputs. They never trigger retraining,
approval changes or deprecation: they are persisted as observations with a plain-language explanation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modelops import config as C
from app.modelops import drift as D
from app.models.delivery import DeliveryAssessment, DeliverySnapshot, LeadTimeRun
from app.models.forecast import ForecastRun
from app.models.modelops import ModelDiagnostic, ModelEvaluation, ModelVersion
from app.models.types import utcnow


def _row(
    uid: str, mv: ModelVersion, kind: str, feature: str | None, d: dict[str, Any], expl: str, actor: str
) -> ModelDiagnostic:
    st = d.get("status", "ok")
    return ModelDiagnostic(
        check_uid=uid, model_version_id=mv.id, kind=kind, feature=feature, status=st, severity=d.get("severity", "info"),
        statistic={k: v for k, v in d.items() if k not in ("status", "severity", "note")}, explanation=(d.get("note") or expl)[:600], created_by=actor,
    )  # fmt: skip


def _split_time(items: list[Any], key, split_at: datetime | None) -> tuple[list[Any], list[Any], datetime | None]:  # noqa: ANN001
    items = sorted(items, key=key)
    if not items:
        return [], [], split_at
    if split_at is None:
        split_at = key(items[len(items) // 2])
    return [x for x in items if key(x) < split_at], [x for x in items if key(x) >= split_at], split_at


def run_check(
    session: Session, mv: ModelVersion, actor: str, *, split_at: datetime | None = None, max_rows: int = 5000
) -> dict[str, Any]:
    uid = f"MC-{uuid.uuid4().hex[:16].upper()}"
    diag: list[ModelDiagnostic] = []
    ctx: dict[str, Any] = {
        "check_uid": uid,
        "model_version": mv.model_uid,
        "statement": C.STATEMENTS["drift"],
        "checked_at": utcnow().isoformat(),
    }
    if mv.source_type == "RISK_METHODOLOGY":
        diag.append(_row(uid, mv, "DATA_QUALITY", None, {"status": "not_applicable", "severity": "info", "note": "Input drift for the rule-based indicator is not monitored here; its evidence coverage and freshness are reported with every assessment."}, "", actor))  # fmt: skip
    elif mv.source_type == "FORECAST_RUN":
        _forecast(session, mv, uid, actor, diag, split_at)
    else:
        _leadtime(session, mv, uid, actor, diag, split_at, max_rows)
    _performance(session, mv, uid, actor, diag)
    for d in diag:
        session.add(d)
    session.flush()
    worst = (
        "alert"
        if any(d.status == "alert" for d in diag)
        else "warning"
        if any(d.status == "warning" for d in diag)
        else "ok"
    )
    return {**ctx, "overall": worst, "diagnostics": [serialize(d) for d in diag], "split_at": ctx.get("split_at")}


def serialize(d: ModelDiagnostic) -> dict[str, Any]:
    return {"id": d.id, "check_uid": d.check_uid, "kind": d.kind, "feature": d.feature, "status": d.status, "severity": d.severity, "statistic": d.statistic, "explanation": d.explanation, "created_at": d.created_at.isoformat()}  # fmt: skip


def _forecast(
    session: Session, mv: ModelVersion, uid: str, actor: str, diag: list[ModelDiagnostic], split_at: datetime | None
) -> None:
    from app.models.inventory import DemandObservation

    r = session.get(ForecastRun, mv.source_id)
    rows = list(session.scalars(select(DemandObservation).where(DemandObservation.product_id == r.product_id, DemandObservation.facility_id == r.facility_id).limit(5000)))  # fmt: skip
    if not rows:
        diag.append(_row(uid, mv, "DATA_QUALITY", None, {"status": "insufficient_data", "severity": "info", "note": "No demand observations are available for this series."}, "", actor))  # fmt: skip
        return
    key = lambda o: datetime.combine(o.period_start, datetime.min.time())  # noqa: E731
    at0 = split_at or r.finished_at
    ref, cur, at = _split_time(rows, key, at0.replace(tzinfo=None) if at0 is not None else None)
    q = lambda o: o.quantity  # noqa: E731
    d = D.psi_numeric([q(o) for o in ref], [q(o) for o in cur])
    diag.append(
        _row(uid, mv, "TARGET_DRIFT", "demand quantity", d, "Compared demand before and after the split point.", actor)
    )
    diag.append(
        _row(
            uid,
            mv,
            "COVARIATE_DRIFT",
            "demand quantity (KS)",
            D.ks_statistic([q(o) for o in ref], [q(o) for o in cur]),
            "Two-sample distribution distance.",
            actor,
        )
    )


def _leadtime(
    session: Session,
    mv: ModelVersion,
    uid: str,
    actor: str,
    diag: list[ModelDiagnostic],
    split_at: datetime | None,
    max_rows: int,
) -> None:
    r = session.get(LeadTimeRun, mv.source_id)
    snaps = list(session.scalars(select(DeliverySnapshot).order_by(DeliverySnapshot.observed_at).limit(max_rows)))
    ref, cur, at = _split_time(snaps, lambda s: s.observed_at, split_at or r.as_of)
    for field, num in (("transport_mode", False), ("quantity", True)):
        a = [getattr(s, field) for s in ref]
        b = [getattr(s, field) for s in cur]
        diag.append(
            _row(
                uid,
                mv,
                "DATA_QUALITY",
                field,
                D.missingness_change(sum(x is None for x in a), len(a), sum(x is None for x in b), len(b)),
                f"Share of missing {field} values.",
                actor,
            )
        )
        if num:
            diag.append(
                _row(
                    uid,
                    mv,
                    "COVARIATE_DRIFT",
                    field,
                    D.psi_numeric([x for x in a if x is not None], [x for x in b if x is not None]),
                    f"{field} distribution before vs after the split.",
                    actor,
                )
            )
        else:
            diag.append(
                _row(
                    uid,
                    mv,
                    "COVARIATE_DRIFT",
                    field,
                    D.psi_categorical(a, b),
                    f"{field} categories before vs after the split.",
                    actor,
                )
            )
    sup_a, sup_b = [s.supplier_id for s in ref], [s.supplier_id for s in cur]
    diag.append(
        _row(
            uid,
            mv,
            "COVARIATE_DRIFT",
            "supplier",
            D.psi_categorical(sup_a, sup_b),
            "Supplier mix before vs after the split (unseen suppliers raise a warning).",
            actor,
        )
    )
    sc = [a for a in session.scalars(select(DeliveryAssessment).where(DeliveryAssessment.leadtime_run_id == r.id, DeliveryAssessment.status == "ASSESSED").limit(max_rows))]  # fmt: skip
    vals = [float((a.estimate or {}).get("value")) for a in sc if (a.estimate or {}).get("value") is not None]
    if vals:
        h = len(vals) // 2
        diag.append(
            _row(
                uid,
                mv,
                "PREDICTION_DRIFT",
                "estimate",
                D.psi_numeric(vals[:h], vals[h:]),
                "Distribution of issued estimates, first half vs second half.",
                actor,
            )
        )
    else:
        diag.append(_row(uid, mv, "PREDICTION_DRIFT", "estimate", {"status": "insufficient_data", "severity": "info", "note": "No issued estimates for this model version."}, "", actor))  # fmt: skip


def _performance(session: Session, mv: ModelVersion, uid: str, actor: str, diag: list[ModelDiagnostic]) -> None:
    """Performance is judged only from a completed PROSPECTIVE evaluation with enough matured outcomes. Drift alone never says 'deteriorated'."""
    ev = session.scalar(select(ModelEvaluation).where(ModelEvaluation.model_version_id == mv.id, ModelEvaluation.kind == "PROSPECTIVE", ModelEvaluation.result_status == "completed").order_by(ModelEvaluation.id.desc()).limit(1))  # fmt: skip
    rec = session.scalar(select(ModelEvaluation).where(ModelEvaluation.model_version_id == mv.id, ModelEvaluation.kind == "RECORDED_BACKTEST", ModelEvaluation.result_status == "completed").order_by(ModelEvaluation.id.desc()).limit(1))  # fmt: skip
    if ev is None:
        diag.append(_row(uid, mv, "PERFORMANCE", None, {"status": "insufficient_data", "severity": "info", "note": "Performance over time cannot be assessed: no prospective evaluation with outcomes exists. Drift signals above are not evidence of deterioration."}, "", actor))  # fmt: skip
        return
    n = (ev.sample or {}).get("with_outcome") or (ev.sample or {}).get("final_periods") or 0
    metric = "mae" if "mae" in (ev.metrics or {}) else "pr_auc"
    cur = ((ev.metrics or {}).get(metric) or {}).get("value")
    ref = (((rec.metrics or {}).get(metric) or {}).get("value")) if rec else None
    if n < 30 or cur is None or ref is None:
        diag.append(_row(uid, mv, "PERFORMANCE", metric, {"status": "insufficient_data", "severity": "info", "value": cur, "reference_value": ref, "n": n, "note": f"Only {n} matured outcome(s) (at least 30 are needed) or the metric is undefined; no conclusion about deterioration is drawn."}, "", actor))  # fmt: skip
        return
    worse = (cur > ref * 1.25) if metric == "mae" else (cur < ref * 0.8)
    diag.append(_row(uid, mv, "PERFORMANCE", metric, {"status": "warning" if worse else "ok", "severity": "warning" if worse else "info", "value": cur, "reference_value": ref, "n": n, "note": ("Prospective " + metric + " is materially worse than the training-time evidence (25% rule of thumb); review the model. This is labelled-outcome evidence, not a drift signal.") if worse else "Prospective performance is within the range of the training-time evidence."}, "", actor))  # fmt: skip
