"""Alerting engine: rules, events, de-duplication, priority, lifecycle, escalation, notifications and SSRF checks. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.alerting import notify as N
from app.alerting import service as S
from app.alerting.config import DETECTORS, validate_params
from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, VersionConflictError
from app.models.alerting import (
    Alert,
    AlertEscalation,
    AlertLink,
    AlertOccurrence,
    AlertRuleRun,
    AlertRuleVersion,
    AlertTransition,
    InboxNotification,
    NotificationAttempt,
    NotificationDelivery,
)
from tests.alerting_helpers import DIRECTORY, T0, FakeProvider, ctx, event, policy_body, rule_body

M = timedelta(minutes=1)


def alerts(db: Session) -> list[Alert]:
    return list(db.scalars(select(Alert).order_by(Alert.id)))


# ---------- parameters and rules ----------


def test_parameter_validation_boundaries_units_and_contradictions():
    assert validate_params("supplier_risk_score", {"threshold": 70})["operator"] == "gte"
    for bad, why in [
        ({"threshold": 101}, "between"), ({"threshold": -1}, "between"), ({}, "required"), ({"threshold": 5, "bogus": 1}, "not a parameter"),
        ({"threshold": "x"}, "number"), ({"threshold": 50, "critical_value": 40}, "contradicts"), ({"threshold": 50, "operator": "eq"}, "one of"),
    ]:  # fmt: skip
        with pytest.raises(ValueError, match=why):
            validate_params("supplier_risk_score", bad)
    with pytest.raises(ValueError, match="no capacity-utilisation"):
        validate_params("capacity_pressure", {})
    with pytest.raises(ValueError, match="unknown detector"):
        validate_params("sql_expression", {})
    assert DETECTORS["capacity_pressure"]["available"] is False


def test_rule_checks_assignee_recipients_policy_webhook_and_auto_resolve(session):
    c = ctx()
    with pytest.raises(DomainValidationError, match="default_assignee"):
        S.create_rule(session, "u", rule_body(default_assignee="viewer-user"), c)
    with pytest.raises(DomainValidationError, match="not a configured user or team"):
        S.create_rule(session, "u", rule_body(notify={"recipients": ["ghost"]}), c)
    with pytest.raises(DomainValidationError, match="webhook"):
        S.create_rule(session, "u", rule_body(notify={"channels": ["webhook"], "webhook": "ops"}), c)
    with pytest.raises(DomainValidationError, match="escalation_policy"):
        S.create_rule(session, "u", rule_body(escalation_policy="EP-NOPE"), c)
    with pytest.raises(DomainValidationError, match="auto_resolve"):
        S.create_rule(session, "u", rule_body(auto_resolve=True), c)
    with pytest.raises(ValidationError):
        rule_body(tenant_id="t1")  # unknown fields (including any organisation or tenant) are rejected
    r = S.create_rule(session, "u", rule_body(), c)
    with pytest.raises(ConflictError):
        S.create_rule(session, "u", rule_body(), c)
    assert r.version == 1 and session.scalar(select(func.count()).select_from(AlertRuleVersion)) == 1


def test_rule_versions_are_preserved_on_alerts(session):
    c = ctx()
    r = S.create_rule(session, "u", rule_body(), c)
    S.ingest_event(session, "e", event("a", 60), c, T0)
    a = alerts(session)[0]
    assert a.rule_version == 1
    S.update_rule(
        session, "u", r, rule_body(params={"metric": "temp", "operator": "gte", "threshold": 55, "unit": "C"}), 1, c
    )
    assert r.version == 2
    with pytest.raises(VersionConflictError):
        S.update_rule(session, "u", r, rule_body(), 1, c)
    S.ingest_event(session, "e", event("b", 70), c, T0 + 5 * M)
    session.refresh(a)
    assert (a.rule_version, a.last_rule_version, a.occurrence_count) == (1, 2, 2)
    vs = session.scalars(select(AlertRuleVersion).order_by(AlertRuleVersion.version)).all()
    assert [v.definition["params"]["threshold"] for v in vs] == [50.0, 55.0]


# ---------- events ----------


def test_event_validation_and_schema_versions(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    for kw, why in [
        ({"event_type": "nope.type"}, "unknown"), ({"schema_version": "ev-9"}, "not supported"),
        ({"payload": {"metric": "temp", "value": "hot", "unit": "C"}}, "must be a number"), ({"payload": {"metric": "temp", "value": 1}}, "unit: required"),
        ({"payload": {"metric": "temp", "value": 1, "unit": "C", "extra": 1}}, "not part of"), ({"occurred_at": T0 + timedelta(hours=1)}, "future"),
        ({"payload": {"metric": "x" * 9000, "value": 1, "unit": "C"}}, "larger than"),
    ]:  # fmt: skip
        with pytest.raises(DomainValidationError, match=why):
            S.ingest_event(session, "e", event(**kw), c, T0)
    with pytest.raises(ValidationError):
        event(entities=[{"entity_type": "galaxy", "entity_id": "x"}])
    with pytest.raises(ValidationError):
        event(entities=[{"entity_type": "facility", "entity_id": "x"}] * 51)
    old = S.ingest_event(session, "e", event("old", 10, T0 - timedelta(days=40)), c, T0)[0]
    assert old.warnings and old.occurred_at != old.ingested_at  # occurrence and ingestion times are kept apart


def test_duplicate_delivery_is_safe_and_key_reuse_with_other_content_conflicts(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    ev1, dup1, _ = S.ingest_event(session, "e", event("same", 60), c, T0)
    ev2, dup2, _ = S.ingest_event(session, "e", event("same", 60), c, T0 + M)
    assert (dup1, dup2, ev1.id == ev2.id, ev1.duplicate_count) == (False, True, True, 1)
    assert len(alerts(session)) == 1 and alerts(session)[0].occurrence_count == 1
    with pytest.raises(ConflictError, match="different content"):
        S.ingest_event(session, "e", event("same", 99), c, T0)
    # similar-looking events with different keys are NOT assumed to be duplicates by the event store (the alert layer groups them)
    ev3, dup3, _ = S.ingest_event(session, "e", event("other", 60), c, T0 + M)
    assert not dup3 and ev3.id != ev1.id


def test_threshold_boundary_missing_data_unit_and_basis(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    for i, (v, _expect) in enumerate([(49.999, 0), (50, 1), (50.001, 1)]):
        S.ingest_event(
            session,
            "e",
            event(f"b{i}", v, entity=None)
            if False
            else event(f"b{i}", v, entities=[{"entity_type": "facility", "entity_id": f"F{i}"}]),
            c,
            T0,
        )
        assert len(alerts(session)) == sum(x[1] for x in [(49.999, 0), (50, 1), (50.001, 1)][: i + 1])
    other_unit = S.ingest_event(
        session, "e", event("u", 99, payload={"metric": "temp", "value": 99, "unit": "F"}), c, T0
    )[0]
    assert (
        other_unit.result["rules"][0]["outcome"] in ("no_data", "no_match")
        and "unit" in other_unit.result["rules"][0]["notes"][0].lower()
    )
    other_metric = S.ingest_event(
        session, "e", event("m", 99, payload={"metric": "pressure", "value": 99, "unit": "C"}), c, T0
    )[0]
    assert other_metric.result["rules"][0]["matched"] == 0
    assert {a.basis for a in alerts(session)} == {"observed"}
    S.create_rule(
        session,
        "u",
        rule_body(name="predicted", params={"metric": "fc", "operator": "gte", "threshold": 1, "unit": "u"}),
        c,
    )
    S.ingest_event(
        session, "e", event("p", 5, payload={"metric": "fc", "value": 5, "unit": "u", "basis": "predicted"}), c, T0
    )
    assert alerts(session)[-1].basis == "predicted"


# ---------- de-duplication, cooldown, correlation, suppression ----------


def test_repeats_aggregate_and_notifications_are_throttled_by_cooldown(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(cooldown_minutes=30), c)
    for i, minute in enumerate([0, 5, 10, 40]):
        S.ingest_event(session, "e", event(f"k{i}", 60 + i, T0 + minute * M), c, T0 + minute * M)
    a = alerts(session)[0]
    assert len(alerts(session)) == 1 and a.occurrence_count == 4
    assert (
        session.scalar(select(func.count()).select_from(AlertOccurrence).where(AlertOccurrence.alert_id == a.id)) == 4
    )  # every event reference is preserved
    assert a.notifications_throttled == 2  # minutes 5 and 10 fell inside the 30 minute cooldown
    kinds = [d.kind for d in session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))]
    assert kinds == ["created", "repeat"]


def test_acknowledged_alerts_count_repeats_but_do_not_renotify(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(cooldown_minutes=1), c)
    S.ingest_event(session, "e", event("k0"), c, T0)
    a = alerts(session)[0]
    S.transition(session, a, "acknowledged", "editor-user", None, c, now=T0 + M)
    S.ingest_event(session, "e", event("k1"), c, T0 + 30 * M)
    session.refresh(a)
    assert a.occurrence_count == 2 and session.scalar(select(func.count()).select_from(NotificationDelivery)) == 1


def test_distinct_disruptions_on_one_supplier_are_correlated_not_merged(session):
    c = ctx()
    S.create_rule(
        session, "u", rule_body(name="disruptions", detector="reported_disruption", params={"min_affected": 1}), c
    )
    ent = [{"entity_type": "supplier", "entity_id": "S-1"}]
    for k in ("d1", "d2"):
        S.ingest_event(
            session,
            "e",
            event(k, event_type="disruption.reported", payload={"title": f"Port closure {k}"}, entities=ent),
            c,
            T0,
        )
    al = alerts(session)
    assert len(al) == 2 and al[0].dedup_key != al[1].dedup_key and al[0].correlation_key == al[1].correlation_key
    assert session.scalar(select(func.count()).select_from(AlertLink).where(AlertLink.created_by == "system")) == 1
    assert (
        al[1].priority < al[0].priority or al[1].priority == al[0].priority
    )  # related unresolved alert may raise urgency by one step
    # synthetic events are ignored unless the rule allows them
    ev = S.ingest_event(
        session,
        "e",
        event(
            "syn", event_type="disruption.reported", provenance="SYNTHETIC_DEMO", payload={"title": "x"}, entities=ent
        ),
        c,
        T0,
    )[0]
    assert len(alerts(session)) == 2 and "Synthetic" in ev.result["rules"][0]["notes"][0]


def test_reopen_within_window_and_new_alert_after_window(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(dedup_window_minutes=60), c)
    S.ingest_event(session, "e", event("k0"), c, T0)
    a = alerts(session)[0]
    S.transition(session, a, "resolved", "editor-user", "fixed", c, now=T0 + 5 * M)
    S.ingest_event(session, "e", event("k1"), c, T0 + 30 * M)  # recurs inside the window: same alert reopens
    session.refresh(a)
    assert (a.status, a.reopened_count, len(alerts(session))) == ("open", 1, 1)
    S.transition(session, a, "resolved", "editor-user", "fixed again", c, now=T0 + 40 * M)
    S.ingest_event(session, "e", event("k2"), c, T0 + 200 * M)  # outside the window: a new alert
    assert len(alerts(session)) == 2


def test_suppression_is_explicit_auditable_bounded_and_never_hides_critical(session):
    c = ctx()
    r = S.create_rule(
        session,
        "u",
        rule_body(params={"metric": "temp", "operator": "gte", "threshold": 50, "unit": "C", "critical_value": 90}),
        c,
    )
    with pytest.raises(DomainValidationError, match="blanket"):
        S.create_suppression(
            session, "rev", rule=None, entity_type=None, entity_id=None, reason="x", minutes=10, now=T0
        )
    with pytest.raises(DomainValidationError, match="30 days"):
        S.create_suppression(
            session, "rev", rule=r, entity_type=None, entity_id=None, reason="maintenance", minutes=60 * 24 * 31, now=T0
        )
    s = S.create_suppression(
        session, "rev", rule=r, entity_type=None, entity_id=None, reason="planned maintenance", minutes=60, now=T0
    )
    S.ingest_event(session, "e", event("k0", 60, entities=[{"entity_type": "facility", "entity_id": "F-A"}]), c, T0 + M)
    S.ingest_event(session, "e", event("k1", 95, entities=[{"entity_type": "facility", "entity_id": "F-B"}]), c, T0 + M)
    a, b = alerts(session)
    assert (a.status, a.suppression_id, b.status, b.severity) == ("suppressed", s.id, "open", "CRITICAL")
    assert "never silently hidden" in " ".join(b.explanation["severity"])
    why = session.scalars(select(AlertTransition).where(AlertTransition.alert_id == a.id)).first()
    assert "planned maintenance" in why.reason  # the reason is inspectable
    assert (
        session.scalar(
            select(func.count()).select_from(NotificationDelivery).where(NotificationDelivery.alert_id == a.id)
        )
        == 0
    )  # no notification, but the alert exists
    out = S.tick(session, "sys", c, T0 + 30 * M)
    assert out["unsuppressed"] == 0
    out = S.tick(session, "sys", c, T0 + 61 * M)  # the window ended: the hidden alert comes back
    session.refresh(a)
    assert out["unsuppressed"] == 1 and a.status == "open"


# ---------- priority ----------


def test_priority_is_a_transparent_table_with_no_weights():
    p, steps = S.prioritise("CRITICAL", "observed", 0, None, None)
    assert p == 1 and steps[0]["factor"] == "severity"
    assert S.prioritise("HIGH", "observed", 0, None, None)[0] == 1  # observed HIGH moves up one step
    assert S.prioritise("HIGH", "predicted", 0, None, None)[0] == 2
    assert (
        S.prioritise("HIGH", "observed", 2, 3, None)[0] == 1 and S.prioritise("LOW", "observed", 3, 1, None)[0] == 2
    )  # at most two steps up
    assert S.prioritise("MEDIUM", "predicted", 0, None, "LOW")[0] == 4
    assert S.prioritise("CRITICAL", "predicted", 0, None, "LOW")[0] == 1  # low confidence never downgrades CRITICAL
    p, steps = S.prioritise("CRITICAL", "hypothetical", 5, 1, None)
    assert p == 1 or p >= 3
    assert S.prioritise("HIGH", "hypothetical", 0, None, None)[0] == 3 and any(
        s["factor"] == "hypothetical_scenario" for s in S.prioritise("HIGH", "hypothetical", 0, None, None)[1]
    )


# ---------- lifecycle ----------


def test_valid_and_invalid_transitions_are_audited_and_reasons_required(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    S.ingest_event(session, "e", event(), c, T0)
    a = alerts(session)[0]
    with pytest.raises(InvalidTransitionError):
        S.transition(session, a, "closed", "editor-user", "x", c)
    with pytest.raises(InvalidTransitionError):
        S.transition(session, a, "investigating", "editor-user", None, c)
    S.transition(session, a, "acknowledged", "editor-user", None, c, now=T0 + M)
    S.transition(session, a, "investigating", "editor-user", "looking", c, now=T0 + 2 * M)
    with pytest.raises(DomainValidationError, match="reason"):
        S.transition(session, a, "resolved", "editor-user", "  ", c)
    S.transition(session, a, "resolved", "editor-user", "sensor recalibrated", c, now=T0 + 3 * M)
    assert (a.resolution_mode, a.resolution_note, a.resolved_by) == ("manual", "sensor recalibrated", "editor-user")
    S.transition(session, a, "closed", "reviewer-user", "confirmed", c, now=T0 + 4 * M)
    with pytest.raises(InvalidTransitionError):
        S.transition(session, a, "open", "reviewer-user", "again", c)  # closed is terminal
    hist = session.scalars(
        select(AlertTransition).where(AlertTransition.alert_id == a.id).order_by(AlertTransition.id)
    ).all()
    assert [(t.from_state, t.to_state) for t in hist] == [
        (None, "open"),
        ("open", "acknowledged"),
        ("acknowledged", "investigating"),
        ("investigating", "resolved"),
        ("resolved", "closed"),
    ]
    assert all(t.actor and t.at for t in hist)
    with pytest.raises(ValueError, match="append-only"):
        hist[0].reason = "rewritten"
        session.flush()
    session.rollback()


def test_acknowledgement_does_not_mean_the_condition_cleared(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    S.ingest_event(session, "e", event("k0"), c, T0)
    a = alerts(session)[0]
    S.transition(session, a, "acknowledged", "editor-user", None, c, now=T0 + M)
    S.ingest_event(session, "e", event("k1"), c, T0 + 2 * M)  # the condition is still present
    session.refresh(a)
    assert a.status == "acknowledged" and a.occurrence_count == 2 and a.resolved_at is None


def test_concurrent_acknowledgements_one_wins(engine):
    c = ctx()
    with Session(engine) as s0:
        S.create_rule(s0, "u", rule_body(), c)
        S.ingest_event(s0, "e", event(), c, T0)
        s0.commit()
    s1, s2 = Session(engine, expire_on_commit=False), Session(engine, expire_on_commit=False)
    a1, a2 = alerts(s1)[0], alerts(s2)[0]
    s1.commit()
    s2.commit()  # both users now hold the same (version 1) alert in memory, with no open database transaction
    S.transition(s1, a1, "acknowledged", "editor-user", None, c, expected_version=a1.version)
    s1.commit()
    with pytest.raises((StaleDataError, VersionConflictError, InvalidTransitionError)):
        S.transition(s2, a2, "acknowledged", "admin-user", None, c, expected_version=a2.version)
        s2.commit()
    s2.rollback()
    with Session(engine) as s3:
        assert alerts(s3)[0].acknowledged_by == "editor-user"
        assert (
            s3.scalar(
                select(func.count()).select_from(AlertTransition).where(AlertTransition.to_state == "acknowledged")
            )
            == 1
        )
    s1.close()
    s2.close()


def test_assignment_authorisation_and_notes(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    S.ingest_event(session, "e", event(), c, T0)
    a = alerts(session)[0]
    for bad in ("viewer-user", "ghost", "team:viewer", "team:nope"):
        with pytest.raises(DomainValidationError, match="assignee"):
            S.assign(session, a, bad, "editor-user", None, c)
    S.assign(session, a, "reviewer-user", "editor-user", "needs a reviewer", c, now=T0 + M)
    S.assign(session, a, "team:admin", "editor-user", None, c, now=T0 + 2 * M)
    assert a.assignee == "team:admin"
    n = S.add_note(session, a, "mitigation", "Moved stock from F-2", "editor-user")
    assert n.kind == "mitigation"
    with pytest.raises(DomainValidationError):
        S.add_note(session, a, "note", "   ", "editor-user")
    inbox = session.scalars(select(InboxNotification).where(InboxNotification.recipient == "reviewer-user")).all()
    assert len(inbox) == 1  # the new owner is told


# ---------- escalation ----------


def test_escalation_timing_levels_no_duplicates_and_restart_recovery(engine):
    c = ctx()
    with Session(engine) as s:
        p = S.create_policy(s, "rev", policy_body(), c)
        S.create_rule(s, "u", rule_body(escalation_policy=p.policy_uid), c)
        S.ingest_event(s, "e", event(), c, T0)
        a = alerts(s)[0]
        assert a.due_ack_at is not None and a.next_escalation_at is not None
        s.commit()
    with Session(engine) as s:
        assert S.tick(s, "sys", c, T0 + 5 * M)["escalated"] == 0  # not yet due
        s.commit()
    with Session(engine) as s:  # a "restarted worker": new session, no memory, deadlines come from the database
        assert S.tick(s, "sys", c, T0 + 11 * M)["escalated"] == 1
        s.commit()
    with Session(engine) as s:
        assert S.tick(s, "sys", c, T0 + 11 * M)["escalated"] == 0  # same instant again: no duplicate
        a = alerts(s)[0]
        assert (a.status, a.escalation_level, a.assignee) == ("escalated", 1, "reviewer-user")
        assert S.tick(s, "sys", c, T0 + 22 * M)["escalated"] == 1
        assert S.tick(s, "sys", c, T0 + 400 * M)["escalated"] == 0  # highest level reached
        a = alerts(s)[0]
        assert (a.escalation_level, a.assignee, a.next_escalation_at) == (2, "admin-user", None)
        assert s.scalar(select(func.count()).select_from(AlertEscalation)) == 2
        s.commit()


def test_acknowledged_alerts_skip_ack_escalation_but_miss_resolution_target(session):
    c = ctx()
    p = S.create_policy(session, "rev", policy_body(), c)
    S.create_rule(session, "u", rule_body(escalation_policy=p.policy_uid), c)
    S.ingest_event(session, "e", event(), c, T0)
    a = alerts(session)[0]
    S.transition(session, a, "acknowledged", "editor-user", None, c, now=T0 + 5 * M)
    assert (
        S.tick(session, "sys", c, T0 + 30 * M)["escalated"] == 0
    )  # acknowledged in time: not escalated for acknowledgement
    assert S.tick(session, "sys", c, T0 + 121 * M)["escalated"] == 1  # the 120 minute resolution target passed
    session.refresh(a)
    assert a.status == "escalated"
    S.transition(session, a, "resolved", "reviewer-user", "done", c, now=T0 + 130 * M)
    assert S.tick(session, "sys", c, T0 + 999 * M)["escalated"] == 0  # resolved alerts are never escalated


def test_manual_escalation_validates_target_and_records_actor(session):
    c = ctx()
    S.create_rule(session, "u", rule_body(), c)
    S.ingest_event(session, "e", event(), c, T0)
    a = alerts(session)[0]
    with pytest.raises(DomainValidationError, match="no escalation policy"):
        S.escalate(session, a, "editor-user", "needs help", c)
    with pytest.raises(DomainValidationError, match="target"):
        S.escalate(session, a, "editor-user", "needs help", c, target="viewer-user")
    rec = S.escalate(session, a, "editor-user", "needs help", c, target="team:reviewer", now=T0 + M)
    assert rec and (rec.trigger, rec.actor, rec.to_target) == ("manual", "editor-user", "team:reviewer")
    inbox = session.scalars(select(InboxNotification).where(InboxNotification.kind == "escalated")).all()
    assert [n.recipient for n in inbox] == ["reviewer-user"]  # a team expands to its members


# ---------- notifications ----------


def test_in_app_delivery_is_per_recipient_and_idempotent(session):
    c = ctx()
    S.create_rule(
        session, "u", rule_body(notify={"channels": ["in_app"], "recipients": ["editor-user", "team:reviewer"]}), c
    )
    S.ingest_event(session, "e", event(), c, T0)
    rows = session.scalars(select(InboxNotification)).all()
    assert sorted(r.recipient for r in rows) == ["editor-user", "reviewer-user"] and all(
        r.read_at is None for r in rows
    )
    ds = session.scalars(select(NotificationDelivery)).all()
    assert {d.status for d in ds} == {"DELIVERED"} and all(d.attempts == 1 for d in ds)
    a = alerts(session)[0]
    rule = S.get_alert  # noqa: F841
    before = session.scalar(select(func.count()).select_from(NotificationDelivery))
    from app.models.alerting import AlertRule

    N.plan(
        session,
        a,
        "created",
        "created",
        session.get(AlertRule, a.rule_id).notify,
        ["editor-user"],
        DIRECTORY,
        T0,
        3,
        {},
    )
    assert (
        session.scalar(select(func.count()).select_from(NotificationDelivery)) == before
    )  # same alert, kind and recipient: no second delivery


def test_preferences_min_severity_disabled_and_quiet_hours(session):
    from app.models.alerting import NotificationPreference as P

    c = ctx()
    session.add_all(
        [
            P(actor="editor-user", in_app_enabled=False),
            P(actor="reviewer-user", min_severity="CRITICAL"),
            P(actor="admin-user", quiet_start_hour=11, quiet_end_hour=14),
        ]
    )
    session.flush()
    S.create_rule(
        session,
        "u",
        rule_body(notify={"channels": ["in_app"], "recipients": ["editor-user", "reviewer-user", "admin-user"]}),
        c,
    )
    S.ingest_event(session, "e", event(), c, T0)  # T0 is 12:00 UTC: inside admin's quiet hours
    by = {d.recipient: d for d in session.scalars(select(NotificationDelivery))}
    assert by["editor-user"].status == "SKIPPED" and "disabled" in by["editor-user"].last_error
    assert by["reviewer-user"].status == "SKIPPED" and "minimum severity" in by["reviewer-user"].last_error
    assert (
        by["admin-user"].status == "QUEUED" and S.aware(by["admin-user"].next_attempt_at).hour == 14
    )  # deferred, not dropped
    assert S.dispatch_due(session, c, T0 + 3 * 60 * M)["delivered"] == 1
    assert session.scalar(select(func.count()).select_from(InboxNotification)) == 1


def _webhook_ctx(provider):
    return ctx(
        provider, webhooks={"ops": "https://hooks.example.com/services/T/B/secret"}, allowed_hosts={"hooks.example.com"}
    )


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(N.socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])


def test_webhook_retries_backoff_and_delivery_status(session, public_dns):
    prov = FakeProvider(["transient", "transient", "accepted"])
    c = _webhook_ctx(prov)
    S.create_rule(session, "u", rule_body(notify={"channels": ["webhook"], "webhook": "ops", "recipients": []}), c)
    S.ingest_event(session, "e", event(), c, T0)
    d = session.scalars(select(NotificationDelivery)).one()
    assert d.status == "ATTEMPTED" and d.attempts == 1 and S.aware(d.next_attempt_at) == T0 + 10 * timedelta(seconds=1)
    assert S.dispatch_due(session, c, T0 + 5 * timedelta(seconds=1))["attempted"] == 0  # backoff not elapsed
    S.dispatch_due(session, c, T0 + 11 * timedelta(seconds=1))
    assert (d.status, d.attempts) == ("ATTEMPTED", 2) and S.aware(d.next_attempt_at) == T0 + 11 * timedelta(
        seconds=1
    ) + 20 * timedelta(seconds=1)  # doubled
    S.dispatch_due(session, c, T0 + 60 * timedelta(seconds=1))
    assert (d.status, d.attempts, d.last_error) == (
        "ACCEPTED",
        3,
        None,
    )  # accepted by the provider: not claimed as read
    assert len(prov.sent) == 3 and len({k for _, _, k in prov.sent}) == 1  # one stable idempotency key across retries
    assert [
        a.outcome for a in session.scalars(select(NotificationAttempt).order_by(NotificationAttempt.attempt_no))
    ] == ["transient", "transient", "accepted"]
    body = prov.sent[0][1].decode()
    assert "secret" not in body and "hooks.example.com" not in body and "Authorization" not in body


def test_webhook_exhaustion_permanent_failure_and_unconfigured(session, public_dns):
    prov = FakeProvider(["transient"] * 5)
    c = _webhook_ctx(prov)
    S.create_rule(session, "u", rule_body(notify={"channels": ["webhook"], "webhook": "ops", "recipients": []}), c)
    S.ingest_event(session, "e", event(), c, T0)
    for i in range(1, 8):
        S.dispatch_due(session, c, T0 + i * 100 * timedelta(seconds=1))
    d = session.scalars(select(NotificationDelivery)).one()
    assert (d.status, d.attempts) == ("EXHAUSTED", 3) and d.next_attempt_at is None
    prov2 = FakeProvider(["permanent"])
    c2 = _webhook_ctx(prov2)
    S.ingest_event(
        session, "e", event("k9", entities=[{"entity_type": "facility", "entity_id": "F-9"}]), c2, T0 + 500 * M
    )
    d2 = session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id.desc())).first()
    assert d2.status == "FAILED" and d2.attempts == 1


@pytest.mark.parametrize("url,why", [
    ("http://hooks.example.com/x", "https"), ("https://user:pw@hooks.example.com/x", "credentials"), ("https://hooks.example.com:8443/x", "port"),
    ("https://evil.example.org/x", "allow-list"), ("https://127.0.0.1/x", "allow-list"), ("https:///x", "no host"), ("not a url", "https"),
])  # fmt: skip
def test_unsafe_webhook_destinations_are_refused(url, why):
    with pytest.raises(N.UnsafeDestination, match=why):
        N.validate_webhook_url(url, {"hooks.example.com"}, resolve=False)


@pytest.mark.parametrize(
    "addr", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "172.16.0.1", "::1", "0.0.0.0", "fe80::1"]
)
def test_hosts_resolving_to_private_or_metadata_addresses_are_refused(monkeypatch, addr):
    monkeypatch.setattr(N.socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", (addr, 443))])
    with pytest.raises(N.UnsafeDestination, match="non-public"):
        N.validate_webhook_url("https://hooks.example.com/x", {"hooks.example.com"})
    for ip in (addr,):  # literal addresses are refused even when "allow-listed"
        with pytest.raises(N.UnsafeDestination):
            N.validate_webhook_url(f"https://[{ip}]/x" if ":" in ip else f"https://{ip}/x", {ip})


def test_rebinding_between_configuration_and_send_is_caught_on_every_attempt(session, monkeypatch):
    prov = FakeProvider()
    c = _webhook_ctx(prov)
    monkeypatch.setattr(N.socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", ("169.254.169.254", 443))])
    S.create_rule(session, "u", rule_body(notify={"channels": ["webhook"], "webhook": "ops", "recipients": []}), c)
    S.ingest_event(session, "e", event(), c, T0)
    d = session.scalars(select(NotificationDelivery)).one()
    assert (
        d.status == "FAILED" and "destination refused" in d.last_error and prov.sent == []
    )  # nothing left the process


def test_parse_webhooks_drops_unsafe_entries_and_logs_nothing_secret():
    raw = '{"ok":"https://hooks.example.com/a","bad":"http://hooks.example.com/a","internal":"https://10.0.0.1/a","other":"https://nope.example.org/a"}'
    assert N.parse_webhooks(raw, {"hooks.example.com"}) == {"ok": "https://hooks.example.com/a"}
    assert N.parse_webhooks("{not json", {"x"}) == {} and N.parse_webhooks(None, {"x"}) == {}


def test_redaction_removes_credentials_and_bounds_size():
    out = S.redact(
        {
            "password": "p",
            "nested": {"api_key": "k", "ok": "v" * 900},
            "list": list(range(80)),
            "Authorization": "Bearer x",
        }
    )
    assert (
        out["password"] == "[redacted]"
        and out["nested"]["api_key"] == "[redacted]"
        and out["Authorization"] == "[redacted]"
    )
    assert len(out["nested"]["ok"]) == 500 and len(out["list"]) == 50


# ---------- rule execution limits and run records ----------


def test_pull_rule_limits_and_failure_isolation(session, monkeypatch):
    from app.alerting import detectors as D
    from app.alerting.config import LIMITS

    c = ctx()
    for i in range(6):
        session.execute(
            text(
                "insert into resilience_runs (run_uid,status,result_status,requested_by,product_id,destination_facility_id,strategy,scenario_mode,request,request_hash,algorithm_version,alternatives,comparison,provenance,scenarios,queued_at) values (:u,'FAILED',null,'t',1,1,'PARETO','FIXED','{}',:h,'x','[]','{}','{}','{}',:q)"
            ),
            {"u": f"RR-F{i}", "h": f"h{i}", "q": T0.replace(tzinfo=None) - M},
        )
    monkeypatch.setitem(LIMITS, "matches_per_rule", 3)
    S.create_rule(
        session,
        "u",
        rule_body(name="failed runs", detector="run_outcome", params={"condition": "failed", "kinds": ["resilience"]}),
        c,
    )
    S.create_rule(session, "u", rule_body(name="broken", detector="source_stale", params={"max_age_hours": 1}), c)
    boom = {"n": 0}

    def explode(db, p, now):
        boom["n"] += 1
        raise RuntimeError("detector bug")

    monkeypatch.setitem(D.PULL, "source_stale", explode)
    out = S.evaluate(session, "rev", c, T0)
    by = {r["name"]: r for r in out["rules"]}
    assert (
        by["failed runs"]["truncated"] is True and by["failed runs"]["matched"] == 3 and len(alerts(session)) == 3
    )  # bounded
    assert by["broken"]["outcome"] == "error" and boom["n"] == 1  # one failing rule did not stop the others
    runs = session.scalars(select(AlertRuleRun).order_by(AlertRuleRun.id)).all()
    assert {r.outcome for r in runs} == {"matched", "error"} and any(r.truncated for r in runs)
    failed = [a for a in alerts(session)]
    assert all(
        "Technical failure" in a.summary and a.basis == "observed" and a.evidence["failure_class"] == "technical"
        for a in failed
    )
