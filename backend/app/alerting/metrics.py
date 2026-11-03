"""Alert analytics. Every figure states its population, timestamp fields and exclusions; nothing is estimated and unresolved alerts are never dropped from age figures."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.alerting import ACTIVE_STATES, Alert, AlertEscalation, AlertEvent, AlertRule, NotificationDelivery


def _aw(d: datetime | None) -> datetime | None:
    return None if d is None else (d if d.tzinfo else d.replace(tzinfo=timezone.utc))


def _mins(a: datetime, b: datetime) -> float:
    return (_aw(b) - _aw(a)).total_seconds() / 60  # type: ignore[operator]


def _dist(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean_minutes": None, "median_minutes": None}
    return {
        "n": len(values),
        "mean_minutes": round(statistics.fmean(values), 1),
        "median_minutes": round(statistics.median(values), 1),
    }


def _cnt(db: Session, col: Any, *where: Any) -> dict[str, int]:
    return {str(k): int(v) for k, v in db.execute(select(col, func.count()).where(*where).group_by(col)).all()}


def analytics(db: Session, now: datetime, days: int) -> dict[str, Any]:
    start = now - timedelta(days=days)
    win = (Alert.first_seen_at >= start, Alert.first_seen_at <= now)
    alerts = db.scalars(select(Alert).where(*win).limit(20000)).all()
    n = len(alerts)
    ack = [_mins(a.first_seen_at, a.acknowledged_at) for a in alerts if a.acknowledged_at]
    res = [_mins(a.first_seen_at, a.resolved_at) for a in alerts if a.resolved_at]

    def sla(due_field: str, done_field: str) -> dict[str, Any]:
        met = missed = pending = 0
        for a in alerts:
            due, done = getattr(a, due_field), getattr(a, done_field)
            if due is None:
                continue
            if done is not None:
                met, missed = (met + 1, missed) if _aw(done) <= _aw(due) else (met, missed + 1)  # type: ignore[operator]
            elif _aw(due) < now:  # type: ignore[operator]
                missed += 1
            else:
                pending += 1  # not yet due: neither met nor missed
        judged = met + missed
        return {
            "met": met,
            "missed": missed,
            "not_yet_due": pending,
            "met_pct": round(100 * met / judged, 1) if judged else None,
        }

    active = db.scalars(select(Alert).where(Alert.status.in_(ACTIVE_STATES)).limit(20000)).all()
    buckets = {"under_1_day": 0, "1_to_3_days": 0, "3_to_7_days": 0, "over_7_days": 0}
    for a in active:
        age = _mins(a.first_seen_at, now) / 1440
        buckets[
            "under_1_day" if age < 1 else "1_to_3_days" if age < 3 else "3_to_7_days" if age < 7 else "over_7_days"
        ] += 1
    deliveries = {
        str(k): int(v)
        for k, v in db.execute(
            select(NotificationDelivery.status, func.count())
            .select_from(NotificationDelivery)
            .join(Alert, Alert.id == NotificationDelivery.alert_id)  # joins the organisation-scoped Alert
            .where(NotificationDelivery.created_at >= start, NotificationDelivery.created_at <= now)
            .group_by(NotificationDelivery.status)
        ).all()
    }
    ok = deliveries.get("DELIVERED", 0) + deliveries.get("ACCEPTED", 0)
    bad = deliveries.get("FAILED", 0) + deliveries.get("EXHAUSTED", 0)
    escalated = int(
        db.scalar(
            select(func.count(func.distinct(AlertEscalation.alert_id)))
            .join(Alert, Alert.id == AlertEscalation.alert_id)
            .where(*win)
        )
        or 0
    )
    by_rule = db.execute(
        select(AlertRule.name, func.count(Alert.id), func.coalesce(func.sum(Alert.occurrence_count), 0))
        .join(AlertRule, AlertRule.id == Alert.rule_id)
        .where(*win)
        .group_by(AlertRule.name)
        .order_by(func.count(Alert.id).desc())
        .limit(10)
    ).all()
    return {
        "window_days": days, "window": {"start": start.isoformat(), "end": now.isoformat()}, "alerts_in_window": n,
        "definitions": {
            "population": "Alerts whose first_seen_at falls in the window. Current-state and age figures use the alerts that are live now, whatever their age.",
            "acknowledgement_time": "acknowledged_at minus first_seen_at, over alerts in the window that have been acknowledged. Unacknowledged alerts are counted separately, not dropped.",
            "resolution_time": "resolved_at minus first_seen_at, over alerts in the window that have been resolved. Unresolved alerts appear in the age figures.",
            "sla": "Only alerts raised under an escalation policy have targets. Met: done by the target. Missed: done late, or still not done after the target. Not yet due is excluded from the percentage. Elapsed time is wall-clock UTC; no business calendar is applied.",
            "notification_failure_rate": "(FAILED + EXHAUSTED) / (DELIVERED + ACCEPTED + FAILED + EXHAUSTED) over deliveries created in the window. SKIPPED and still-pending deliveries are excluded and shown separately. ACCEPTED means the provider accepted it, not that a person read it.",
        },
        "by_severity": dict(sorted(_cnt(db, Alert.severity, *win).items())), "by_source": _cnt(db, Alert.source, *win), "by_basis": _cnt(db, Alert.basis, *win),
        "by_state_now": _cnt(db, Alert.status, *win),
        "acknowledgement": {**_dist(ack), "unacknowledged": sum(1 for a in alerts if not a.acknowledged_at and a.status in ACTIVE_STATES)},
        "resolution": {**_dist(res), "unresolved": sum(1 for a in alerts if not a.resolved_at and a.status in ACTIVE_STATES)},
        "sla": {"acknowledgement": sla("due_ack_at", "acknowledged_at"), "resolution": sla("due_resolve_at", "resolved_at")},
        "unresolved_age": {**buckets, "total_live": len(active), "oldest_days": round(max((_mins(a.first_seen_at, now) for a in active), default=0) / 1440, 1) if active else None},
        "repeated_alert_rate": {"repeated": sum(1 for a in alerts if a.occurrence_count > 1), "of": n, "pct": round(100 * sum(1 for a in alerts if a.occurrence_count > 1) / n, 1) if n else None},
        "escalation_rate": {"escalated": escalated, "of": n, "pct": round(100 * escalated / n, 1) if n else None},
        "noise": {
            "duplicate_events": int(db.scalar(select(func.coalesce(func.sum(AlertEvent.duplicate_count), 0)).where(AlertEvent.ingested_at >= start, AlertEvent.ingested_at <= now)) or 0),
            "suppressed_alerts": sum(1 for a in alerts if a.suppression_id is not None), "reopened_alerts": sum(1 for a in alerts if a.reopened_count > 0),
            "throttled_notifications": int(sum(a.notifications_throttled for a in alerts)), "repeat_occurrences": int(sum(max(a.occurrence_count - 1, 0) for a in alerts)),
        },
        "notifications": {"by_status": deliveries, "failure_rate_pct": round(100 * bad / (ok + bad), 1) if ok + bad else None, "terminal": ok + bad, "pending": deliveries.get("QUEUED", 0) + deliveries.get("ATTEMPTED", 0), "skipped": deliveries.get("SKIPPED", 0)},
        "top_rules": [{"rule": r, "alerts": int(a), "occurrences": int(o)} for r, a, o in by_rule],
    }  # fmt: skip


def sla_view(db: Session, now: datetime, soon_minutes: int = 60) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in db.scalars(
        select(Alert)
        .where(Alert.status.in_(ACTIVE_STATES), (Alert.due_ack_at.is_not(None)) | (Alert.due_resolve_at.is_not(None)))
        .order_by(Alert.priority, Alert.id)
        .limit(500)
    ):
        for target, due, done in (
            ("acknowledge", a.due_ack_at, a.acknowledged_at),
            ("resolve", a.due_resolve_at, None),
        ):
            if due is None or done is not None:
                continue
            rem = _mins(now, due)
            out.append({"alert_uid": a.alert_uid, "title": a.title, "severity": a.severity, "priority": a.priority, "status": a.status, "assignee": a.assignee, "target": target, "due_at": _aw(due).isoformat(),  # type: ignore[union-attr]
                        "elapsed_minutes": round(_mins(a.first_seen_at, now), 1), "remaining_minutes": round(rem, 1), "state": "overdue" if rem < 0 else "due_soon" if rem <= soon_minutes else "on_track"})  # fmt: skip
    out.sort(key=lambda r: r["remaining_minutes"])
    return out
