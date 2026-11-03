"""Model Evaluation, Explainability & Governance API.

Registers finished domain runs as immutable model versions, evaluates them with one metric library, explains them from stored
evidence only, monitors drift as observation and records lifecycle decisions. It never trains, replaces, deploys or retrains a
model. Data is limited to the caller's organisation and identity is the authenticated actor only; bodies reject unknown fields
(including any organisation id). Within the organisation, model-governance data is visible at or above the endpoint role.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.modelops import cards, evaluate, explain, monitor, registry
from app.modelops import config as C
from app.models.enums import SimStatus
from app.models.modelops import ModelDiagnostic, ModelEvaluation, ModelExplanation, ModelState, ModelTask, ModelVersion
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/modelops", tags=["model evaluation and governance"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
UidPath = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
IdemKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
]


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_state: Literal["DEVELOPMENT", "EVALUATED", "APPROVED", "DEPRECATED", "ARCHIVED"]
    note: str | None = Field(default=None, max_length=500)


class ExplainBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["GLOBAL", "LOCAL"] = "GLOBAL"
    method: str = Field(default="stored_global", min_length=3, max_length=40, pattern=r"^[a-z_]+$")
    subject: str | None = Field(default=None, max_length=40)


class MonitorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Model-governance write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _limits(request: Request) -> C.Limits:
    s = request.app.state.settings
    return C.Limits(
        max_active_evaluations=s.modelops_max_active_evaluations, time_budget_seconds=s.modelops_time_budget_seconds
    )


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _mv_get(db, uid: str) -> ModelVersion:  # noqa: ANN001
    return registry.get(db, uid)


def _ev_get(db, uid: str) -> ModelEvaluation:  # noqa: ANN001
    ev = db.scalar(select(ModelEvaluation).where(ModelEvaluation.eval_uid == uid))
    if ev is None:
        raise NotFoundError("Evaluation not found.")
    return ev


def _mv(m: ModelVersion, full: bool = False) -> dict[str, Any]:
    out = {
        "model_uid": m.model_uid, "model_key": m.model_key, "version": m.version, "family": m.family, "task": m.task.value, "domain": m.domain, "target": m.target,
        "lifecycle_state": m.lifecycle_state.value, "state_changed_at": _iso(m.state_changed_at), "source_type": m.source_type, "source_ref": m.source_ref,
        "algorithm": m.algorithm, "artifact_ref": m.artifact_ref, "created_by": m.created_by, "created_at": _iso(m.created_at),
    }  # fmt: skip
    if full:
        out.update({
            "intended_use": m.intended_use, "prohibited_use": m.prohibited_use, "limitations": m.limitations, "operating_conditions": m.operating_conditions,
            "feature_schema": m.feature_schema, "preprocessing_version": m.preprocessing_version, "data_snapshot_hash": m.data_snapshot_hash,
            "training_window": m.training_window, "evaluation_window": m.evaluation_window, "hyperparameters": m.hyperparameters, "seed": m.seed,
            "artifact_checksum": m.artifact_checksum, "code_revision": m.code_revision, "dependency_versions": m.dependency_versions,
            "fallback_behavior": m.fallback_behavior, "known_issues": m.known_issues, "monitoring_requirements": m.monitoring_requirements,
            "statement": C.STATEMENTS["registry"],
        })  # fmt: skip
    return out


def _ev(e: ModelEvaluation, full: bool = False) -> dict[str, Any]:
    out = {
        "eval_uid": e.eval_uid, "kind": e.kind, "status": e.status.value, "result_status": e.result_status, "requested_by": e.requested_by,
        "algorithm_version": e.algorithm_version, "config": e.config, "config_hash": e.config_hash, "queued_at": _iso(e.queued_at), "started_at": _iso(e.started_at),
        "finished_at": _iso(e.finished_at), "duration_ms": e.duration_ms, "error": e.error, "validity_overall": (e.validity or {}).get("overall"),
    }  # fmt: skip
    if full:
        out.update({k: getattr(e, k) for k in ("dataset_ref", "split", "sample", "metrics", "baselines", "folds", "segments", "calibration", "intervals", "validity", "warnings", "limitations", "reproducibility", "software")})  # fmt: skip
        out["statements"] = [
            C.STATEMENTS["recorded" if e.kind == "RECORDED_BACKTEST" else "prospective"],
            C.STATEMENTS["tenancy"],
        ]
    return out


def _model_id(db, uid: str) -> int:  # noqa: ANN001
    return _mv_get(db, uid).id


# ------------------------------------------------------------------------------------------------ meta
@router.get("/health", summary="Capabilities, limits and what is not supported")
def health(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    lim = _limits(request)
    states = dict(
        db.execute(select(ModelVersion.lifecycle_state, func.count()).group_by(ModelVersion.lifecycle_state)).all()
    )
    return {
        "algorithm_version": C.ALGORITHM_VERSION, "write_api_enabled": request.app.state.settings.write_api_allowed,
        "registered_versions": sum(states.values()), "by_state": {k.value: v for k, v in states.items()},
        "evaluation_kinds": list(C.KINDS), "lifecycle_states": list(C.STATES), "segment_fields": list(C.SEGMENT_FIELDS),
        "limits": {"max_active_evaluations": lim.max_active_evaluations, "time_budget_seconds": lim.time_budget_seconds},
        "explanation_methods": {"supported": list(explain.SUPPORTED), "unsupported": {k: v for k, v in explain.UNSUPPORTED.items()}},
        "not_supported": ["automatic retraining", "automatic approval", "online serving", "SHAP, LIME and counterfactual explanations", "predictive evaluation of the rule-based supplier risk indicator (no outcome labels)"],  # fmt: skip
        "metric_definitions": __import__("app.modelops.metrics", fromlist=["DEFINITIONS"]).DEFINITIONS, "statements": C.STATEMENTS,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ registry
@router.post("/models/sync", responses=ERR, summary="Register finished runs that are not registered yet (idempotent)")
def sync(db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        out = registry.sync(db, actor.name)
        if out["registered"]:
            audit.record(
                db,
                actor,
                "modelops.sync",
                target_type="MODEL_REGISTRY",
                details={"registered": out["registered"]},
                request_id=request_id_of(request),
            )
    return out


@router.get("/models", summary="Registered model versions (newest first)")
def list_models(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, domain: Annotated[str | None, Query(max_length=24)] = None,
    task: ModelTask | None = None, state: ModelState | None = None, q: Annotated[str | None, Query(max_length=80)] = None,
) -> dict[str, Any]:  # fmt: skip
    query = select(ModelVersion)
    for col, v in ((ModelVersion.domain, domain), (ModelVersion.task, task), (ModelVersion.lifecycle_state, state)):
        if v is not None:
            query = query.where(col == v)
    if q:
        query = query.where(ModelVersion.model_key.contains(q, autoescape=True))
    return _page(db, query.order_by(ModelVersion.id.desc()), limit, offset, _mv)


@router.get("/models/{uid}", responses=ERR, summary="One model version with its documented metadata")
def get_model(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _mv(_mv_get(db, uid), full=True)


@router.get("/models/{uid}/versions", responses=ERR, summary="Version history of the same model key")
def versions(uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    m = _mv_get(db, uid)
    return _page(
        db,
        select(ModelVersion).where(ModelVersion.model_key == m.model_key).order_by(ModelVersion.id.desc()),
        limit,
        offset,
        _mv,
    )


@router.get("/models/{uid}/lifecycle", responses=ERR, summary="Lifecycle and approval history (insert-only)")
def lifecycle(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    m = _mv_get(db, uid)
    return {"model_uid": m.model_uid, "state": m.lifecycle_state.value, "allowed_transitions": [{"to": b, "minimum_role": r} for (a, b), r in C.TRANSITIONS.items() if a == m.lifecycle_state.value], "items": [{"from_state": h.from_state, "to_state": h.to_state, "actor": h.actor, "note": h.note, "evidence": h.evidence, "created_at": _iso(h.created_at)} for h in registry.history(db, m)], "statement": C.STATEMENTS["registry"]}  # fmt: skip


@router.post(
    "/models/{uid}/transition",
    responses=ERR,
    summary="Move a model version through its lifecycle (needs evidence for approval)",
)
def transition(uid: UidPath, body: TransitionBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    role = actor.role.name.lower()
    with db.begin():
        m = _mv_get(db, uid)
        frm = m.lifecycle_state.value
        registry.transition(db, m, body.to_state, actor.name, role, body.note)
        audit.record(
            db,
            actor,
            "modelops.transition",
            target_type="MODEL_VERSION",
            target_id=m.id,
            details={"from": frm, "to": body.to_state},
            request_id=request_id_of(request),
        )
        return _mv(m)


@router.get("/compare", responses=ERR, summary="Side-by-side comparison with explicit incompatibility warnings")
def compare(
    db: DbSession, _a: Viewer, ids: Annotated[str, Query(min_length=4, max_length=200, pattern=r"^[A-Za-z0-9,-]+$")]
) -> dict[str, Any]:
    uids = list(dict.fromkeys(ids.split(",")))
    if not 2 <= len(uids) <= 4:
        raise DomainValidationError("Compare 2 to 4 model versions.")
    ms = [_mv_get(db, u) for u in uids]
    rows, evs = [], []
    for m in ms:
        e = registry.latest_valid_evaluation(db, m)
        evs.append(e)
        rows.append({**_mv(m), "evaluation": None if e is None else {"eval_uid": e.eval_uid, "kind": e.kind, "metrics": e.metrics, "sample": e.sample, "validity": (e.validity or {}).get("overall"), "dataset_ref": e.dataset_ref}})  # fmt: skip
    warns: list[str] = []
    for name, vals in (
        ("task", {m.task.value for m in ms}),
        ("domain", {m.domain for m in ms}),
        ("target", {m.target for m in ms}),
    ):
        if len(vals) > 1:
            warns.append(
                f"The models differ in {name} ({', '.join(sorted(vals))}); their metrics measure different things and must not be ranked against each other."
            )
    if any(e is None for e in evs):
        warns.append("At least one model has no completed evaluation; it is shown without metrics rather than as zero.")
    kinds = {e.kind for e in evs if e is not None}
    if len(kinds) > 1:
        warns.append(
            "The evaluations are of different kinds (recorded backtest vs prospective); they use different data and are not comparable."
        )
    snaps = {m.data_snapshot_hash for m in ms}
    if len(snaps) > 1:
        warns.append(
            "The models were trained and evaluated on different data snapshots, so differences may reflect the data rather than the model."
        )
    ok = not warns
    return {"items": rows, "comparable": ok, "warnings": warns, "note": "Comparability is judged on task, domain, target, evaluation kind and data snapshot; even when comparable, a gap within the stated sample size is not evidence of a better model."}  # fmt: skip


# ------------------------------------------------------------------------------------------------ evaluations
@router.post(
    "/models/{uid}/evaluations", status_code=202, responses=ERR, summary="Queue an evaluation (idempotent with a key)"
)
def create_evaluation(uid: UidPath, body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    try:
        cfg = C.EvalConfig.model_validate(body)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e
    lim = _limits(request)
    with db.begin():
        m = _mv_get(db, uid)
        ev, created = evaluate.create(db, m, cfg, actor.name, lim, idempotency_key=idempotency_key)
        if created:
            audit.record(db, actor, "modelops.evaluation.queued", target_type="MODEL_EVALUATION", target_id=ev.id, details={"model": m.model_uid, "kind": cfg.kind, "config_hash": ev.config_hash}, request_id=request_id_of(request))  # fmt: skip
        out, eid = _ev(ev), ev.id
    if created:
        tasks.add_task(evaluate.execute, request_session_factory(request), eid, lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/models/{uid}/evaluations", responses=ERR, summary="Evaluations of one model version (newest first)")
def list_evaluations(uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, kind: Literal["RECORDED_BACKTEST", "PROSPECTIVE"] | None = None) -> dict[str, Any]:  # fmt: skip
    mid = _model_id(db, uid)
    q = select(ModelEvaluation).where(ModelEvaluation.model_version_id == mid)
    if kind:
        q = q.where(ModelEvaluation.kind == kind)
    return _page(db, q.order_by(ModelEvaluation.id.desc()), limit, offset, _ev)


@router.get(
    "/evaluations/{eval_uid}",
    responses=ERR,
    summary="One evaluation with metrics, baselines, validity and reproducibility",
)
def get_evaluation(eval_uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _ev(_ev_get(db, eval_uid), full=True)


@router.post(
    "/evaluations/{eval_uid}/cancel", responses=ERR, summary="Cancel a queued evaluation, or ask a running one to stop"
)
def cancel_evaluation(eval_uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        e = _ev_get(db, eval_uid)
        if e.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this evaluation.")
        evaluate.cancel(db, e)
        audit.record(
            db,
            actor,
            "modelops.evaluation.cancel",
            target_type="MODEL_EVALUATION",
            target_id=e.id,
            details={"status": e.status.value},
            request_id=request_id_of(request),
        )
        return _ev(e)


def _finished(e: ModelEvaluation) -> ModelEvaluation:
    if e.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise DomainValidationError(
            f"The evaluation is {e.status.value}; this section exists only for completed evaluations."
        )
    return e


def _section(db, eval_uid: str, *names: str) -> dict[str, Any]:  # noqa: ANN001
    e = _finished(_ev_get(db, eval_uid))
    return {
        "evaluation": _ev(e),
        **{n: getattr(e, n) for n in names},
        "warnings": e.warnings,
        "limitations": e.limitations,
    }


@router.get("/evaluations/{eval_uid}/baselines", responses=ERR, summary="Baseline comparison on the same data")
def baselines(eval_uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(db, eval_uid, "baselines")


@router.get("/evaluations/{eval_uid}/folds", responses=ERR, summary="Fold or split results")
def folds(eval_uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(db, eval_uid, "folds", "split")


@router.get("/evaluations/{eval_uid}/segments", responses=ERR, summary="Segment results (small segments suppressed)")
def segments(eval_uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(db, eval_uid, "segments")


@router.get("/evaluations/{eval_uid}/calibration", responses=ERR, summary="Calibration and uncertainty diagnostics")
def calibration(eval_uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(db, eval_uid, "calibration", "intervals")


# ------------------------------------------------------------------------------------------------ explanations / monitoring / card
@router.post(
    "/models/{uid}/explanations",
    responses=ERR,
    summary="Explain a model globally or one stored assessment locally (cached per version)",
)
def explain_model(uid: UidPath, body: ExplainBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        m = _mv_get(db, uid)
        if body.scope == "GLOBAL":
            out = explain.global_explanation(db, m, body.method, actor.name)
        else:
            if not body.subject:
                raise DomainValidationError("A local explanation needs a subject (the assessment id).")
            out = explain.local_explanation(db, m, body.subject, body.method, actor.name)
        audit.record(db, actor, "modelops.explain", target_type="MODEL_VERSION", target_id=m.id, details={"scope": body.scope, "method": body.method, "status": out["status"]}, request_id=request_id_of(request))  # fmt: skip
        return out


@router.get("/models/{uid}/explanations", responses=ERR, summary="Stored explanations (traceable to this version)")
def list_explanations(uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    mid = _model_id(db, uid)
    q = select(ModelExplanation).where(ModelExplanation.model_version_id == mid).order_by(ModelExplanation.id.desc())
    return _page(db, q, limit, offset, lambda x: {"id": x.id, "scope": x.scope, "subject": x.subject_ref, "method": x.method, "method_version": x.method_version, "status": x.status, "created_by": x.created_by, "created_at": _iso(x.created_at), "result": x.result})  # fmt: skip


@router.post(
    "/models/{uid}/monitoring/checks", responses=ERR, summary="Run a drift and data-quality check (observation only)"
)
def run_check(uid: UidPath, body: MonitorBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        m = _mv_get(db, uid)
        out = monitor.run_check(db, m, actor.name)
        audit.record(db, actor, "modelops.monitor", target_type="MODEL_VERSION", target_id=m.id, details={"overall": out["overall"], "check": out["check_uid"]}, request_id=request_id_of(request))  # fmt: skip
        return out


@router.get("/models/{uid}/monitoring", responses=ERR, summary="Stored monitoring diagnostics (newest first)")
def diagnostics(uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, kind: Annotated[str | None, Query(max_length=24)] = None) -> dict[str, Any]:  # fmt: skip
    mid = _model_id(db, uid)
    q = select(ModelDiagnostic).where(ModelDiagnostic.model_version_id == mid)
    if kind:
        q = q.where(ModelDiagnostic.kind == kind)
    return {
        **_page(db, q.order_by(ModelDiagnostic.id.desc()), limit, offset, monitor.serialize),
        "statement": C.STATEMENTS["drift"],
    }


@router.get("/models/{uid}/card", responses=ERR, summary="Model card generated from persisted records")
def model_card(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return cards.build(db, _mv_get(db, uid))
