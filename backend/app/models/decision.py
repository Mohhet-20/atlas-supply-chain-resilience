"""Decision support storage: decision cases, their insert-only versions, immutable evaluations and the insert-only event trail.

A case is a *review container*. It never copies or replaces a scenario engine: it references finished runs from earlier modules, freezes
the metric values it read from them in an evaluation snapshot, and records who reviewed what. Nothing here is executed operationally.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.types import UTCDateTime, utcnow

CASE_STATUSES = ("DRAFT", "READY", "EVALUATED", "UNDER_REVIEW", "APPROVED", "REJECTED", "SUPERSEDED")
EVENT_KINDS = ("CREATED", "VERSIONED", "VALIDATED", "EVALUATED", "ASSIGNED", "COMMENT", "CONCERN", "ASSUMPTION", "SUBMITTED", "RETURNED", "APPROVED", "REJECTED", "SUPERSEDED")  # fmt: skip


class DecisionCase(TenantOwned, Base):
    __tablename__ = "decision_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_uid: Mapped[str] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # latest definition version
    created_by: Mapped[str] = mapped_column(String(64))
    reviewer: Mapped[str | None] = mapped_column(String(64))
    cloned_from: Mapped[str | None] = mapped_column(String(40))
    superseded_by: Mapped[str | None] = mapped_column(String(40))
    current_evaluation_uid: Mapped[str | None] = mapped_column(String(40))
    selected_alternative: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_decision_cases_version"),
        CheckConstraint(f"status in {CASE_STATUSES}", name="ck_decision_cases_status"),
        Index("ix_decision_cases_status", "status", "id"),
    )


class DecisionCaseVersion(TenantOwned, Base):
    """Insert-only: the full definition at each version, so an old evaluation can always be read against what was assumed."""

    __tablename__ = "decision_case_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("decision_cases.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[Any] = mapped_column(JSON)
    definition_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_decision_case_versions"),)


class DecisionEvaluation(TenantOwned, Base):
    """Insert-only: the evidence values read from finished runs (snapshot), and everything derived from them."""

    __tablename__ = "decision_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_uid: Mapped[str] = mapped_column(String(40), unique=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("decision_cases.id", ondelete="RESTRICT"))
    case_version: Mapped[int] = mapped_column(Integer)
    result_status: Mapped[str] = mapped_column(
        String(32)
    )  # completed | completed_with_warnings | insufficient_data | invalid_input
    algorithm_version: Mapped[str] = mapped_column(String(16))
    metric_registry_version: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[Any] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[Any] = mapped_column(JSON)
    result_hash: Mapped[str] = mapped_column(String(64))
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    evaluated_by: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_decision_evaluations_case", "case_id", "id"),)


class DecisionEvent(TenantOwned, Base):
    """Insert-only audit trail of a case: transitions, assignments, comments, concerns, assumptions and the final decision."""

    __tablename__ = "decision_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("decision_cases.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    text: Mapped[str | None] = mapped_column(Text)
    evaluation_uid: Mapped[str | None] = mapped_column(String(40))
    alternative_key: Mapped[str | None] = mapped_column(String(80))
    data: Mapped[Any] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(f"kind in {EVENT_KINDS}", name="ck_decision_events_kind"),
        Index("ix_decision_events_case", "case_id", "id"),
    )
