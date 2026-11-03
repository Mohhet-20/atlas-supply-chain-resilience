"""Analyst review actions. Reviews are append-only and never change the automated assessment.

An override records WHO classified the supplier differently, WHY, and what the automated result was; the automated
score and classification remain on the assessment and are displayed next to the override.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor, Role
from app.core.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError
from app.models.enums import ReviewAction, RiskCategory, RiskClass
from app.models.risk import RiskAssessment, RiskReview
from app.models.types import utcnow
from app.risk import service

MIN_OVERRIDE_NOTE = 20
MAX_OVERRIDE_DAYS = 365
ROLE_FOR = {
    ReviewAction.ACKNOWLEDGE: Role.EDITOR, ReviewAction.NOTE: Role.EDITOR, ReviewAction.REQUEST_EVIDENCE: Role.EDITOR,
    ReviewAction.FOLLOW_UP: Role.EDITOR, ReviewAction.OVERRIDE: Role.REVIEWER, ReviewAction.REVOKE_OVERRIDE: Role.REVIEWER,
}  # fmt: skip


def add_review(session: Session, actor: Actor, supplier_id: int, *, assessment_id: int, action: ReviewAction, note: str | None,
               factor_category: str | None, override_classification: RiskClass | None, expires_at: datetime | None,
               target_review_id: int | None, request_id: str | None, now: datetime | None = None) -> RiskReview:  # fmt: skip
    """Validate and insert one review. Caller supplies the transaction (so the audit event commits with it)."""
    now = now or utcnow()
    if actor.role < ROLE_FOR[action]:
        raise ForbiddenError(f"{action.value} requires the {ROLE_FOR[action].name.lower()} role.")
    service.get_supplier(session, supplier_id)
    cur = service.current_assessment(session, supplier_id)
    if cur is None:
        raise ConflictError("The supplier has no assessment to review. Run an assessment first.")
    if assessment_id != cur.id:
        raise ConflictError("That assessment was superseded by a newer one; review the current assessment.")
    note = (note or "").strip() or None
    category = None
    if factor_category is not None:
        try:
            category = RiskCategory(factor_category).value
        except ValueError:
            raise DomainValidationError("Unknown risk category.") from None
    if action in (ReviewAction.NOTE, ReviewAction.REQUEST_EVIDENCE, ReviewAction.FOLLOW_UP) and (
        note is None or len(note) < 3
    ):
        raise DomainValidationError(f"{action.value} needs a note.")
    if action is ReviewAction.FOLLOW_UP and category is None:
        raise DomainValidationError("FOLLOW_UP must name the risk category to follow up.")
    if action is not ReviewAction.OVERRIDE and (override_classification is not None or expires_at is not None):
        raise DomainValidationError("Only an OVERRIDE carries a classification or expiry.")
    if action is not ReviewAction.REVOKE_OVERRIDE and target_review_id is not None:
        raise DomainValidationError("Only REVOKE_OVERRIDE names a target review.")
    if action is ReviewAction.OVERRIDE:
        if override_classification is None:
            raise DomainValidationError("An override needs the classification being asserted.")
        if note is None or len(note) < MIN_OVERRIDE_NOTE:
            raise DomainValidationError(
                f"An override needs a justification of at least {MIN_OVERRIDE_NOTE} characters."
            )
        if expires_at is not None:
            if expires_at.tzinfo is None:
                raise DomainValidationError("The override expiry must include a timezone.")
            if expires_at <= now or expires_at > now + timedelta(days=MAX_OVERRIDE_DAYS):
                raise DomainValidationError(
                    f"The override expiry must be in the future and within {MAX_OVERRIDE_DAYS} days."
                )
    if action is ReviewAction.REVOKE_OVERRIDE:
        if target_review_id is None:
            raise DomainValidationError("REVOKE_OVERRIDE needs the override's review id.")
        target = session.get(RiskReview, target_review_id)
        if target is None or target.supplier_id != supplier_id or target.action is not ReviewAction.OVERRIDE:
            raise NotFoundError("Override not found for this supplier.")
        if session.scalar(
            select(RiskReview.id).where(
                RiskReview.action == ReviewAction.REVOKE_OVERRIDE, RiskReview.target_review_id == target_review_id
            )
        ):
            raise ConflictError("That override was already revoked.")
        if note is None or len(note) < 3:
            raise DomainValidationError("Revoking an override needs a note.")
    review = RiskReview(
        supplier_id=supplier_id, assessment_id=cur.id, action=action, factor_category=category, actor=actor.name, actor_role=actor.role.name,
        created_at=now, note=note[:1000] if note else None, override_classification=override_classification,
        override_expires_at=expires_at, target_review_id=target_review_id, original_status=cur.status.value,
        original_classification=cur.classification.value if cur.classification else None, original_score=cur.risk_score, request_id=request_id,
    )  # fmt: skip
    session.add(review)
    session.flush()
    _refresh_state(session, cur, now)
    return review


def _refresh_state(session: Session, cur: RiskAssessment, now: datetime) -> None:
    ov = service.active_override(session, cur.supplier_id, now, cur.id)
    if ov and ov["effective"]:
        cur.review_state = "OVERRIDDEN"
        return
    acked = session.scalar(
        select(RiskReview.id)
        .where(RiskReview.assessment_id == cur.id, RiskReview.action == ReviewAction.ACKNOWLEDGE)
        .limit(1)
    )
    cur.review_state = "ACKNOWLEDGED" if acked else "UNREVIEWED"
