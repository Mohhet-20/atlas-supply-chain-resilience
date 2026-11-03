"""Append-only history of network relationship changes (polymorphic reference to the two edge tables)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.types import UTCDateTime, utcnow


class GraphEdgeEvent(TenantOwned, Base):
    """One immutable change to a relationship.

    ``edge_kind`` is SUPPLY (``supply_relationships``) or PRODUCT (``product_links``). There is deliberately no foreign
    key: a duplicate relationship removed by a registry merge must keep its history. Rows cannot be updated or deleted
    (SQLite triggers; see migration 0004).
    """

    __tablename__ = "graph_edge_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    edge_kind: Mapped[str] = mapped_column(String(16))
    edge_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(500))
    changes: Mapped[Any] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("edge_kind IN ('SUPPLY','PRODUCT')", name="edge_kind"),
        Index("ix_graph_edge_events_edge", "edge_kind", "edge_id"),
        Index("ix_graph_edge_events_occurred", "occurred_at"),
    )
