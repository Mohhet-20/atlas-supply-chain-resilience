"""Integration health, derived from facts (recent runs, connection state) and never stored as a free-floating flag."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations import secrets
from app.integrations.connectors import CONNECTORS
from app.models.integrations import Integration, IntegrationRun, SyncError, WebhookDelivery


def compute(db: Session, integ: Integration) -> dict[str, Any]:
    runs = db.scalars(
        select(IntegrationRun)
        .where(IntegrationRun.integration_id == integ.id, IntegrationRun.status.in_(("succeeded", "partial", "failed")))
        .order_by(IntegrationRun.id.desc())
        .limit(10)
    ).all()
    failed = sum(1 for r in runs if r.status == "failed")
    partial = sum(1 for r in runs if r.status == "partial")
    durations = [r.duration_ms for r in runs if r.duration_ms is not None]
    conn = CONNECTORS.get(integ.provider)
    needs_cred = bool(conn and conn.info.needs_credential)
    reasons: list[str] = []
    if (
        integ.status in ("draft",)
        or conn is None
        or (needs_cred and secrets.credential_status(integ.credential_ref) != "available")
    ):
        state = "not_configured"
        reasons.append("draft or credential missing" if conn else "connector not available")
    elif integ.status == "disabled" or not integ.enabled:
        state = "not_configured"
        reasons.append("disabled")
    elif len(runs) >= 3 and all(r.status == "failed" for r in runs[:3]):
        state, _ = "unavailable", reasons.append("the last 3 runs failed")
    elif runs and runs[0].status == "failed":
        state, _ = "degraded", reasons.append("the last run failed")
    elif runs and runs[0].status == "partial":
        state, _ = "degraded", reasons.append("the last run had rejected or failed records")
    elif runs and failed / len(runs) > 0.2:
        state, _ = "degraded", reasons.append("more than 20% of recent runs failed")
    else:
        state = "healthy"
    return {
        "state": state, "reasons": reasons, "recent_runs": len(runs), "error_rate": round(failed / len(runs), 3) if runs else None, "partial_runs": partial,
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None, "retries": sum(r.retries for r in runs),
        "data_quality_failures": sum(r.rejected + r.failed for r in runs), "last_success_at": integ.last_success_at.isoformat() if integ.last_success_at else None,
        "last_failure_at": integ.last_failure_at.isoformat() if integ.last_failure_at else None, "last_run_status": runs[0].status if runs else None,
    }  # fmt: skip


def overview(db: Session) -> dict[str, Any]:
    integs = db.scalars(select(Integration)).all()
    states: dict[str, int] = {}
    for i in integs:
        s = compute(db, i)["state"]
        states[s] = states.get(s, 0) + 1
    active = (
        db.scalar(
            select(func.count()).select_from(IntegrationRun).where(IntegrationRun.status.in_(("queued", "running")))
        )
        or 0
    )
    recent = db.scalars(
        select(IntegrationRun).where(IntegrationRun.status == "failed").order_by(IntegrationRun.id.desc()).limit(5)
    ).all()
    return {
        "total": len(integs), "by_health": states, "active_syncs": active,
        "recent_failures": [{"run_uid": r.run_uid, "integration_id": r.integration_id, "error_summary": r.error_summary, "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in recent],
        "webhook_queue_depth": db.scalar(select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.status.in_(("queued", "attempted")))) or 0,
        "webhook_dead_letters": db.scalar(select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.status == "dead")) or 0,
        "sync_errors_recorded": db.scalar(select(func.count()).select_from(SyncError)) or 0,
    }  # fmt: skip
