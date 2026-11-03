"""Human review workflow for duplicate candidates (state machine, comparison view, decisions)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.auth import Actor
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.models.enums import CandidateStatus, CandidateTier, RegistryType
from app.models.registry import CandidateReview, DuplicateCandidate
from app.registry import audit, service, views
from app.registry.entities import MODELS, Entity, code_of
from app.registry.normalize import normalize_address, normalize_name

S = CandidateStatus
TRANSITIONS: dict[CandidateStatus, set[CandidateStatus]] = {
    S.NEW: {S.UNDER_REVIEW, S.CONFIRMED_DUPLICATE, S.REJECTED, S.DEFERRED},
    S.UNDER_REVIEW: {S.CONFIRMED_DUPLICATE, S.REJECTED, S.DEFERRED},
    S.DEFERRED: {S.UNDER_REVIEW, S.CONFIRMED_DUPLICATE, S.REJECTED},
    S.REOPENED: {S.UNDER_REVIEW, S.CONFIRMED_DUPLICATE, S.REJECTED, S.DEFERRED},
    S.REJECTED: {S.REOPENED},
    S.CONFIRMED_DUPLICATE: {S.REOPENED},
    S.MERGED: set(),
}
OPEN_STATUSES = (S.NEW, S.UNDER_REVIEW, S.DEFERRED, S.REOPENED)


def get_candidate(session: Session, candidate_id: int) -> DuplicateCandidate:
    cand = session.get(DuplicateCandidate, candidate_id)
    if cand is None:
        raise NotFoundError(f"Candidate {candidate_id} was not found.")
    return cand


def _entities(session: Session, cand: DuplicateCandidate) -> tuple[Entity | None, Entity | None]:
    model = MODELS[cand.entity_type]
    return session.get(model, cand.left_id), session.get(model, cand.right_id)


def candidate_out(session: Session, cand: DuplicateCandidate, names: dict | None = None) -> dict[str, Any]:
    left, right = _entities(session, cand) if names is None else (None, None)
    ln = names.get((cand.entity_type, cand.left_id)) if names else (left.name if left else None)
    rn = names.get((cand.entity_type, cand.right_id)) if names else (right.name if right else None)
    stale = False
    if names is None and left and right:
        stale = (
            left.version != cand.left_version
            or right.version != cand.right_version
            or left.merged_into_id is not None
            or right.merged_into_id is not None
        )
    return {
        "id": cand.id,
        "entity_type": cand.entity_type.value,
        "left_id": cand.left_id,
        "right_id": cand.right_id,
        "left_name": ln,
        "right_name": rn,
        "rule_version": cand.rule_version,
        "score": cand.score,
        "tier": cand.tier.value,
        "hard_conflict": cand.hard_conflict,
        "status": cand.status.value,
        "evidence": cand.evidence,
        "left_version": cand.left_version,
        "right_version": cand.right_version,
        "stale": stale,
        "first_seen_at": cand.first_seen_at,
        "updated_at": cand.updated_at,
    }


def list_candidates(
    session: Session,
    *,
    entity_type: RegistryType | None,
    status: CandidateStatus | None,
    tiers: list[CandidateTier],
    min_score: float | None,
    entity_id: int | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    stmt = select(DuplicateCandidate)
    if entity_type:
        stmt = stmt.where(DuplicateCandidate.entity_type == entity_type)
    if status:
        stmt = stmt.where(DuplicateCandidate.status == status)
    if tiers:
        stmt = stmt.where(DuplicateCandidate.tier.in_(tiers))
    if min_score is not None:
        stmt = stmt.where(DuplicateCandidate.score >= min_score)
    if entity_id is not None:
        stmt = stmt.where(or_(DuplicateCandidate.left_id == entity_id, DuplicateCandidate.right_id == entity_id))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(DuplicateCandidate.score.desc(), DuplicateCandidate.id).limit(limit).offset(offset)
    ).all()
    names: dict[tuple[RegistryType, int], str] = {}
    for rtype in {c.entity_type for c in rows}:  # one query per type instead of one per row
        ids = {i for c in rows if c.entity_type == rtype for i in (c.left_id, c.right_id)}
        model = MODELS[rtype]
        for eid, name in session.execute(select(model.id, model.name).where(model.id.in_(ids))):
            names[(rtype, eid)] = name
    return {
        "items": [candidate_out(session, c, names) for c in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def decide(
    session: Session,
    cand: DuplicateCandidate,
    decision: CandidateStatus,
    actor: Actor,
    *,
    reason: str | None,
    acknowledge_conflict: bool,
    request_id: str | None,
) -> DuplicateCandidate:
    if decision in (S.NEW, S.MERGED):
        raise DomainValidationError(f"'{decision.value}' cannot be set through a review decision.")
    allowed = TRANSITIONS[cand.status]
    if decision not in allowed:
        raise ConflictError(
            f"Cannot move a candidate from {cand.status.value} to {decision.value}. "
            f"Allowed: {', '.join(sorted(s.value for s in allowed)) or 'none'}."
        )
    left, right = _entities(session, cand)
    if decision is S.CONFIRMED_DUPLICATE:
        if left is None or right is None or left.merged_into_id is not None or right.merged_into_id is not None:
            raise ConflictError("Both records must still exist as live records to confirm a duplicate.")
        if cand.hard_conflict:
            if not acknowledge_conflict or not (reason and reason.strip()):
                raise DomainValidationError(
                    "These records carry conflicting authoritative identifiers. Confirming requires "
                    "acknowledge_identifier_conflict=true and a reason."
                )
    if decision in (S.REJECTED, S.REOPENED) and cand.status is S.CONFIRMED_DUPLICATE and decision is S.REJECTED:
        raise ConflictError("A confirmed duplicate must be reopened before it can be rejected.")
    previous = cand.status
    cand.status = decision
    session.add(
        CandidateReview(
            candidate_id=cand.id,
            from_status=previous,
            to_status=decision,
            actor=actor.name,
            authenticated=actor.authenticated,
            reason=(reason or None),
            evidence_snapshot={
                "score": cand.score,
                "tier": cand.tier.value,
                "rule_version": cand.rule_version,
                "hard_conflict": cand.hard_conflict,
                "evidence": cand.evidence,
                "left_version": cand.left_version,
                "right_version": cand.right_version,
            },
        )
    )
    session.flush()
    audit.record(
        session,
        actor,
        "candidate.decision",
        target_type="CANDIDATE",
        target_id=cand.id,
        details={
            "entity_type": cand.entity_type.value,
            "left_id": cand.left_id,
            "right_id": cand.right_id,
            "from": previous.value,
            "to": decision.value,
            "reason": reason,
            "conflict_acknowledged": acknowledge_conflict,
        },
        request_id=request_id,
    )
    return cand


def history(session: Session, candidate_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(CandidateReview).where(CandidateReview.candidate_id == candidate_id).order_by(CandidateReview.id)
    ).all()
    return [
        {
            "id": r.id,
            "from_status": r.from_status.value if r.from_status else None,
            "to_status": r.to_status.value,
            "actor": r.actor,
            "authenticated": r.authenticated,
            "reason": r.reason,
            "created_at": r.created_at,
            "evidence_snapshot": r.evidence_snapshot,
        }
        for r in rows
    ]


COMPARE_FIELDS: dict[RegistryType, list[str]] = {
    RegistryType.SUPPLIER: [
        "name", "legal_name", "country_or_region", "category", "status", "address", "parent_supplier_id",
    ],
    RegistryType.FACILITY: [
        "name", "facility_type", "country_or_region", "city", "address", "latitude", "longitude",
        "location_status", "capacity_value", "capacity_unit", "owner_supplier_id", "status",
    ],
    RegistryType.PRODUCT: ["name", "category", "unit_of_measure", "description", "manufacturer_supplier_id", "status"],
}  # fmt: skip
NORMALIZED = {"name": normalize_name, "legal_name": normalize_name, "address": normalize_address}


def comparison(session: Session, cand: DuplicateCandidate) -> dict[str, Any]:
    """Side-by-side view with original and normalized values, conflicts, identifiers, provenance, relationships."""
    left, right = _entities(session, cand)
    if left is None or right is None:
        raise NotFoundError("One of the candidate records no longer exists.")
    rows = []
    for fname in COMPARE_FIELDS[cand.entity_type]:
        lv, rv = getattr(left, fname), getattr(right, fname)
        lv, rv = (getattr(v, "value", v) for v in (lv, rv))
        norm = NORMALIZED.get(fname)
        both = lv not in (None, "") and rv not in (None, "")
        rows.append(
            {
                "field": fname,
                "left": lv,
                "right": rv,
                "left_normalized": norm(lv) if norm and isinstance(lv, str) else None,
                "right_normalized": norm(rv) if norm and isinstance(rv, str) else None,
                "equal": both and lv == rv,
                "conflict": both and (norm(lv) != norm(rv) if norm else lv != rv),
                "missing_side": ("left" if lv in (None, "") else "right")
                if (lv in (None, "")) != (rv in (None, ""))
                else None,
            }
        )

    def side(e: Entity) -> dict[str, Any]:
        return {
            "id": e.id,
            "code": code_of(e),
            "version": e.version,
            "lifecycle": views.lifecycle_of(e),
            "provenance": e.provenance.value,
            "source_namespace": e.source_namespace,
            "external_id": e.external_id,
            "created_at": e.created_at,
            "identifiers": service.list_identifiers(session, cand.entity_type, e),
            "aliases": [a.alias for a in service.list_aliases(session, cand.entity_type, e.id)],
            "relationships": views.relationships_of(session, cand.entity_type, e.id),
            "lineage_count": len(views.lineage_for(session, cand.entity_type, e.id)),
        }

    return {
        "candidate": candidate_out(session, cand),
        "fields": rows,
        "left": side(left),
        "right": side(right),
        "history": history(session, cand.id),
    }
