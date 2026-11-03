"""Model-type-aware explanations, traceable to a model version. Unsupported requests return a structured ``unsupported`` result.

Global: stored coefficients and permutation importance (learned models), the transparent weights of the rule-based indicator, or the
stored fitted parameters of a forecasting model. Local: the factor breakdown that was stored with the issued assessment. Nothing is
recomputed with a different method, and no SHAP, LIME or counterfactual output is fabricated.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, NotFoundError
from app.modelops import config as C
from app.models.delivery import DeliveryAssessment, LeadTimeRun
from app.models.forecast import ForecastRun
from app.models.modelops import ModelExplanation, ModelVersion
from app.models.risk import RiskAssessment, RiskFactor
from app.risk.methodology import RiskConfig

METHOD_VERSION = "ex-1"
SUPPORTED = ("stored_global", "stored_local")
UNSUPPORTED = {
    "shap": "SHAP values are not computed: no background dataset or Shapley estimator is part of ATLAS, and the stored models are small linear or rule-based ones whose coefficients and contributions are already exact.",
    "lime": "LIME is not available: local surrogate sampling is not implemented and would add approximation without more truth than the stored contributions.",
    "counterfactual": "Counterfactual explanations are not available: they would imply actionable causal changes that the data cannot support.",
    "causal": "ATLAS does not estimate causal effects. Attributions describe the fitted model, not the real world.",
}
ATTR = C.STATEMENTS["attribution"]


def _key(mv: ModelVersion, scope: str, subject: str | None, method: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [mv.model_uid, mv.artifact_checksum, scope, subject, method, METHOD_VERSION], sort_keys=True
        ).encode()
    ).hexdigest()


def unsupported(mv: ModelVersion, method: str) -> dict[str, Any]:
    return {"status": "unsupported", "method": method, "reason": UNSUPPORTED.get(method, f"The method {method!r} is not available."), "supported_methods": list(SUPPORTED), "model_version": mv.model_uid, "statement": ATTR}  # fmt: skip


def _cached(
    session: Session, mv: ModelVersion, scope: str, subject: str | None, method: str, actor: str, build
) -> tuple[dict[str, Any], bool]:  # noqa: ANN001
    ck = _key(mv, scope, subject, method)
    hit = session.scalar(
        select(ModelExplanation).where(ModelExplanation.model_version_id == mv.id, ModelExplanation.cache_key == ck)
    )
    if hit is not None:
        return {**hit.result, "cached": True}, True
    res = build()
    res.update(
        {
            "model_version": mv.model_uid,
            "model_key": mv.model_key,
            "version": mv.version,
            "method_version": METHOD_VERSION,
            "statement": ATTR,
            "cached": False,
        }
    )
    session.add(ModelExplanation(model_version_id=mv.id, scope=scope, subject_ref=subject, method=method, method_version=METHOD_VERSION, status=res["status"], cache_key=ck, result=res, created_by=actor))  # fmt: skip
    session.flush()
    return res, False


def global_explanation(session: Session, mv: ModelVersion, method: str, actor: str) -> dict[str, Any]:
    if method in UNSUPPORTED:
        return unsupported(mv, method)
    if method != "stored_global":
        raise DomainValidationError("method must be 'stored_global', or one of: " + ", ".join(sorted(UNSUPPORTED)))
    return _cached(session, mv, "GLOBAL", None, method, actor, lambda: _build_global(session, mv))[0]


def _build_global(session: Session, mv: ModelVersion) -> dict[str, Any]:
    if mv.source_type == "RISK_METHODOLOGY":
        cfg = RiskConfig()
        w = sorted(cfg.weights.items(), key=lambda kv: -kv[1])
        return {"status": "ok", "kind": "transparent_weights", "method": "the documented methodology weights; the score is a weighted mean of factor scores over the available evidence", "weights": [{"factor": k, "weight": v} for k, v in w], "scope": "global methodology (not learned from data)", "limitations": ["Weights are analytical choices by the model's authors, not estimated from outcomes."]}  # fmt: skip
    if mv.source_type == "LEADTIME_RUN":
        r = session.get(LeadTimeRun, mv.source_id)
        ex = r.explanation or {}
        if "coefficients" not in ex:
            return {"status": "ok", "kind": "transparent_lookup", "method": ex.get("method") or "history lookup", "importance": [], "limitations": ["The selected model is a transparent lookup, so there are no feature coefficients to report."]}  # fmt: skip
        return {
            "status": "ok", "kind": "learned_linear", "method": ex.get("method"), "coefficients": ex.get("coefficients"), "permutation_importance": ex.get("permutation_importance"),
            "scope": "validation split of the training run", "limitations": ["Correlated inputs share credit; importance is not additive across them.", "Computed once at training time on the validation split."],
        }  # fmt: skip
    r = session.get(ForecastRun, mv.source_id)
    p = r.model_params or {}
    shown = {
        k: v for k, v in p.items() if isinstance(v, (int, float, str, bool)) or (isinstance(v, list) and len(v) <= 60)
    }
    return {"status": "ok", "kind": "fitted_parameters", "method": f"stored fitted parameters of {r.selected_model}", "parameters": shown, "scope": "this series", "limitations": ["Forecasting models are fitted to one series, so there are no cross-feature importances.", "Parameters describe the fit; they do not explain why demand moved."]}  # fmt: skip


def local_explanation(session: Session, mv: ModelVersion, subject: str, method: str, actor: str) -> dict[str, Any]:
    if method in UNSUPPORTED:
        return unsupported(mv, method)
    if method != "stored_local":
        raise DomainValidationError("method must be 'stored_local', or one of: " + ", ".join(sorted(UNSUPPORTED)))
    if mv.source_type == "FORECAST_RUN":
        return {"status": "unsupported", "method": method, "reason": "A forecast value is not decomposed per prediction by the stored model, so no local attribution exists.", "supported_methods": ["stored_global"], "model_version": mv.model_uid, "statement": ATTR}  # fmt: skip
    if not (isinstance(subject, str) and 3 <= len(subject) <= 40 and all(c.isalnum() or c in "-_" for c in subject)):
        raise DomainValidationError("subject must be an assessment id such as 'DA-…' or 'RA-…'.")
    return _cached(session, mv, "LOCAL", subject, method, actor, lambda: _build_local(session, mv, subject))[0]


def _build_local(session: Session, mv: ModelVersion, subject: str) -> dict[str, Any]:
    if mv.source_type == "RISK_METHODOLOGY":
        a = session.scalar(select(RiskAssessment).where(RiskAssessment.assessment_uid == subject))
        if a is None or a.methodology_version != mv.version:
            raise NotFoundError("No assessment with that id was issued by this model version.")
        facs = list(session.scalars(select(RiskFactor).where(RiskFactor.assessment_id == a.id)))
        return {
            "status": "ok", "kind": "factor_contributions", "subject": subject, "prediction": {"risk_score": a.risk_score, "classification": a.classification.value if a.classification else None, "status": a.status.value, "evidence_coverage": a.evidence_coverage, "reliability": a.reliability},  # fmt: skip
            "contributions": [{"factor": f.category.value, "state": f.state.value, "score": f.score, "weight": f.weight, "explanation": f.explanation, "confidence": f.confidence} for f in facs if f.role == "risk"],
            "missing": a.missing, "note": "These are the exact stored factor scores and weights that produced the number; there is no approximation.",
        }  # fmt: skip
    a = session.scalar(select(DeliveryAssessment).where(DeliveryAssessment.assessment_uid == subject))
    if a is None or mv.source_id not in (a.leadtime_run_id, a.risk_run_id):
        raise NotFoundError("No assessment with that id was issued by this model version.")
    return {
        "status": "ok", "kind": "stored_factors", "subject": subject, "prediction": {"estimate": a.estimate, "risk": a.risk, "as_of": a.as_of.isoformat()},
        "factors": a.factors or [], "warnings": a.warnings or [], "note": "Factors were stored with the assessment when it was issued; they are not recomputed with the current model or data.",
    }  # fmt: skip
