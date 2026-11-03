"""Decision case lifecycle: create, version, clone, validate, evaluate, replay, review and decide.

Writes touch only the four ``decision_*`` tables. Evidence runs of other modules are read, never changed. Every transition is checked against an explicit
table, recorded in the insert-only event trail with the actor and time, and approval never executes anything.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import Actor, Role
from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.decision import compare
from app.decision.config import (
    ALGORITHM_VERSION,
    METRIC_REGISTRY_VERSION,
    CaseDefinition,
    Limits,
    stable_hash,
)
from app.decision.evidence import Evidence, EvidenceProblem, extract
from app.models.decision import DecisionCase, DecisionCaseVersion, DecisionEvaluation, DecisionEvent
from app.models.scenario import Scenario
from app.models.types import utcnow

TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"READY", "EVALUATED"},
    "READY": {"DRAFT", "EVALUATED"},
    "EVALUATED": {"DRAFT", "UNDER_REVIEW", "EVALUATED"},
    "UNDER_REVIEW": {"EVALUATED", "APPROVED", "REJECTED"},
    "APPROVED": {"SUPERSEDED"},
    "REJECTED": {"SUPERSEDED"},
    "SUPERSEDED": set(),
}
EDITABLE = ("DRAFT", "READY", "EVALUATED")
USABLE_EVALUATION = ("completed", "completed_with_warnings")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")


def _move(db: Session, case: DecisionCase, to: str) -> str:
    frm = case.status
    if to not in TRANSITIONS[frm]:
        raise InvalidTransitionError(f"A case that is {frm} cannot become {to}.")
    case.status = to
    return frm


def _event(db: Session, case: DecisionCase, kind: str, actor: str, *, text: str | None = None, frm: str | None = None, to: str | None = None, evaluation_uid: str | None = None,
           alternative_key: str | None = None, data: dict[str, Any] | None = None, limits: Limits | None = None) -> DecisionEvent:  # fmt: skip
    cap = (limits or Limits()).max_events_per_case
    if (db.scalar(select(func.count()).select_from(DecisionEvent).where(DecisionEvent.case_id == case.id)) or 0) >= cap:
        raise ConflictError(f"This case already has {cap} events, the limit. Start a new case (clone) to continue.")
    e = DecisionEvent(case_id=case.id, kind=kind, from_status=frm, to_status=to, actor=actor, at=utcnow(), text=text, evaluation_uid=evaluation_uid, alternative_key=alternative_key, data=data)  # fmt: skip
    db.add(e)
    db.flush()
    return e


def get_case(db: Session, uid: str) -> DecisionCase:
    c = db.scalar(select(DecisionCase).where(DecisionCase.case_uid == uid))
    if c is None:
        raise NotFoundError("Decision case not found.")
    return c


def definition_of(db: Session, case: DecisionCase, version: int | None = None) -> tuple[dict[str, Any], int]:
    v = db.scalar(
        select(DecisionCaseVersion).where(
            DecisionCaseVersion.case_id == case.id, DecisionCaseVersion.version == (version or case.version)
        )
    )
    if v is None:
        raise NotFoundError("Case version not found.")
    return v.definition, v.version


def create(
    db: Session, d: CaseDefinition, actor: str, limits: Limits, *, cloned_from: str | None = None
) -> DecisionCase:
    if len(d.evidence) > limits.max_evidence:
        raise DomainValidationError(f"At most {limits.max_evidence} evidence runs per case.")
    if len(d.scenario_ids) > limits.max_scenario_refs:
        raise DomainValidationError(f"At most {limits.max_scenario_refs} scenarios per case.")
    case = DecisionCase(
        case_uid=f"DC-{uuid.uuid4().hex[:12].upper()}",
        title=d.title,
        status="DRAFT",
        version=1,
        created_by=actor,
        cloned_from=cloned_from,
    )
    db.add(case)
    db.flush()
    db.add(
        DecisionCaseVersion(
            case_id=case.id, version=1, definition=d.canonical(), definition_hash=d.digest(), created_by=actor
        )
    )
    _event(
        db,
        case,
        "CREATED",
        actor,
        to="DRAFT",
        text=("Cloned from " + cloned_from) if cloned_from else None,
        limits=limits,
    )
    return case


def update(
    db: Session, case: DecisionCase, d: CaseDefinition, actor: str, expected_version: int, limits: Limits
) -> DecisionCase:
    if case.status not in EDITABLE:
        raise ConflictError(
            f"A case that is {case.status} cannot be edited. "
            + ("Return it for changes first." if case.status == "UNDER_REVIEW" else "Clone it to start a new case.")
        )
    if case.version != expected_version:
        raise ConflictError(
            f"The case is now at version {case.version}, not {expected_version}; someone else changed it. Reload and re-apply your edit."
        )
    if len(d.evidence) > limits.max_evidence:
        raise DomainValidationError(f"At most {limits.max_evidence} evidence runs per case.")
    prev, _ = definition_of(db, case)
    if stable_hash(prev) == d.digest():
        raise ConflictError("The definition is unchanged; no new version was created.")
    frm = case.status
    case.version += 1
    case.title = d.title
    case.status = "DRAFT"
    case.current_evaluation_uid = (
        case.current_evaluation_uid
    )  # kept for history; it refers to an earlier version until re-evaluated
    db.add(
        DecisionCaseVersion(
            case_id=case.id,
            version=case.version,
            definition=d.canonical(),
            definition_hash=d.digest(),
            created_by=actor,
        )
    )
    db.flush()
    _event(db, case, "VERSIONED", actor, frm=frm, to="DRAFT", data={"version": case.version}, limits=limits)
    return case


def clone(db: Session, case: DecisionCase, actor: str, limits: Limits, title: str | None = None) -> DecisionCase:
    raw, _ = definition_of(db, case)
    d = CaseDefinition.model_validate({**raw, "title": title or f"Copy of {case.title}"[:200]})
    return create(db, d, actor, limits, cloned_from=case.case_uid)


def validate(db: Session, d: CaseDefinition, limits: Limits) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    ev_rows: list[dict[str, Any]] = []
    evs: list[Evidence] = []
    for ref in d.evidence:
        try:
            e = extract(db, ref, limits)
            evs.append(e)
            ev_rows.append({"kind": ref.kind, "run_uid": ref.run_uid, "role": ref.role, "plan": ref.plan, "ok": True, "plans": [p["key"] for p in e.plans], "conditions": [c["name"] for c in e.conditions if c["key"] != "NORMAL"]})  # fmt: skip
        except EvidenceProblem as p:
            issues.append({"code": p.code, "text": p.message})
            ev_rows.append(
                {
                    "kind": ref.kind,
                    "run_uid": ref.run_uid,
                    "role": ref.role,
                    "plan": ref.plan,
                    "ok": False,
                    "problem": p.message,
                }
            )
    baselines = [r for r in d.evidence if r.role == "BASELINE"]
    if len(baselines) != 1:
        issues.append(
            {
                "code": "BASELINE_REQUIRED" if not baselines else "MULTIPLE_BASELINES",
                "text": "Exactly one evidence run must have the role BASELINE (the reference plan every other plan is compared with).",
            }
        )
    if len(d.evidence) > limits.max_evidence:
        issues.append({"code": "TOO_MANY_EVIDENCE", "text": f"At most {limits.max_evidence} evidence runs per case."})
    if d.scenario_ids:
        found = set(db.scalars(select(Scenario.id).where(Scenario.id.in_(d.scenario_ids))))
        for i in d.scenario_ids:
            if i not in found:
                issues.append({"code": "SCENARIO_NOT_FOUND", "text": f"Scenario {i} does not exist."})
    if evs and not issues:
        snap = compare.assemble(evs, d.canonical())
        issues += snap["issues"]
    return {"ok": not issues, "issues": issues, "evidence": ev_rows}


def mark_ready(db: Session, case: DecisionCase, actor: str, limits: Limits) -> dict[str, Any]:
    if case.status not in ("DRAFT", "READY"):
        raise InvalidTransitionError(f"A case that is {case.status} cannot be marked ready.")
    raw, _ = definition_of(db, case)
    rep = validate(db, CaseDefinition.model_validate(raw), limits)
    if rep["ok"] and case.status == "DRAFT":
        frm = _move(db, case, "READY")
        _event(db, case, "VALIDATED", actor, frm=frm, to="READY", limits=limits)
    return rep


def evaluate(db: Session, case: DecisionCase, actor: str, limits: Limits) -> tuple[DecisionEvaluation, bool]:
    if case.status not in EDITABLE:
        raise InvalidTransitionError(
            f"A case that is {case.status} cannot be (re)evaluated."
            + (" Return it for changes first." if case.status == "UNDER_REVIEW" else "")
        )
    raw, version = definition_of(db, case)
    d = CaseDefinition.model_validate(raw)
    rep = validate(db, d, limits)
    if not rep["ok"]:
        raise DomainValidationError("The case cannot be evaluated: " + " ".join(i["text"] for i in rep["issues"]))
    n = (
        db.scalar(select(func.count()).select_from(DecisionEvaluation).where(DecisionEvaluation.case_id == case.id))
        or 0
    )
    t0 = time.monotonic()
    evs = [extract(db, r, limits) for r in d.evidence]
    snap = compare.assemble(evs, d.canonical())
    snap_hash = stable_hash(snap)
    prior = db.scalar(
        select(DecisionEvaluation).where(
            DecisionEvaluation.case_id == case.id,
            DecisionEvaluation.case_version == version,
            DecisionEvaluation.snapshot_hash == snap_hash,
        )
    )
    if prior is not None:
        return prior, True
    if n >= limits.max_evaluations_per_case:
        raise ConflictError(f"This case already has {n} evaluations, the limit. Clone it to continue.")
    res = compare.evaluate(json.loads(json.dumps(snap)))
    ev = DecisionEvaluation(
        evaluation_uid=f"DE-{uuid.uuid4().hex[:12].upper()}", case_id=case.id, case_version=version, result_status=res["result_status"], algorithm_version=ALGORITHM_VERSION,
        metric_registry_version=METRIC_REGISTRY_VERSION, snapshot=snap, snapshot_hash=snap_hash, result=res, result_hash=res["result_hash"], warnings=res["warnings"], evaluated_by=actor,
        evaluated_at=utcnow(), duration_ms=int((time.monotonic() - t0) * 1000),
    )  # fmt: skip
    db.add(ev)
    db.flush()
    frm = case.status
    if res["result_status"] in USABLE_EVALUATION:
        if "EVALUATED" not in TRANSITIONS[frm]:
            raise InvalidTransitionError(f"A case that is {frm} cannot be evaluated.")
        case.status, case.current_evaluation_uid = "EVALUATED", ev.evaluation_uid
    _event(db, case, "EVALUATED", actor, frm=frm, to=case.status, evaluation_uid=ev.evaluation_uid, data={"result_status": res["result_status"], "result_hash": res["result_hash"], "case_version": version}, limits=limits)  # fmt: skip
    return ev, False


def get_evaluation(db: Session, case: DecisionCase, uid: str) -> DecisionEvaluation:
    e = db.scalar(
        select(DecisionEvaluation).where(
            DecisionEvaluation.evaluation_uid == uid, DecisionEvaluation.case_id == case.id
        )
    )
    if e is None:
        raise NotFoundError("Evaluation not found for this case.")
    return e


def replay(db: Session, ev: DecisionEvaluation, case: DecisionCase, limits: Limits) -> dict[str, Any]:
    again = compare.evaluate(json.loads(json.dumps(ev.snapshot)))
    live: dict[str, Any]
    try:
        raw, _ = definition_of(db, case, ev.case_version)
        d = CaseDefinition.model_validate(raw)
        evs = [extract(db, r, limits) for r in d.evidence]
        live_hash = stable_hash(compare.assemble(evs, d.canonical()))
        live = {"live_evidence_unchanged": live_hash == ev.snapshot_hash}
    except EvidenceProblem as p:
        live = {"live_evidence_unchanged": False, "live_note": p.message}
    return {"evaluation_uid": ev.evaluation_uid, "reproduced_from_stored_snapshot": again["result_hash"] == ev.result_hash, "stored_result_hash": ev.result_hash, "replayed_result_hash": again["result_hash"],
            **live, "explanation": "The stored snapshot reproduces the stored result exactly." if again["result_hash"] == ev.result_hash else "The stored snapshot did not reproduce the stored result; the algorithm changed since."}  # fmt: skip


def assign_reviewer(db: Session, case: DecisionCase, reviewer: str, actor: Actor, limits: Limits) -> DecisionCase:
    if not NAME_RE.match(reviewer):
        raise DomainValidationError("The reviewer must be a user name (letters, digits, . _ -).")
    if case.status in ("APPROVED", "REJECTED", "SUPERSEDED"):
        raise ConflictError(f"A case that is {case.status} cannot be given a new reviewer.")
    if reviewer == case.created_by and actor.role < Role.ADMIN:
        raise ConflictError("The person who created a case cannot be its reviewer; choose someone else.")
    case.reviewer = reviewer
    db.flush()
    _event(db, case, "ASSIGNED", actor.name, text=f"Reviewer: {reviewer}", data={"reviewer": reviewer}, limits=limits)
    return case


def note(
    db: Session,
    case: DecisionCase,
    kind: str,
    text: str,
    actor: str,
    limits: Limits,
    alternative_key: str | None = None,
) -> DecisionEvent:
    if kind not in ("COMMENT", "CONCERN", "ASSUMPTION"):
        raise DomainValidationError("kind must be COMMENT, CONCERN or ASSUMPTION.")
    t = text.strip()
    if not 3 <= len(t) <= 2000:
        raise DomainValidationError("A note must be 3-2000 characters.")
    return _event(
        db,
        case,
        kind,
        actor,
        text=t,
        alternative_key=alternative_key,
        evaluation_uid=case.current_evaluation_uid,
        limits=limits,
    )


def submit(db: Session, case: DecisionCase, actor: str, limits: Limits) -> DecisionCase:
    if case.status != "EVALUATED":
        raise InvalidTransitionError(f"Only an evaluated case can be submitted for review; this one is {case.status}.")
    if not case.reviewer:
        raise ConflictError("Assign a reviewer before submitting for review.")
    ev = db.scalar(select(DecisionEvaluation).where(DecisionEvaluation.evaluation_uid == case.current_evaluation_uid))
    if ev is None or ev.case_version != case.version or ev.result_status not in USABLE_EVALUATION:
        raise ConflictError("The case has no completed evaluation of its current version; evaluate it first.")
    frm = _move(db, case, "UNDER_REVIEW")
    db.flush()
    _event(db, case, "SUBMITTED", actor, frm=frm, to="UNDER_REVIEW", evaluation_uid=ev.evaluation_uid, limits=limits)
    return case


def _may_decide(case: DecisionCase, actor: Actor) -> None:
    if actor.role < Role.ADMIN and actor.name != case.reviewer:
        raise ConflictError(f"Only the assigned reviewer ({case.reviewer}) or an administrator can decide this case.")


def return_for_changes(db: Session, case: DecisionCase, text: str, actor: Actor, limits: Limits) -> DecisionCase:
    if case.status != "UNDER_REVIEW":
        raise InvalidTransitionError(f"Only a case under review can be returned; this one is {case.status}.")
    _may_decide(case, actor)
    if len(text.strip()) < 10:
        raise DomainValidationError("Say what needs to change (at least 10 characters).")
    frm = _move(db, case, "EVALUATED")
    db.flush()
    _event(db, case, "RETURNED", actor.name, text=text.strip(), frm=frm, to="EVALUATED", limits=limits)
    return case


def decide(
    db: Session,
    case: DecisionCase,
    decision: str,
    rationale: str,
    alternative_key: str | None,
    actor: Actor,
    limits: Limits,
) -> DecisionCase:
    if case.status != "UNDER_REVIEW":
        raise InvalidTransitionError(f"Only a case under review can be decided; this one is {case.status}.")
    _may_decide(case, actor)
    if case.created_by == actor.name and actor.role < Role.ADMIN:
        raise ConflictError("The person who created a case cannot decide it; another reviewer must.")
    if len(rationale.strip()) < 10:
        raise DomainValidationError("A rationale of at least 10 characters is required.")
    ev = db.scalar(select(DecisionEvaluation).where(DecisionEvaluation.evaluation_uid == case.current_evaluation_uid))
    if ev is None or ev.result_status not in USABLE_EVALUATION or ev.case_version != case.version:
        raise ConflictError("The case has no completed evaluation of its current version.")
    keys = {a["key"] for a in ev.result["alternatives"]}
    if decision == "APPROVED":
        if alternative_key is None or alternative_key not in keys:
            raise DomainValidationError(
                "Name one of the evaluated alternatives (alternative_key) to approve; MAINTAIN_BASELINE is allowed."
            )
    elif alternative_key is not None and alternative_key not in keys:
        raise DomainValidationError("Unknown alternative_key for this evaluation.")
    frm = _move(db, case, decision)
    case.selected_alternative = alternative_key if decision == "APPROVED" else None
    db.flush()
    _event(db, case, decision, actor.name, text=rationale.strip(), frm=frm, to=decision, evaluation_uid=ev.evaluation_uid, alternative_key=alternative_key, data={"result_hash": ev.result_hash, "executed": False}, limits=limits)  # fmt: skip
    return case


def supersede(
    db: Session, case: DecisionCase, text: str, superseded_by: str | None, actor: str, limits: Limits
) -> DecisionCase:
    if case.status not in ("APPROVED", "REJECTED"):
        raise InvalidTransitionError(f"Only an approved or rejected case can be superseded; this one is {case.status}.")
    if len(text.strip()) < 10:
        raise DomainValidationError("Say why it is superseded (at least 10 characters).")
    if superseded_by is not None:
        if superseded_by == case.case_uid:
            raise DomainValidationError("A case cannot supersede itself.")
        get_case(db, superseded_by)
    frm = _move(db, case, "SUPERSEDED")
    case.superseded_by = superseded_by
    db.flush()
    _event(
        db,
        case,
        "SUPERSEDED",
        actor,
        text=text.strip(),
        frm=frm,
        to="SUPERSEDED",
        data={"superseded_by": superseded_by},
        limits=limits,
    )
    return case


def approval_state(db: Session, case: DecisionCase) -> dict[str, Any]:
    last = db.scalar(
        select(DecisionEvent)
        .where(DecisionEvent.case_id == case.id, DecisionEvent.kind.in_(("APPROVED", "REJECTED")))
        .order_by(DecisionEvent.id.desc())
        .limit(1)
    )
    return {"state": case.status, "reviewer": case.reviewer, "decided_by": last.actor if last else None, "decided_at": last.at.isoformat() if last else None, "rationale": last.text if last else None,
            "selected_alternative": case.selected_alternative, "executed": False}  # fmt: skip
