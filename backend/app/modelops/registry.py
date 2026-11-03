"""Unified model registry: one persisted, immutable version per finished domain run (and per rule-based methodology).

Registration only *describes* an existing artefact. It never loads code, never fits a model and never copies a pickle: the
``artifact_ref`` is a validated reference like ``leadtime_run:LT-…`` into the domain run that already stores the fitted parameters.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, InvalidTransitionError, NotFoundError
from app.modelops import config as C
from app.models.delivery import LeadTimeRun
from app.models.enums import SimStatus
from app.models.forecast import ForecastRun
from app.models.modelops import ModelEvaluation, ModelLifecycleEvent, ModelState, ModelTask, ModelVersion
from app.models.types import utcnow
from app.risk.methodology import DEFAULT_HASH, METHODOLOGY_VERSION

REF_RE = re.compile(r"^(forecast_run|leadtime_run|risk_methodology):[A-Za-z0-9._-]{1,64}$")
DONE = (SimStatus.COMPLETED, SimStatus.PARTIAL)
_DEPS = ("fastapi", "sqlalchemy", "pydantic", "alembic")


def validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise DomainValidationError("artifact_ref must look like 'forecast_run:<id>', 'leadtime_run:<id>' or 'risk_methodology:<version>'; paths, URLs and code are never accepted.")  # fmt: skip
    return ref


def checksum(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def deps() -> dict[str, str]:
    out = {"python": sys.version.split()[0]}
    for d in _DEPS:
        try:
            out[d] = importlib.metadata.version(d)
        except importlib.metadata.PackageNotFoundError:
            out[d] = "unknown"
    return out


def code_revision() -> str:
    return (os.environ.get("ATLAS_CODE_REVISION") or "unknown")[:64]


USE = {
    "DEMAND": (
        [
            "Planning-level demand outlook for a product at a facility, with the stored validation evidence",
            "Comparison with the documented baselines",
        ],
        [
            "Automated ordering or inventory commitments",
            "Use outside the product/facility series it was trained on",
            "Treating the point value as certain",
        ],
    ),  # fmt: skip
    "LEAD_TIME": (
        ["Estimating an order's lead time with an empirical interval, for planning and review"],
        [
            "Contractual commitments",
            "Judging a supplier's performance on its own",
            "Use for delivery types not seen in training",
        ],
    ),  # fmt: skip
    "DELIVERY_RISK": (
        ["Ranking deliveries for review by likelihood of being late"],
        [
            "Reading an uncalibrated score as a probability",
            "Automatic expediting or penalties",
            "Decisions about individual people",
        ],
    ),  # fmt: skip
    "SUPPLIER_RISK": (
        ["Transparent, evidence-weighted review aid for supplier risk"],
        ["Reading the score as a probability of failure", "Excluding a supplier without human review"],
    ),  # fmt: skip
}


def _version_row(**kw: Any) -> ModelVersion:
    dom = kw["domain"]
    intended, prohibited = USE[dom]
    kw.setdefault("intended_use", intended)
    kw.setdefault("prohibited_use", prohibited)
    kw.setdefault("known_issues", [])
    kw.setdefault("monitoring_requirements", ["Compare outcomes with predictions as they mature", "Review input drift before relying on old fitted parameters"])  # fmt: skip
    kw["model_uid"] = f"MV-{uuid.uuid4().hex[:16].upper()}"
    kw["dependency_versions"] = deps()
    kw["code_revision"] = code_revision()
    kw["state_changed_at"] = utcnow()
    kw["lifecycle_state"] = ModelState.DEVELOPMENT
    return ModelVersion(**kw)


def _from_forecast(r: ForecastRun, actor: str) -> ModelVersion | None:
    if r.status not in DONE or not r.selected_model:
        return None
    params = r.model_params or {}
    sel = r.selection or {}
    lim = ["Trained on one product/facility series.", "Backtest evidence is from past periods; the future may differ."]
    if sel.get("status") != "selected":
        lim.append(
            "No candidate beat the baselines reliably; the model is a labelled fallback, not a validated winner."
        )
    return _version_row(
        model_key=f"forecast/{r.selected_model}", version=r.run_uid, family="forecasting", task=ModelTask.FORECASTING, domain="DEMAND",
        target=f"demand per {r.frequency} period, horizon {r.horizon}" + (f" ({r.unit})" if r.unit else ""), source_type="FORECAST_RUN", source_id=r.id,
        source_ref=r.run_uid, limitations=lim, operating_conditions={"frequency": r.frequency, "horizon": r.horizon, "product_id": r.product_id, "facility_id": r.facility_id},  # fmt: skip
        feature_schema={"kind": "time series", "inputs": ["lagged and seasonal history of the target series"]}, preprocessing_version=r.algorithm_version, data_snapshot_hash=r.data_snapshot_hash,  # fmt: skip
        training_window=r.training_interval, evaluation_window={"scheme": (r.validation or {}).get("scheme"), "folds": len((r.validation or {}).get("fold_train_lengths") or [])},  # fmt: skip
        algorithm=r.selected_model, hyperparameters=params, seed=(r.reproducibility or {}).get("seed"), artifact_ref=validate_ref(f"forecast_run:{r.run_uid}"),
        artifact_checksum=checksum({"model": r.selected_model, "params": params, "config": r.config_hash}), fallback_behavior=("labelled fallback to a baseline" if r.fallback else None),  # fmt: skip
        created_by=actor,
    )  # fmt: skip


def _from_leadtime(r: LeadTimeRun, actor: str) -> ModelVersion | None:
    if r.status not in DONE or not r.selected_model or not r.artifact:
        return None
    risk = r.target == "LATE_RISK"
    art = r.artifact
    cal = r.calibration or {}
    lim = [
        "Learned from the delivery history in scope only.",
        "Right-censored (still open) orders are excluded from training.",
    ]
    if risk and art.get("score_kind") != "calibrated_probability":
        lim.append("The output is a score, not a calibrated probability.")
    return _version_row(
        model_key=f"leadtime/{r.target.lower()}/{r.selected_model}", version=r.run_uid, family="lead-time" if not risk else "delivery-risk",
        task=ModelTask.CLASSIFICATION if risk else ModelTask.REGRESSION, domain="DELIVERY_RISK" if risk else "LEAD_TIME",
        target=("late versus commitment" if risk else f"lead time ({art.get('unit')})"), source_type="LEADTIME_RUN", source_id=r.id, source_ref=r.run_uid,
        limitations=lim, operating_conditions={"prediction_point": art.get("point"), "scope": (r.config or {}).get("supplier_id")},
        feature_schema={"kind": "tabular", "spec": (art.get("spec") or {}).get("names") if isinstance(art.get("spec"), dict) else None},
        preprocessing_version=r.algorithm_version, data_snapshot_hash=r.data_snapshot_hash, training_window=(r.splits or {}).get("train"),
        evaluation_window={"validation": (r.splits or {}).get("validation"), "test": (r.splits or {}).get("test")}, algorithm=r.selected_model,
        hyperparameters={"ridge_lambda": (r.config or {}).get("ridge_lambda"), "decision": cal.get("decision")}, seed=(r.reproducibility or {}).get("seed"),
        artifact_ref=validate_ref(f"leadtime_run:{r.run_uid}"), artifact_checksum=checksum(art), fallback_behavior=None, created_by=actor,
    )  # fmt: skip


def _risk_method(actor: str) -> ModelVersion:
    return _version_row(
        model_key="risk/rule_based", version=METHODOLOGY_VERSION, family="rule-based", task=ModelTask.RULE_BASED_INDICATOR, domain="SUPPLIER_RISK",
        target="supplier risk indicator (0–100, evidence-weighted)", source_type="RISK_METHODOLOGY", source_id=None, source_ref=METHODOLOGY_VERSION,
        limitations=["Expert-set weights, not learned from labelled outcomes.", "No outcome labels exist in ATLAS, so predictive performance cannot be measured."],
        operating_conditions={"methodology_hash": DEFAULT_HASH}, feature_schema={"kind": "weighted factors"}, preprocessing_version=None, data_snapshot_hash=None,
        training_window=None, evaluation_window=None, algorithm="weighted evidence factors", hyperparameters={"methodology_hash": DEFAULT_HASH}, seed=None,
        artifact_ref=validate_ref(f"risk_methodology:{METHODOLOGY_VERSION}"), artifact_checksum=DEFAULT_HASH, fallback_behavior="insufficient evidence yields no score", created_by=actor,
    )  # fmt: skip


def _log(
    session: Session, mv: ModelVersion, frm: str | None, to: str, actor: str, note: str | None, evidence: Any = None
) -> None:
    session.add(
        ModelLifecycleEvent(
            model_version_id=mv.id, from_state=frm, to_state=to, actor=actor, note=note, evidence=evidence
        )
    )


def sync(session: Session, actor: str = "system", *, limit: int = 500) -> dict[str, Any]:
    """Idempotently register every finished run that is not yet registered. Re-running adds nothing."""
    known = {(s, r) for s, r in session.execute(select(ModelVersion.source_type, ModelVersion.source_ref))}
    added, skipped = [], 0
    cands: list[ModelVersion | None] = []
    for fr in session.scalars(
        select(ForecastRun).where(ForecastRun.status.in_(DONE)).order_by(ForecastRun.id).limit(limit)
    ):
        if ("FORECAST_RUN", fr.run_uid) not in known:
            cands.append(_from_forecast(fr, actor))
            skipped += cands[-1] is None
    for lr in session.scalars(
        select(LeadTimeRun).where(LeadTimeRun.status.in_(DONE)).order_by(LeadTimeRun.id).limit(limit)
    ):
        if ("LEADTIME_RUN", lr.run_uid) not in known:
            cands.append(_from_leadtime(lr, actor))
            skipped += cands[-1] is None
    if ("RISK_METHODOLOGY", METHODOLOGY_VERSION) not in known:
        cands.append(_risk_method(actor))
    for mv in cands:
        if mv is None:
            continue
        session.add(mv)
        session.flush()
        _log(
            session,
            mv,
            None,
            "DEVELOPMENT",
            actor,
            "registered from a finished run" if mv.source_type != "RISK_METHODOLOGY" else "registered methodology",
        )
        added.append(mv.model_uid)
    return {"registered": len(added), "already_registered": len(known), "not_registrable": skipped, "model_uids": added}


def get(session: Session, uid: str) -> ModelVersion:
    mv = session.scalar(select(ModelVersion).where(ModelVersion.model_uid == uid))
    if mv is None:
        raise NotFoundError("Model version not found.")
    return mv


def latest_valid_evaluation(session: Session, mv: ModelVersion) -> ModelEvaluation | None:
    return session.scalar(
        select(ModelEvaluation)
        .where(
            ModelEvaluation.model_version_id == mv.id,
            ModelEvaluation.status.in_(DONE),
            ModelEvaluation.result_status == "completed",
        )
        .order_by(ModelEvaluation.id.desc())
        .limit(1)  # fmt: skip
    )


def transition(session: Session, mv: ModelVersion, to: str, actor: str, role: str, note: str | None) -> ModelVersion:
    if to not in C.STATES:
        raise DomainValidationError(f"Unknown state {to!r}.")
    frm = mv.lifecycle_state.value
    need = C.TRANSITIONS.get((frm, to))
    if need is None:
        raise InvalidTransitionError(f"A model cannot move from {frm} to {to}.")
    order = {"viewer": 0, "editor": 1, "reviewer": 2, "admin": 3, "system": 9}
    if order.get(role, 0) < order[need] and need != "system":
        raise InvalidTransitionError(f"Moving {frm} → {to} needs the {need} role.")
    evidence: dict[str, Any] | None = None
    if to == "APPROVED":
        ev = latest_valid_evaluation(session, mv)
        if ev is None:
            raise InvalidTransitionError(
                "Approval needs a completed, valid evaluation; none exists. A metric alone is not grounds for approval."
            )
        if (ev.validity or {}).get("overall") == "invalid":
            raise InvalidTransitionError("The latest evaluation failed validity checks; approval is not possible.")
        if not note or len(note.strip()) < 10:
            raise DomainValidationError("Approval needs a written rationale (at least 10 characters).")
        evidence = {"evaluation": ev.eval_uid, "kind": ev.kind, "validity": (ev.validity or {}).get("overall")}
    if to == "EVALUATED" and latest_valid_evaluation(session, mv) is None:
        raise InvalidTransitionError("A model is EVALUATED only after a completed evaluation exists.")
    mv.lifecycle_state, mv.state_changed_at = ModelState(to), utcnow()
    _log(session, mv, frm, to, actor, (note or None) and note.strip()[:500], evidence)
    return mv


def mark_evaluated(session: Session, mv: ModelVersion, actor: str) -> None:
    """Called after a completed evaluation: DEVELOPMENT → EVALUATED (never touches APPROVED or later states)."""
    if mv.lifecycle_state is ModelState.DEVELOPMENT:
        mv.lifecycle_state, mv.state_changed_at = ModelState.EVALUATED, utcnow()
        _log(session, mv, "DEVELOPMENT", "EVALUATED", actor, "evaluation completed")


def history(session: Session, mv: ModelVersion) -> list[ModelLifecycleEvent]:
    return list(
        session.scalars(
            select(ModelLifecycleEvent)
            .where(ModelLifecycleEvent.model_version_id == mv.id)
            .order_by(ModelLifecycleEvent.id)
        )
    )


def count_active_evaluations(session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(ModelEvaluation)
            .where(ModelEvaluation.status.in_((SimStatus.QUEUED, SimStatus.RUNNING)))
        )
        or 0
    )
