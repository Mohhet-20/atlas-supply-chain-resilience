from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantOwned
from app.models.enums import EdgeCriticality, EdgeStatus, Provenance, RelationshipType
from app.models.facility import Facility
from app.models.source import DataSource  # noqa: F401
from app.models.supplier import Supplier
from app.models.types import UTCDateTime, enum_column, utcnow


class SupplyRelationship(TenantOwned, Base):
    """Typed dependency edge.

    SUPPLIES:      supplier_id -> target_facility_id
    TRANSFERS_TO:  source_facility_id -> target_facility_id

    Endpoints are real foreign keys (no polymorphic ids), and CHECK constraints reject any
    other endpoint combination, so arbitrary invalid edges cannot be stored.
    """

    __tablename__ = "supply_relationships"

    id: Mapped[int] = mapped_column(primary_key=True)
    relationship_code: Mapped[str] = mapped_column(String(128))
    relationship_type: Mapped[RelationshipType] = mapped_column(enum_column(RelationshipType, "relationship_type"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    source_facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    target_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    capacity_value: Mapped[float | None] = mapped_column(Float)
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "relationship_provenance"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Network-graph attributes. Optional values stay NULL when unknown (never defaulted to 0).
    status: Mapped[EdgeStatus] = mapped_column(
        enum_column(EdgeStatus, "supply_relationship_status"), default=EdgeStatus.CONFIRMED, server_default="CONFIRMED"
    )
    status_reason: Mapped[str | None] = mapped_column(String(500))
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    confidence: Mapped[float | None] = mapped_column(Float)
    criticality: Mapped[EdgeCriticality | None] = mapped_column(
        enum_column(EdgeCriticality, "supply_relationship_criticality")
    )
    supply_share_pct: Mapped[float | None] = mapped_column(Float)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}

    source: Mapped[DataSource | None] = relationship("DataSource", lazy="joined")

    @property
    def source_namespace(self) -> str | None:
        return self.source.namespace if self.source is not None else None

    supplier: Mapped[Supplier | None] = relationship(foreign_keys=[supplier_id])
    source_facility: Mapped[Facility | None] = relationship(foreign_keys=[source_facility_id])
    target_facility: Mapped[Facility] = relationship(foreign_keys=[target_facility_id])

    __table_args__ = (
        UniqueConstraint("organization_id", "relationship_code", name="uq_supply_relationships_org_relationship_code"),
        UniqueConstraint("source_id", "external_id", name="uq_supply_relationships_source_external"),
        CheckConstraint("(source_id IS NULL) = (external_id IS NULL)", name="source_pair"),
        CheckConstraint(
            "(relationship_type = 'SUPPLIES' AND supplier_id IS NOT NULL AND source_facility_id IS NULL) OR "
            "(relationship_type = 'TRANSFERS_TO' AND source_facility_id IS NOT NULL AND supplier_id IS NULL "
            "AND source_facility_id <> target_facility_id)",
            name="valid_endpoints",
        ),
        CheckConstraint(
            "(capacity_value IS NULL AND capacity_unit IS NULL) OR "
            "(capacity_value IS NOT NULL AND capacity_unit IS NOT NULL AND capacity_value >= 0 "
            "AND length(trim(capacity_unit)) > 0)",
            name="capacity_pair",
        ),
        CheckConstraint("lead_time_days IS NULL OR lead_time_days >= 0", name="lead_time_nonnegative"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
        CheckConstraint(
            "supply_share_pct IS NULL OR (supply_share_pct >= 0 AND supply_share_pct <= 100)", name="share_range"
        ),
        CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR effective_to >= effective_from", name="effective_range"
        ),
        # One relationship row per (type, endpoints), whatever its status: re-open or reactivate instead of re-creating.
        Index(
            "uq_supply_relationships_supplier_pair",
            "relationship_type",
            "supplier_id",
            "target_facility_id",
            unique=True,
            sqlite_where=text("supplier_id IS NOT NULL"),
            postgresql_where=text("supplier_id IS NOT NULL"),
        ),
        Index(
            "uq_supply_relationships_facility_pair",
            "relationship_type",
            "source_facility_id",
            "target_facility_id",
            unique=True,
            sqlite_where=text("source_facility_id IS NOT NULL"),
            postgresql_where=text("source_facility_id IS NOT NULL"),
        ),
        Index("ix_supply_relationships_status", "status"),
        Index("ix_supply_relationships_supplier_id", "supplier_id"),
        Index("ix_supply_relationships_source_facility_id", "source_facility_id"),
        Index("ix_supply_relationships_target_facility_id", "target_facility_id"),
        Index("ix_supply_relationships_provenance", "provenance"),
    )
