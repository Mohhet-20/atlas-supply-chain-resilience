"""Alerting & event workflows API.

Alerts are raised from stored signals (and ingested events) by transparent, versioned rules; they are acknowledged, assigned, escalated and resolved by
people. Nothing here executes an operational action. Every query is limited to the caller's organisation; bodies and queries reject unknown fields, including
any organisation or tenant id. Evaluation and escalation run when invoked (or on the optional timer); no schedule is implied by this API.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.alerting import metrics as M
from app.alerting import notify as N
from app.alerting import service as S
from app.alerting.config import (
    DETECTORS,
    ENGINE_VERSION,
    EVENT_SCHEMA_VERSION,
    EVENT_TYPES,
    LIMITS,
    SEVERITIES,
    TRANSITIONS,
    UNAVAILABLE_CHANNELS,
    PolicyBody,
    RuleBody,
)
from app.core.auth import Actor, CurrentActor, Role, parse_token_table, require
from app.core.errors import ConflictError, DomainValidationError, NotFoundError, WriteDisabledError
from app.db.tenancy import note_probe, scope_session
from app.models.alerting import (
    ACTIVE_STATES,
    Alert,
    AlertEscalation,
    AlertEscalationPolicy,
    AlertEvent,
    AlertLink,
    AlertNote,
    AlertOccurrence,
    AlertRule,
    AlertRuleRun,
    AlertRuleVersion,
    AlertSuppression,
    AlertTransition,
    InboxNotification,
    NotificationAttempt,
    NotificationDelivery,
    NotificationPreference,
)
from app.models.enums import AuditOutcome
from app.models.identity import Organization, User
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse


def _tx(request: Request, actor: CurrentActor) -> Iterator[Session]:
    """One transaction per request: commit when the handler succeeds, roll back on any error.

    The session is scoped to the authenticated principal's organisation, so every query below is tenant-filtered."""
    session = request.app.state.session_factory()
    scope_session(session, actor.org_id)
    request.state.org_id = actor.org_id
    try:
        yield session
        session.commit()
    except Exception:
        probes = session.info.get("probes")
        session.rollback()
        if probes:
            _audit_probes(request, actor, probes)
        raise
    finally:
        session.close()


def _audit_probes(request: Request, actor: Actor, probes: list[dict[str, str]]) -> None:
    """Record cross-organisation access attempts (the response was an ordinary not-found). Never raises."""
    try:
        with request.app.state.session_factory() as db, db.begin():
            for p in probes[:5]:
                audit.record(
                    db,
                    actor,
                    "security.cross_tenant_access",
                    target_type=p["kind"],
                    target_id=p["id"],
                    outcome=AuditOutcome.DENIED,
                    details={"path": request.url.path, "method": request.method},
                    request_id=request_id_of(request),
                )
    except Exception:  # an audit failure must not turn a 404 into a 500
        logging.getLogger("atlas.security").exception("could not record cross-tenant probe")


DbSession = Annotated[Session, Depends(_tx)]


def _declared(dep: Any) -> set[str]:
    names = {p.alias or p.name for p in dep.query_params}
    for d in dep.dependencies:
        names |= _declared(d)
    return names


def _strict_query(request: Request) -> None:
    """Reject query parameters the route does not declare, so an organisation or tenant parameter is refused, not silently ignored."""
    route = request.scope.get("route")
    if route is None or not hasattr(route, "dependant"):
        return
    bad = sorted(set(request.query_params) - _declared(route.dependant))
    if bad:
        raise DomainValidationError(
            f"Unsupported parameter(s): {', '.join(bad)}. The organisation comes from the credential, never a parameter."
        )


router = APIRouter(prefix="/alerting", tags=["alerting and event workflows"], dependencies=[Depends(_strict_query)])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Uid = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Lim = Annotated[int, Query(ge=1, le=100)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
NOT_SUPPORTED = [
    "email, SMS or chat-app notifications (no transport is configured; Slack and Teams are reachable only through an allow-listed incoming webhook)",
    "capacity-pressure alerts (ATLAS stores no capacity-utilisation signal)",
    "business-calendar or time-zone aware SLA clocks (deadlines are wall-clock UTC)",
    "exactly-once execution (the timer and the tick are at-least-once; effects are idempotent)",
    "executing any operational action: alerts inform, people decide",
]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Alerting write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def org_directory(app: Any, org_id: int) -> dict[str, int]:
    """Who can be assigned or notified: the active users of the organisation, plus static-token actors for the default one."""
    s = app.state.settings
    out: dict[str, int] = {}
    with app.state.session_factory() as db:
        for name, role in db.execute(
            select(User.username, User.role).where(User.organization_id == org_id, User.status == "active")
        ):
            out[name] = int(Role[role.upper()])
    if org_id == 1:
        raw_tokens = s.api_tokens.get_secret_value() if s.api_tokens else ""
        table = parse_token_table(raw_tokens) if raw_tokens else {}
        out.update({a.name: int(a.role) for a in table.values()} if table else {"dev-local": int(Role.ADMIN)})
    return out


def org_webhooks(raw: str | None, hosts: set[str], org_slug: str, org_id: int) -> dict[str, str]:
    """Webhook destinations an organisation may use. Plain names belong to the default organisation; ``<slug>/<name>`` entries
    belong to that organisation and are exposed to it as ``<name>``. One organisation can never name another's destination."""
    if not hosts:
        return {}
    allp = N.parse_webhooks(raw, hosts)
    out: dict[str, str] = {}
    for name, url in allp.items():
        if "/" in name:
            slug, _, short = name.partition("/")
            if slug == org_slug:
                out[short] = url
        elif org_id == 1:
            out[name] = url
    return out


def build_ctx(app: Any, request: Request | None = None, org_id: int | None = None) -> S.Ctx:
    s = app.state.settings
    org_id = org_id if org_id is not None else getattr(getattr(request, "state", None), "org_id", 1)
    hosts = {h.strip().lower() for h in s.alerting_webhook_hosts.split(",") if h.strip()}
    raw = s.alerting_webhooks.get_secret_value() if s.alerting_webhooks else None
    with app.state.session_factory() as db:
        slug = db.scalar(select(Organization.slug).where(Organization.id == org_id)) or "default"
    return S.Ctx(
        directory=org_directory(app, org_id), provider=getattr(app.state, "alert_provider", None) or N.UrllibProvider(), webhooks=org_webhooks(raw, hosts, slug, org_id),
        allowed_hosts=hosts, max_attempts=s.alerting_max_attempts, retry_base=s.alerting_retry_base_seconds, timeout=s.alerting_webhook_timeout_seconds, retention_days=s.alerting_inbox_retention_days,
        org_id=org_id,
    )  # fmt: skip


def _ctx(request: Request) -> S.Ctx:
    return build_ctx(request.app, request)


def _iso(d: datetime | None) -> str | None:
    a = S.aware(d)
    return None if a is None else a.isoformat()


def _alert(a: Alert, rule_name: str | None = None) -> dict[str, Any]:
    return {
        "alert_uid": a.alert_uid, "title": a.title, "summary": a.summary, "source": a.source, "detector": a.detector, "basis": a.basis, "severity": a.severity, "priority": a.priority, "status": a.status,
        "entity_type": a.entity_type, "entity_id": a.entity_id, "entity_label": a.entity_label, "link": a.link, "assignee": a.assignee, "rule_uid": None, "rule_name": rule_name,
        "rule_version": a.rule_version, "last_rule_version": a.last_rule_version, "first_seen_at": _iso(a.first_seen_at), "last_seen_at": _iso(a.last_seen_at), "occurrence_count": a.occurrence_count,
        "due_ack_at": _iso(a.due_ack_at), "due_resolve_at": _iso(a.due_resolve_at), "escalation_level": a.escalation_level, "acknowledged_at": _iso(a.acknowledged_at), "acknowledged_by": a.acknowledged_by,
        "resolved_at": _iso(a.resolved_at), "resolution_mode": a.resolution_mode, "reopened_count": a.reopened_count, "version": a.version,
    }  # fmt: skip


def _rule(r: AlertRule, pol_uid: str | None = None, include_notify: bool = True) -> dict[str, Any]:
    d = S.rule_definition(r, pol_uid)
    d.update(
        {
            "rule_uid": r.rule_uid,
            "version": r.version,
            "created_by": r.created_by,
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
            "available": DETECTORS.get(r.detector, {}).get("available", False),
        }
    )
    if not include_notify:
        d["notify"] = {"channels": d["notify"].get("channels"), "min_severity": d["notify"].get("min_severity")}
    return d


def _policy(p: AlertEscalationPolicy) -> dict[str, Any]:
    return {
        "policy_uid": p.policy_uid,
        "name": p.name,
        "enabled": p.enabled,
        "ack_minutes": p.ack_minutes,
        "resolve_minutes": p.resolve_minutes,
        "escalate_after_minutes": p.escalate_after_minutes,
        "targets": p.targets,
        "version": p.version,
        "created_by": p.created_by,
    }


def _page(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _audit(
    db: DbSession, actor: Actor, request: Request, action: str, target: str, details: dict[str, Any] | None = None
) -> None:
    audit.record(
        db, actor, action, target_type="alert", target_id=target, details=details, request_id=request_id_of(request)
    )


# ---------- catalogue ----------


@router.get("/health", responses=ERR, summary="Capabilities, detector catalogue and limits")
def health(request: Request, db: DbSession, actor: Viewer) -> dict[str, Any]:
    ctx = _ctx(request)
    s = request.app.state.settings
    return {
        "engine_version": ENGINE_VERSION, "event_schema_version": EVENT_SCHEMA_VERSION, "severities": list(SEVERITIES), "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
        "detectors": [{"key": k, **{f: v[f] for f in ("label", "source", "basis", "mode", "available", "meaning", "missing")}, "unavailable_reason": v.get("unavailable_reason"), "params": {n: {kk: vv for kk, vv in p.items()} for n, p in v["params"].items()}} for k, v in DETECTORS.items()],
        "event_types": {k: {"label": v["label"], "required": v["required"], "optional": v["optional"]} for k, v in EVENT_TYPES.items()},
        "channels": {"in_app": {"available": True}, "webhook": {"available": bool(ctx.webhooks), "configured": sorted(ctx.webhooks), "reason": None if ctx.webhooks else "No webhook is configured, or no host is on the allow-list."}, **{k: {"available": False, "reason": v} for k, v in UNAVAILABLE_CHANNELS.items()}},
        "limits": LIMITS, "tick_seconds": s.alerting_tick_seconds, "retry": {"max_attempts": s.alerting_max_attempts, "base_seconds": s.alerting_retry_base_seconds}, "not_supported": NOT_SUPPORTED,
        "scope": "The caller's organisation; rules, alerts and events of other organisations are never visible.",
        "live_alerts": int(db.scalar(select(func.count()).select_from(Alert).where(Alert.status.in_(ACTIVE_STATES))) or 0),
    }  # fmt: skip


@router.get("/actors", responses=ERR, summary="Users who can be assigned alerts (editor or higher)")
def actors(request: Request, actor: Editor) -> dict[str, Any]:
    d = _ctx(request).directory
    return {"users": sorted(n for n, r in d.items() if r >= 2), "teams": ["team:editor", "team:reviewer", "team:admin"]}


# ---------- events ----------


@router.post(
    "/events", status_code=201, responses=ERR, summary="Ingest one event (idempotent by source and idempotency key)"
)
def post_event(body: S.EventBody, request: Request, response: Response, db: DbSession, actor: Editor) -> dict[str, Any]:
    _writes(request)
    ev, dup, summary = S.ingest_event(db, actor.name, body, _ctx(request))
    if dup:
        response.status_code = 200
    audit.record(
        db,
        actor,
        "alerting.event.ingested" if not dup else "alerting.event.duplicate",
        target_type="alert_event",
        target_id=ev.event_uid,
        details={"event_type": ev.event_type, "source": ev.source, "alerts_created": summary.get("alerts_created")},
        request_id=request_id_of(request),
    )
    return {
        "event_uid": ev.event_uid,
        "duplicate": dup,
        "status": ev.status,
        "occurred_at": _iso(ev.occurred_at),
        "ingested_at": _iso(ev.ingested_at),
        "warnings": ev.warnings or [],
        "processing": summary,
    }


@router.get("/events", responses=ERR, summary="Received events")
def list_events(
    db: DbSession,
    actor: Reviewer,
    limit: Lim = 20,
    offset: Off = 0,
    event_type: str | None = Query(default=None, max_length=48),
) -> dict[str, Any]:
    q = select(AlertEvent)
    if event_type:
        q = q.where(AlertEvent.event_type == event_type)
    total = int(db.scalar(select(func.count()).select_from(q.subquery())) or 0)
    rows = db.scalars(q.order_by(AlertEvent.id.desc()).limit(limit).offset(offset)).all()
    return _page(
        [
            {
                "event_uid": e.event_uid,
                "event_type": e.event_type,
                "source": e.source,
                "occurred_at": _iso(e.occurred_at),
                "ingested_at": _iso(e.ingested_at),
                "provenance": e.provenance,
                "status": e.status,
                "duplicate_count": e.duplicate_count,
                "entities": e.entities,
                "result": e.result,
                "warnings": e.warnings,
            }
            for e in rows
        ],
        total,
        limit,
        offset,
    )


# ---------- evaluation ----------


class EvalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[str] | None = Field(default=None, max_length=50)


@router.post("/evaluate", responses=ERR, summary="Evaluate enabled rules once")
def evaluate(body: EvalBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    out = S.evaluate(db, actor.name, _ctx(request), rule_uids=body.rules)
    audit.record(
        db,
        actor,
        "alerting.evaluate",
        target_type="alert_rule",
        details={"created": out["created"], "updated": out["updated"], "rules": len(out["rules"])},
        request_id=request_id_of(request),
    )
    return out


@router.post("/tick", responses=ERR, summary="Process due escalations, notification retries and expired suppressions")
def tick(request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    out = S.tick(db, actor.name, _ctx(request))
    audit.record(
        db,
        actor,
        "alerting.tick",
        target_type="alert",
        details={"escalated": out["escalated"], "unsuppressed": out["unsuppressed"]},
        request_id=request_id_of(request),
    )
    return out


# ---------- alerts ----------

SORTS = {
    "priority": (Alert.priority, Alert.last_seen_at.desc()),
    "newest": (Alert.first_seen_at.desc(),),
    "oldest": (Alert.first_seen_at,),
    "recent": (Alert.last_seen_at.desc(),),
    "severity": (Alert.severity.desc(), Alert.priority),
}


@router.get("/alerts", responses=ERR, summary="Alerts, filterable and sortable")
def list_alerts(
    request: Request, db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0,
    status: Annotated[list[str] | None, Query(max_length=7)] = None, severity: Annotated[list[str] | None, Query(max_length=5)] = None,
    source: Annotated[str | None, Query(max_length=40)] = None, basis: Annotated[str | None, Query(max_length=12)] = None, assignee: Annotated[str | None, Query(max_length=64)] = None,
    rule: Annotated[str | None, Query(max_length=40)] = None, entity_type: Annotated[str | None, Query(max_length=24)] = None, entity_id: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None, sort: Literal["priority", "newest", "oldest", "recent", "severity"] = "priority",
    live: Annotated[bool, Query(description="Only alerts that are not resolved or closed.")] = False,
) -> dict[str, Any]:  # fmt: skip
    st = select(Alert, AlertRule.name, AlertRule.rule_uid).outerjoin(AlertRule, AlertRule.id == Alert.rule_id)
    if status:
        st = st.where(Alert.status.in_(status))
    if live:
        st = st.where(Alert.status.in_(ACTIVE_STATES))
    if severity:
        st = st.where(Alert.severity.in_(severity))
    for col, v in (
        (Alert.source, source),
        (Alert.basis, basis),
        (Alert.entity_type, entity_type),
        (Alert.entity_id, entity_id),
        (AlertRule.rule_uid, rule),
    ):
        if v:
            st = st.where(col == v)
    if assignee:
        st = st.where(Alert.assignee == (actor.name if assignee == "me" else assignee))
    if q:
        like = f"%{q.strip().lower()}%"
        st = st.where(
            or_(
                func.lower(Alert.title).like(like),
                func.lower(func.coalesce(Alert.entity_label, "")).like(like),
                Alert.alert_uid == q.strip(),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(st.subquery())) or 0)
    rows = db.execute(st.order_by(*SORTS[sort], Alert.id.desc()).limit(limit).offset(offset)).all()
    items = []
    for a, name, ruid in rows:
        d = _alert(a, name)
        d["rule_uid"] = ruid
        items.append(d)
    return _page(items, total, limit, offset)


@router.get("/alerts/{alert_uid}", responses=ERR, summary="One alert with its full history")
def get_alert(alert_uid: Uid, db: DbSession, actor: Viewer) -> dict[str, Any]:
    a = S.get_alert(db, alert_uid)
    rule = db.get(AlertRule, a.rule_id) if a.rule_id else None
    d = _alert(a, rule.name if rule else None)
    d["rule_uid"] = rule.rule_uid if rule else None
    created_def = None
    if rule and a.rule_version:
        v = db.scalar(
            select(AlertRuleVersion).where(
                AlertRuleVersion.rule_id == rule.id, AlertRuleVersion.version == a.rule_version
            )
        )
        created_def = v.definition if v else None
    occ = db.scalars(
        select(AlertOccurrence).where(AlertOccurrence.alert_id == a.id).order_by(AlertOccurrence.id.desc()).limit(50)
    ).all()
    trans = db.scalars(
        select(AlertTransition).where(AlertTransition.alert_id == a.id).order_by(AlertTransition.id)
    ).all()
    notes = db.scalars(select(AlertNote).where(AlertNote.alert_id == a.id).order_by(AlertNote.id)).all()
    esc = db.scalars(select(AlertEscalation).where(AlertEscalation.alert_id == a.id).order_by(AlertEscalation.id)).all()
    links = db.execute(
        select(AlertLink, Alert)
        .join(Alert, or_(Alert.id == AlertLink.related_alert_id, Alert.id == AlertLink.alert_id))
        .where(or_(AlertLink.alert_id == a.id, AlertLink.related_alert_id == a.id), Alert.id != a.id)
    ).all()
    supp = db.get(AlertSuppression, a.suppression_id) if a.suppression_id else None
    d.update({
        "condition": a.condition, "evidence": a.evidence, "explanation": a.explanation, "escalation_policy_id": a.escalation_policy_id, "resolution_note": a.resolution_note, "notifications_throttled": a.notifications_throttled,
        "rule_definition_at_creation": created_def, "can_transition_to": list(TRANSITIONS.get(a.status, ())),
        "occurrences": [{"observed_at": _iso(o.observed_at), "value": o.value, "event_id": o.event_id, "detail": o.detail} for o in occ],
        "transitions": [{"from": t.from_state, "to": t.to_state, "actor": t.actor, "mode": t.mode, "reason": t.reason, "at": _iso(t.at)} for t in trans],
        "notes": [{"kind": n.kind, "text": n.text, "author": n.author, "at": _iso(n.created_at)} for n in notes],
        "escalations": [{"level": e.level, "to": e.to_target, "from": e.from_target, "trigger": e.trigger, "actor": e.actor, "reason": e.reason, "at": _iso(e.at)} for e in esc],
        "related": [{"alert_uid": o.alert_uid, "title": o.title, "status": o.status, "severity": o.severity, "reason": lk.reason, "linked_by": lk.created_by} for lk, o in links],
        "suppression": ({"reason": supp.reason, "expires_at": _iso(supp.expires_at)} if supp and actor.role >= Role.REVIEWER else ({"reason": "hidden: reviewers can see why this alert is suppressed", "expires_at": _iso(supp.expires_at)} if supp else None)),
    })  # fmt: skip
    if actor.role >= Role.REVIEWER:
        dels = db.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.alert_id == a.id)
            .order_by(NotificationDelivery.id)
            .limit(200)
        ).all()
        d["deliveries"] = [{"delivery_uid": x.delivery_uid, "channel": x.channel, "recipient": x.recipient, "kind": x.kind, "status": x.status, "attempts": x.attempts, "max_attempts": x.max_attempts, "next_attempt_at": _iso(x.next_attempt_at), "last_error": x.last_error, "created_at": _iso(x.created_at),
                            "attempt_log": [{"n": t.attempt_no, "at": _iso(t.at), "outcome": t.outcome, "detail": t.detail} for t in db.scalars(select(NotificationAttempt).where(NotificationAttempt.delivery_id == x.id).order_by(NotificationAttempt.attempt_no)).all()]} for x in dels]  # fmt: skip
    else:
        d["deliveries"] = None
    return d


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=600)


def _act(
    request: Request, db: DbSession, actor: Actor, alert_uid: str, to: str, body: ActionBody, action: str
) -> dict[str, Any]:
    _writes(request)
    a = S.get_alert(db, alert_uid)
    S.transition(db, a, to, actor.name, body.reason, _ctx(request), expected_version=body.expected_version)
    _audit(db, actor, request, f"alerting.alert.{action}", a.alert_uid, {"to": to, "reason": body.reason})
    db.flush()
    return {
        "alert_uid": a.alert_uid,
        "status": a.status,
        "version": a.version,
        "can_transition_to": list(TRANSITIONS.get(a.status, ())),
    }


@router.post("/alerts/{alert_uid}/acknowledge", responses=ERR, summary="Acknowledge (the condition may still exist)")
def acknowledge(alert_uid: Uid, body: ActionBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    return _act(request, db, actor, alert_uid, "acknowledged", body, "acknowledge")


@router.post("/alerts/{alert_uid}/investigate", responses=ERR, summary="Start investigating")
def investigate(alert_uid: Uid, body: ActionBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    return _act(request, db, actor, alert_uid, "investigating", body, "investigate")


@router.post("/alerts/{alert_uid}/resolve", responses=ERR, summary="Resolve (a reason is required)")
def resolve(alert_uid: Uid, body: ActionBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    return _act(request, db, actor, alert_uid, "resolved", body, "resolve")


@router.post("/alerts/{alert_uid}/close", responses=ERR, summary="Close a resolved alert (terminal)")
def close(alert_uid: Uid, body: ActionBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    return _act(request, db, actor, alert_uid, "closed", body, "close")


@router.post("/alerts/{alert_uid}/reopen", responses=ERR, summary="Reopen a resolved alert")
def reopen(alert_uid: Uid, body: ActionBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    return _act(request, db, actor, alert_uid, "open", body, "reopen")


class AssignBody(ActionBody):
    assignee: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


@router.post("/alerts/{alert_uid}/assign", responses=ERR, summary="Assign or reassign")
def assign(alert_uid: Uid, body: AssignBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    _writes(request)
    a = S.get_alert(db, alert_uid)
    S.assign(db, a, body.assignee, actor.name, body.reason, _ctx(request), expected_version=body.expected_version)
    _audit(db, actor, request, "alerting.alert.assign", a.alert_uid, {"assignee": body.assignee})
    db.flush()
    return {"alert_uid": a.alert_uid, "assignee": a.assignee, "version": a.version}


class EscalateBody(ActionBody):
    target: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


@router.post("/alerts/{alert_uid}/escalate", responses=ERR, summary="Escalate manually")
def escalate(alert_uid: Uid, body: EscalateBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    _writes(request)
    a = S.get_alert(db, alert_uid)
    if body.expected_version is not None and a.version != body.expected_version:
        raise ConflictError(f"The alert changed (now version {a.version}); reload before acting.")
    if not (body.reason and body.reason.strip()):
        raise DomainValidationError("A reason is required to escalate.")
    rec = S.escalate(db, a, actor.name, body.reason.strip(), _ctx(request), trigger="manual", target=body.target)
    if rec is None:
        raise ConflictError("This escalation level was already recorded.")
    _audit(db, actor, request, "alerting.alert.escalate", a.alert_uid, {"level": rec.level, "to": rec.to_target})
    return {
        "alert_uid": a.alert_uid,
        "status": a.status,
        "escalation_level": a.escalation_level,
        "assignee": a.assignee,
        "version": a.version,
    }


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["note", "mitigation"] = "note"
    text: str = Field(min_length=1, max_length=2000)


@router.post(
    "/alerts/{alert_uid}/notes",
    status_code=201,
    responses=ERR,
    summary="Add an investigation note or a mitigation step",
)
def add_note(alert_uid: Uid, body: NoteBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    _writes(request)
    a = S.get_alert(db, alert_uid)
    n = S.add_note(db, a, body.kind, body.text, actor.name)
    _audit(db, actor, request, "alerting.alert.note", a.alert_uid, {"kind": body.kind})
    return {"kind": n.kind, "text": n.text, "author": n.author, "at": _iso(n.created_at)}


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    related_alert_uid: str = Field(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")
    reason: str = Field(default="linked by analyst", max_length=200)


@router.post("/alerts/{alert_uid}/links", status_code=201, responses=ERR, summary="Link two related alerts")
def link(alert_uid: Uid, body: LinkBody, request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    _writes(request)
    a, o = S.get_alert(db, alert_uid), S.get_alert(db, body.related_alert_uid)
    S.link_alerts(db, a, o, body.reason, actor.name)
    _audit(db, actor, request, "alerting.alert.link", a.alert_uid, {"related": o.alert_uid})
    return {"alert_uid": a.alert_uid, "related_alert_uid": o.alert_uid}


# ---------- rules and policies ----------


@router.get("/rules", responses=ERR, summary="Alert rules")
def list_rules(db: DbSession, actor: Viewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    total = int(db.scalar(select(func.count()).select_from(AlertRule)) or 0)
    pols = {p.id: p.policy_uid for p in db.scalars(select(AlertEscalationPolicy))}
    rows = db.scalars(select(AlertRule).order_by(AlertRule.id).limit(limit).offset(offset)).all()
    counts = dict(
        db.execute(
            select(Alert.rule_id, func.count()).where(Alert.status.in_(ACTIVE_STATES)).group_by(Alert.rule_id)
        ).all()
    )
    return _page(
        [
            {
                **_rule(r, pols.get(r.escalation_policy_id), include_notify=actor.role >= Role.REVIEWER),
                "live_alerts": int(counts.get(r.id, 0)),
            }
            for r in rows
        ],
        total,
        limit,
        offset,
    )


def _get_rule(db: DbSession, rule_uid: str) -> AlertRule:
    r = db.scalar(select(AlertRule).where(AlertRule.rule_uid == rule_uid))
    if r is None:
        note_probe(db, AlertRule, AlertRule.rule_uid, rule_uid, "alert_rule")
        raise NotFoundError(f"Rule {rule_uid} was not found.")
    return r


@router.post("/rules/validate", responses=ERR, summary="Check a rule without saving it")
def validate_rule(body: RuleBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    try:
        S.check_rule(db, body, _ctx(request))
    except (DomainValidationError, ConflictError) as e:
        return {"valid": False, "problems": [p for p in e.message.split("; ") if p]}
    return {
        "valid": True,
        "problems": [],
        "detector": {k: DETECTORS[body.detector][k] for k in ("label", "basis", "meaning", "missing")},
    }


@router.post("/rules", status_code=201, responses=ERR, summary="Create a rule")
def create_rule(body: RuleBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    r = S.create_rule(db, actor.name, body, _ctx(request))
    audit.record(
        db,
        actor,
        "alerting.rule.created",
        target_type="alert_rule",
        target_id=r.rule_uid,
        details={"detector": r.detector, "severity": r.severity},
        request_id=request_id_of(request),
    )
    return _rule(r, body.escalation_policy)


class RuleUpdate(RuleBody):
    expected_version: int = Field(ge=1)


@router.put("/rules/{rule_uid}", responses=ERR, summary="Edit a rule (creates a new version)")
def update_rule(rule_uid: Uid, body: RuleUpdate, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    r = _get_rule(db, rule_uid)
    plain = RuleBody.model_validate(body.model_dump(exclude={"expected_version"}))
    S.update_rule(db, actor.name, r, plain, body.expected_version, _ctx(request))
    audit.record(
        db,
        actor,
        "alerting.rule.updated",
        target_type="alert_rule",
        target_id=r.rule_uid,
        details={"version": r.version},
        request_id=request_id_of(request),
    )
    return _rule(r, body.escalation_policy)


@router.post("/rules/{rule_uid}/{state}", responses=ERR, summary="Enable or disable a rule")
def toggle_rule(
    rule_uid: Uid, state: Literal["enable", "disable"], request: Request, db: DbSession, actor: Reviewer
) -> dict[str, Any]:
    _writes(request)
    r = _get_rule(db, rule_uid)
    want = state == "enable"
    if r.enabled != want:
        r.enabled = want
        r.version += 1
        db.flush()
        S._snapshot(db, r, actor.name)
    audit.record(
        db,
        actor,
        f"alerting.rule.{state}d",
        target_type="alert_rule",
        target_id=r.rule_uid,
        request_id=request_id_of(request),
    )
    return {"rule_uid": r.rule_uid, "enabled": r.enabled, "version": r.version}


@router.get("/rules/{rule_uid}/versions", responses=ERR, summary="Every saved version of a rule")
def rule_versions(rule_uid: Uid, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _get_rule(db, rule_uid)
    vs = db.scalars(
        select(AlertRuleVersion).where(AlertRuleVersion.rule_id == r.id).order_by(AlertRuleVersion.version.desc())
    ).all()
    return {
        "items": [
            {"version": v.version, "author": v.author, "at": _iso(v.created_at), "definition": v.definition} for v in vs
        ]
    }


@router.get(
    "/rules/{rule_uid}/runs",
    responses=ERR,
    summary="What recent evaluations of the rule did (why it did or did not match)",
)
def rule_runs(rule_uid: Uid, db: DbSession, actor: Reviewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    r = _get_rule(db, rule_uid)
    total = int(db.scalar(select(func.count()).select_from(AlertRuleRun).where(AlertRuleRun.rule_id == r.id)) or 0)
    rows = db.scalars(
        select(AlertRuleRun)
        .where(AlertRuleRun.rule_id == r.id)
        .order_by(AlertRuleRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return _page(
        [
            {
                "at": _iso(x.started_at),
                "rule_version": x.rule_version,
                "trigger": x.trigger,
                "outcome": x.outcome,
                "evaluated": x.evaluated,
                "matched": x.matched,
                "created": x.created,
                "updated": x.updated,
                "suppressed": x.suppressed,
                "truncated": x.truncated,
                "detail": x.detail,
                "duration_ms": x.duration_ms,
            }
            for x in rows
        ],
        total,
        limit,
        offset,
    )


@router.get("/policies", responses=ERR, summary="Escalation policies")
def list_policies(db: DbSession, actor: Viewer) -> dict[str, Any]:
    return {"items": [_policy(p) for p in db.scalars(select(AlertEscalationPolicy).order_by(AlertEscalationPolicy.id))]}


@router.post("/policies", status_code=201, responses=ERR, summary="Create an escalation policy")
def create_policy(body: PolicyBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    p = S.create_policy(db, actor.name, body, _ctx(request))
    audit.record(
        db,
        actor,
        "alerting.policy.created",
        target_type="alert_policy",
        target_id=p.policy_uid,
        request_id=request_id_of(request),
    )
    return _policy(p)


class PolicyUpdate(PolicyBody):
    expected_version: int = Field(ge=1)


@router.put("/policies/{policy_uid}", responses=ERR, summary="Edit a policy (affects alerts raised afterwards)")
def update_policy(
    policy_uid: Uid, body: PolicyUpdate, request: Request, db: DbSession, actor: Reviewer
) -> dict[str, Any]:
    _writes(request)
    p = db.scalar(select(AlertEscalationPolicy).where(AlertEscalationPolicy.policy_uid == policy_uid))
    if p is None:
        raise NotFoundError(f"Policy {policy_uid} was not found.")
    S.update_policy(
        db,
        p,
        PolicyBody.model_validate(body.model_dump(exclude={"expected_version"})),
        body.expected_version,
        _ctx(request),
    )
    audit.record(
        db,
        actor,
        "alerting.policy.updated",
        target_type="alert_policy",
        target_id=p.policy_uid,
        details={"version": p.version},
        request_id=request_id_of(request),
    )
    return _policy(p)


# ---------- suppressions ----------


class SuppressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_uid: str | None = Field(default=None, max_length=40)
    entity_type: str | None = Field(default=None, max_length=24)
    entity_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=3, max_length=400)
    minutes: int = Field(ge=1, le=60 * 24 * 30)


@router.get("/suppressions", responses=ERR, summary="Suppressions (active and past)")
def list_suppressions(db: DbSession, actor: Reviewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    now = S.utcnow()
    total = int(db.scalar(select(func.count()).select_from(AlertSuppression)) or 0)
    rows = db.scalars(select(AlertSuppression).order_by(AlertSuppression.id.desc()).limit(limit).offset(offset)).all()
    rules = {r.id: r.rule_uid for r in db.scalars(select(AlertRule))}
    return _page([{"id": s.id, "rule_uid": rules.get(s.rule_id), "entity_type": s.entity_type, "entity_id": s.entity_id, "reason": s.reason, "created_by": s.created_by, "starts_at": _iso(s.starts_at), "expires_at": _iso(s.expires_at), "revoked_at": _iso(s.revoked_at), "matched_count": s.matched_count,
                    "active": s.revoked_at is None and S.aware(s.starts_at) <= now < S.aware(s.expires_at)} for s in rows], total, limit, offset)  # fmt: skip


@router.post(
    "/suppressions",
    status_code=201,
    responses=ERR,
    summary="Suppress a rule and/or entity for a bounded time (never applied to CRITICAL alerts)",
)
def create_suppression(body: SuppressBody, request: Request, db: DbSession, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    rule = _get_rule(db, body.rule_uid) if body.rule_uid else None
    s = S.create_suppression(
        db,
        actor.name,
        rule=rule,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        reason=body.reason,
        minutes=body.minutes,
    )
    audit.record(
        db,
        actor,
        "alerting.suppression.created",
        target_type="alert_suppression",
        target_id=s.id,
        details={"rule": body.rule_uid, "entity": body.entity_id, "minutes": body.minutes, "reason": body.reason},
        request_id=request_id_of(request),
    )
    return {"id": s.id, "expires_at": _iso(s.expires_at)}


@router.post("/suppressions/{sid}/revoke", responses=ERR, summary="Revoke a suppression now")
def revoke_suppression(
    sid: Annotated[int, Path(ge=1)], request: Request, db: DbSession, actor: Reviewer
) -> dict[str, Any]:
    _writes(request)
    s = db.get(AlertSuppression, sid)
    if s is None:
        raise NotFoundError("Suppression not found.")
    if s.revoked_at is None:
        s.revoked_at, s.revoked_by = S.utcnow(), actor.name
    audit.record(
        db,
        actor,
        "alerting.suppression.revoked",
        target_type="alert_suppression",
        target_id=s.id,
        request_id=request_id_of(request),
    )
    return {"id": s.id, "revoked_at": _iso(s.revoked_at)}


# ---------- notifications ----------


def _inbox(n: InboxNotification, alert_uid: str) -> dict[str, Any]:
    return {
        "id": n.id,
        "alert_uid": alert_uid,
        "kind": n.kind,
        "severity": n.severity,
        "source": n.source,
        "title": n.title,
        "body": n.body,
        "created_at": _iso(n.created_at),
        "read": n.read_at is not None,
        "read_at": _iso(n.read_at),
    }


@router.get("/notifications", responses=ERR, summary="My in-app notifications")
def list_notifications(
    db: DbSession,
    actor: Viewer,
    limit: Lim = 20,
    offset: Off = 0,
    unread: bool = False,
    severity: Annotated[str | None, Query(max_length=10)] = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
) -> dict[str, Any]:
    st = (
        select(InboxNotification, Alert.alert_uid)
        .join(Alert, Alert.id == InboxNotification.alert_id)
        .where(InboxNotification.recipient == actor.name)
    )
    if unread:
        st = st.where(InboxNotification.read_at.is_(None))
    if severity:
        st = st.where(InboxNotification.severity == severity)
    if source:
        st = st.where(InboxNotification.source == source)
    total = int(db.scalar(select(func.count()).select_from(st.subquery())) or 0)
    rows = db.execute(st.order_by(InboxNotification.id.desc()).limit(limit).offset(offset)).all()
    unread_n = int(
        db.scalar(
            select(func.count())
            .select_from(InboxNotification)
            .where(InboxNotification.recipient == actor.name, InboxNotification.read_at.is_(None))
        )
        or 0
    )
    return {**_page([_inbox(n, u) for n, u in rows], total, limit, offset), "unread": unread_n}


@router.get("/notifications/count", responses=ERR, summary="My unread count")
def notification_count(db: DbSession, actor: Viewer) -> dict[str, int]:
    return {
        "unread": int(
            db.scalar(
                select(func.count())
                .select_from(InboxNotification)
                .where(InboxNotification.recipient == actor.name, InboxNotification.read_at.is_(None))
            )
            or 0
        )
    }


@router.post("/notifications/read-all", responses=ERR, summary="Mark all of MY notifications read")
def read_all(request: Request, db: DbSession, actor: Viewer) -> dict[str, int]:
    _writes(request)
    now = S.utcnow()
    rows = db.scalars(
        select(InboxNotification).where(InboxNotification.recipient == actor.name, InboxNotification.read_at.is_(None))
    ).all()
    for n in rows:
        n.read_at = now
    return {"marked": len(rows)}


@router.post("/notifications/{nid}/read", responses=ERR, summary="Mark one of MY notifications read")
def read_one(nid: Annotated[int, Path(ge=1)], request: Request, db: DbSession, actor: Viewer) -> dict[str, Any]:
    _writes(request)
    n = db.scalar(
        select(InboxNotification).where(InboxNotification.id == nid, InboxNotification.recipient == actor.name)
    )
    if n is None:
        raise NotFoundError("Notification not found.")  # someone else's notification looks identical to a missing one
    n.read_at = n.read_at or S.utcnow()
    return {"id": n.id, "read": True}


class PrefBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_app_enabled: bool = True
    min_severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    quiet_start_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_end_hour: int | None = Field(default=None, ge=0, le=23)


@router.get("/preferences", responses=ERR, summary="My notification preferences and which channels are available")
def get_prefs(request: Request, db: DbSession, actor: Viewer) -> dict[str, Any]:
    p = db.scalar(select(NotificationPreference).where(NotificationPreference.actor == actor.name))
    ctx = _ctx(request)
    return {"in_app_enabled": p.in_app_enabled if p else True, "min_severity": p.min_severity if p else "LOW", "quiet_start_hour": p.quiet_start_hour if p else None, "quiet_end_hour": p.quiet_end_hour if p else None, "timezone": "UTC",
            "channels": {"in_app": {"available": True}, "webhook": {"available": bool(ctx.webhooks), "reason": None if ctx.webhooks else "Webhooks are rule-level destinations and none is configured."}, **{k: {"available": False, "reason": v} for k, v in UNAVAILABLE_CHANNELS.items()}},
            "note": "CRITICAL alerts ignore quiet hours and minimum-severity preferences."}  # fmt: skip


@router.put("/preferences", responses=ERR, summary="Set my notification preferences")
def put_prefs(body: PrefBody, request: Request, db: DbSession, actor: Viewer) -> dict[str, Any]:
    _writes(request)
    if (body.quiet_start_hour is None) != (body.quiet_end_hour is None):
        raise DomainValidationError("Set both quiet_start_hour and quiet_end_hour, or neither.")
    p = db.scalar(select(NotificationPreference).where(NotificationPreference.actor == actor.name))
    if p is None:
        p = NotificationPreference(actor=actor.name)
        db.add(p)
    p.in_app_enabled, p.min_severity, p.quiet_start_hour, p.quiet_end_hour = (
        body.in_app_enabled,
        body.min_severity,
        body.quiet_start_hour,
        body.quiet_end_hour,
    )
    audit.record(
        db,
        actor,
        "alerting.preferences.updated",
        target_type="notification_preference",
        target_id=actor.name,
        request_id=request_id_of(request),
    )
    return body.model_dump()


# ---------- analytics and SLA ----------


@router.get("/analytics", responses=ERR, summary="Alert quality and response analytics")
def analytics(
    db: DbSession,
    actor: Viewer,
    window_days: Annotated[Literal["7", "30", "90"], Query(description="Window in days: 7, 30 or 90.")] = "30",
) -> dict[str, Any]:
    return M.analytics(db, S.utcnow(), int(window_days))


@router.get("/sla", responses=ERR, summary="Alerts approaching or past their acknowledgement or resolution target")
def sla(db: DbSession, actor: Viewer, soon_minutes: Annotated[int, Query(ge=1, le=1440)] = 60) -> dict[str, Any]:
    items = M.sla_view(db, S.utcnow(), soon_minutes)
    return {
        "items": items,
        "note": "Targets exist only for alerts raised under an escalation policy. Elapsed and remaining time are wall-clock UTC minutes; no business calendar is applied.",
        "counts": {k: sum(1 for i in items if i["state"] == k) for k in ("overdue", "due_soon", "on_track")},
    }


def run_tick_loop(app: Any, stop: threading.Event) -> None:
    """Optional in-process timer. Each organisation is processed in its own tenant-scoped transaction; a failure is logged and
    the loop continues. At-least-once; effects are idempotent."""
    interval = float(app.state.settings.alerting_tick_seconds)
    log = logging.getLogger("atlas.alerting")
    import time as _time

    beats = app.state.__dict__.setdefault("worker_beats", {})
    beats["alerting"] = _time.time()
    while not stop.wait(interval):
        beats["alerting"] = _time.time()  # heartbeat for /system/health
        try:
            with app.state.session_factory() as db:
                org_ids = list(db.scalars(select(Organization.id).where(Organization.status == "active")))
            for org_id in org_ids:
                with app.state.session_factory() as db:
                    scope_session(db, org_id)
                    S.tick(db, "system", build_ctx(app, org_id=org_id))
                    db.commit()
        except Exception:
            log.exception("alerting tick failed; will retry at the next interval")
