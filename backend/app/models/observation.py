from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance
from app.models.facility import Facility
from app.models.supplier import Supplier
from app.models.types import UTCDateTime, enum_column, utcnow


class Observation(TenantOwned, Base):
    """A single numeric metric about exactly one supplier or one facility."""

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    observation_code: Mapped[str] = mapped_column(String(48))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    metric_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source_label: Mapped[str] = mapped_column(String(200))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "observation_provenance"))
    quality_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    supplier: Mapped[Supplier | None] = relationship(foreign_keys=[supplier_id])
    facility: Mapped[Facility | None] = relationship(foreign_keys=[facility_id])

    __table_args__ = (
        UniqueConstraint("organization_id", "observation_code", name="uq_observations_org_observation_code"),
        CheckConstraint(
            "(supplier_id IS NOT NULL AND facility_id IS NULL) OR (supplier_id IS NULL AND facility_id IS NOT NULL)",
            name="exactly_one_subject",
        ),
        CheckConstraint("length(trim(unit)) > 0", name="unit_not_blank"),
        CheckConstraint("length(trim(metric_name)) > 0", name="metric_not_blank"),
        CheckConstraint("length(trim(source_label)) > 0", name="source_not_blank"),
        Index("ix_observations_supplier_id", "supplier_id"),
        Index("ix_observations_facility_id", "facility_id"),
        Index("ix_observations_metric_name", "metric_name"),
        Index("ix_observations_observed_at", "observed_at"),
        Index("ix_observations_provenance", "provenance"),
    )
