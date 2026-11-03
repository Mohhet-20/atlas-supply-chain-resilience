"""Alert analytics: deterministic figures from fixed timestamps. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.alerting import metrics as MX
from app.alerting import service as S
from app.models.alerting import Alert
from tests.alerting_helpers import T0, ctx, event, policy_body, rule_body

M = timedelta(minutes=1)


def test_analytics_figures_are_exact_and_keep_unresolved_alerts(session):
    c = ctx()
    p = S.create_policy(session, "rev", policy_body(), c)  # ack 10 min, resolve 120 min
    S.create_rule(session, "u", rule_body(escalation_policy=p.policy_uid), c)
    S.ingest_event(session, "e", event("a", entities=[{"entity_type": "facility", "entity_id": "F-1"}]), c, T0)
    S.ingest_event(session, "e", event("b", entities=[{"entity_type": "facility", "entity_id": "F-2"}]), c, T0)
    first, second = session.scalars(select(Alert).order_by(Alert.id)).all()
    S.transition(session, first, "acknowledged", "editor-user", None, c, now=T0 + 4 * M)
    S.transition(session, first, "resolved", "editor-user", "fixed", c, now=T0 + 30 * M)
    out = MX.analytics(session, T0 + 60 * M, 7)
    assert out["alerts_in_window"] == 2
    assert out["acknowledgement"]["n"] == 1 and out["acknowledgement"]["mean_minutes"] == 4.0
    assert out["acknowledgement"]["unacknowledged"] == 1
    assert out["resolution"]["median_minutes"] == 30.0 and out["resolution"]["unresolved"] == 1
    assert out["sla"]["acknowledgement"] == {"met": 1, "missed": 1, "not_yet_due": 0, "met_pct": 50.0}
    r = out["sla"]["resolution"]
    assert (r["met"], r["missed"], r["not_yet_due"]) == (1, 0, 1) and r["met_pct"] == 100.0
    assert out["unresolved_age"]["total_live"] == 1 and out["unresolved_age"]["under_1_day"] == 1
    assert out["by_severity"] == {"HIGH": 2}


def test_empty_window_reports_none_not_zero(session):
    out = MX.analytics(session, T0, 30)
    assert out["alerts_in_window"] == 0
    assert out["acknowledgement"]["mean_minutes"] is None
    assert out["sla"]["acknowledgement"]["met_pct"] is None
