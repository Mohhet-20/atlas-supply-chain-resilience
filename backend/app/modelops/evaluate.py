"""Evaluation runs: immutable once finished, idempotent, bounded and cancellable (QUEUED → RUNNING → COMPLETED | FAILED | CANCELLED).

A run reads persisted evidence and writes only ``model_evaluations`` (plus the lifecycle stamp DEVELOPMENT → EVALUATED).
``result_status`` separates a finished-but-unsupported or insufficient evaluation from a completed, valid one.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError
from app.modelops import config as C
from app.modelops import evidence, registry
from app.modelops import validity as V
from app.models.enums import SimStatus
from app.models.modelops import ModelEvaluation, ModelVersion
from app.models.types import utcnow

logger = logging.getLogger("atlas.modelops")
ACTIVE = (SimStatus.QUEUED, SimStatus.RUNNING)


def create(session: Session, mv: ModelVersion, cfg: C.EvalConfig, requested_by: str, limits: C.Limits, *, idempotency_key: str | None = None) -> tuple[ModelEvaluation, bool]:  # fmt: skip
    digest = cfg.digest()
    if idempotency_key:
        prior = session.scalar(select(ModelEvaluation).where(ModelEvaluation.idempotency_key == idempotency_key))
        if prior is not None:
            if (prior.requested_by, prior.config_hash, prior.model_version_id) != (requested_by, digest, mv.id):
                raise ConflictError("This idempotency key was already used for a different request.")
            return prior, False
    now = utcnow()
    from datetime import timedelta

    cutoff = now - timedelta(seconds=limits.stale_lock_seconds)
    for old in session.scalars(select(ModelEvaluation).where(ModelEvaluation.status.in_(ACTIVE))):
        if (old.heartbeat_at or old.queued_at) < cutoff:
            old.status, old.finished_at, old.error = SimStatus.FAILED, now, "abandoned: no heartbeat"
    session.flush()
    active = registry.count_active_evaluations(session)
    if active >= limits.max_active_evaluations:
        raise ConflictError(
            f"{active} evaluations are already queued or running (limit {limits.max_active_evaluations}). Try again shortly."
        )
    ev = ModelEvaluation(
        eval_uid=f"EV-{uuid.uuid4().hex[:16].upper()}", model_version_id=mv.id, kind=cfg.kind, status=SimStatus.QUEUED, requested_by=requested_by,
        idempotency_key=idempotency_key, config=cfg.canonical(), config_hash=digest, algorithm_version=C.ALGORITHM_VERSION, queued_at=now,
    )  # fmt: skip
    session.add(ev)
    session.flush()
    return ev, True


def cancel(session: Session, ev: ModelEvaluation) -> ModelEvaluation:
    from app.core.errors import InvalidTransitionError

    if ev.status is SimStatus.QUEUED:
        ev.status, ev.finished_at, ev.error = SimStatus.CANCELLED, utcnow(), "cancelled before start"
    elif ev.status is SimStatus.RUNNING:
        ev.cancel_requested = True
    else:
        raise InvalidTransitionError(f"A {ev.status.value} evaluation cannot be cancelled.")
    return ev


def compute(session: Session, mv: ModelVersion, cfg: C.EvalConfig) -> dict[str, Any]:
    res = evidence.dispatch(session, mv, cfg)
    res["validity"] = {"overall": V.overall(res["checks"]) if res["checks"] else "not_checked", "checks": res["checks"]}
    if res["validity"]["overall"] == "invalid":
        res["result_status"] = "invalid"
        res["warnings"].append(
            "One or more validity checks failed: the metrics are kept for inspection but must not be used as evidence of performance."
        )
    return res


def execute(factory: sessionmaker[Session], eval_id: int, limits: C.Limits) -> None:
    session = factory()
    try:
        claimed = session.execute(update(ModelEvaluation).where(ModelEvaluation.id == eval_id, ModelEvaluation.status == SimStatus.QUEUED).values(status=SimStatus.RUNNING, started_at=utcnow(), heartbeat_at=utcnow())).rowcount  # fmt: skip
        session.commit()
        if not claimed:
            return
        t0 = time.monotonic()
        try:
            ev = session.get(ModelEvaluation, eval_id)
            mv = session.get(ModelVersion, ev.model_version_id)
            cfg = C.EvalConfig.model_validate(ev.config)
            if session.scalar(select(ModelEvaluation.cancel_requested).where(ModelEvaluation.id == eval_id)):
                raise _Cancelled
            res = compute(session, mv, cfg)
            if time.monotonic() - t0 > limits.time_budget_seconds:
                raise _Budget
            session.refresh(ev)
            if ev.cancel_requested:
                raise _Cancelled
            for k in ("dataset_ref", "split", "sample", "metrics", "baselines", "folds", "segments", "calibration", "intervals", "validity", "warnings", "limitations"):  # fmt: skip
                setattr(ev, k, res.get(k))
            ev.reproducibility = {"config_hash": ev.config_hash, "algorithm_version": C.ALGORITHM_VERSION, "model_version": mv.model_uid, "artifact_checksum": mv.artifact_checksum, "evidence_hash": evidence.input_hash(res), "note": "Re-running with the same configuration against unchanged stored evidence reproduces these values."}  # fmt: skip
            ev.software = {**registry.deps(), "code_revision": registry.code_revision()}
            ev.result_status = res["result_status"]
            ev.duration_ms = int((time.monotonic() - t0) * 1000)
            ev.finished_at = utcnow()
            ev.status = SimStatus.COMPLETED
            if res["result_status"] == "completed":
                registry.mark_evaluated(session, mv, ev.requested_by)
            session.commit()
        except _Cancelled:
            session.rollback()
            _finish(session, eval_id, SimStatus.CANCELLED, "cancelled by request")
        except _Budget:
            session.rollback()
            _finish(session, eval_id, SimStatus.FAILED, "compute budget exceeded")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            logger.exception("evaluation failed", extra={"evaluation": eval_id})
            _finish(session, eval_id, SimStatus.FAILED, f"unexpected {type(exc).__name__}")
    finally:
        session.close()


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


def _finish(session: Session, eval_id: int, status: SimStatus, err: str) -> None:
    session.execute(update(ModelEvaluation).where(ModelEvaluation.id == eval_id, ModelEvaluation.status == SimStatus.RUNNING).values(status=status, finished_at=utcnow(), error=err))  # fmt: skip
    session.commit()


def run_sync(
    session: Session, mv: ModelVersion, cfg: C.EvalConfig, requested_by: str, limits: C.Limits | None = None
) -> ModelEvaluation:
    """Create and execute in the caller's session (tests, scripts). The API uses ``create`` + a background ``execute``."""
    lim = limits or C.Limits()
    ev, _ = create(session, mv, cfg, requested_by, lim)
    eid = ev.id
    session.commit()
    execute(sessionmaker(bind=session.get_bind(), expire_on_commit=False), eid, lim)
    return session.get(ModelEvaluation, eid)
