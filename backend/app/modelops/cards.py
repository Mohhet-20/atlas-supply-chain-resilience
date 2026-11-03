"""Model cards generated from persisted records only. Missing information is shown as missing, never invented."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modelops import config as C
from app.modelops import registry
from app.models.modelops import ModelDiagnostic, ModelEvaluation, ModelVersion

NOT_RECORDED = "not recorded"


def _or(v: Any) -> Any:
    return NOT_RECORDED if v in (None, [], {}, "") else v


def build(session: Session, mv: ModelVersion) -> dict[str, Any]:
    evs = list(session.scalars(select(ModelEvaluation).where(ModelEvaluation.model_version_id == mv.id, ModelEvaluation.status.in_(registry.DONE)).order_by(ModelEvaluation.id.desc()).limit(10)))  # fmt: skip
    best = {k: next((e for e in evs if e.kind == k and e.result_status == "completed"), None) for k in C.KINDS}
    diags = list(
        session.scalars(
            select(ModelDiagnostic)
            .where(ModelDiagnostic.model_version_id == mv.id)
            .order_by(ModelDiagnostic.id.desc())
            .limit(40)
        )
    )
    open_drift = [d for d in diags if d.status in ("warning", "alert")]
    hist = registry.history(session, mv)

    def ev_summary(e: ModelEvaluation | None) -> dict[str, Any] | str:
        if e is None:
            return NOT_RECORDED
        return {"evaluation": e.eval_uid, "kind": e.kind, "finished_at": e.finished_at.isoformat() if e.finished_at else None, "validity": (e.validity or {}).get("overall"), "metrics": e.metrics, "baselines": e.baselines, "sample": e.sample, "warnings": e.warnings, "limitations": e.limitations}  # fmt: skip

    unevaluated = [k for k in C.KINDS if best[k] is None]
    return {
        "generated_from": "persisted records only", "statement": C.STATEMENTS["card"],
        "identity": {"model_version": mv.model_uid, "model_key": mv.model_key, "version": mv.version, "family": mv.family, "task": mv.task.value, "domain": mv.domain, "algorithm": mv.algorithm, "created_by": mv.created_by, "created_at": mv.created_at.isoformat()},  # fmt: skip
        "intended_use": _or(mv.intended_use), "prohibited_use": _or(mv.prohibited_use), "target": mv.target,
        "training": {"data_snapshot_hash": _or(mv.data_snapshot_hash), "training_window": _or(mv.training_window), "evaluation_window": _or(mv.evaluation_window), "feature_schema": _or(mv.feature_schema), "preprocessing_version": _or(mv.preprocessing_version), "hyperparameters": _or(mv.hyperparameters), "seed": _or(mv.seed)},  # fmt: skip
        "reproducibility": {"artifact_ref": mv.artifact_ref, "artifact_checksum": _or(mv.artifact_checksum), "code_revision": mv.code_revision, "dependency_versions": mv.dependency_versions},  # fmt: skip
        "evaluation": {"recorded_backtest": ev_summary(best["RECORDED_BACKTEST"]), "prospective": ev_summary(best["PROSPECTIVE"]), "missing": unevaluated},
        "limitations": _or(mv.limitations), "known_issues": mv.known_issues or [], "fallback_behavior": _or(mv.fallback_behavior), "operating_conditions": _or(mv.operating_conditions),
        "monitoring": {"requirements": _or(mv.monitoring_requirements), "open_signals": [{"kind": d.kind, "feature": d.feature, "status": d.status, "explanation": d.explanation} for d in open_drift[:10]], "note": C.STATEMENTS["drift"]},  # fmt: skip
        "governance": {"state": mv.lifecycle_state.value, "state_changed_at": mv.state_changed_at.isoformat(), "history": [{"from": h.from_state, "to": h.to_state, "actor": h.actor, "note": h.note, "evidence": h.evidence, "at": h.created_at.isoformat()} for h in hist]},  # fmt: skip
        "statements": {"score": C.STATEMENTS["score"], "attribution": C.STATEMENTS["attribution"], "tenancy": C.STATEMENTS["tenancy"]},
    }  # fmt: skip
