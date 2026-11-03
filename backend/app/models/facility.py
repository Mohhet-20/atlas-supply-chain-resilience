from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantOwned
from app.models.enums import EntityStatus, FacilityType, LocationStatus, Provenance
from app.models.source import DataSource  # noqa: F401
from app.models.types import UTCDateTime, enum_column, utcnow

if TYPE_CHECKING:
    from app.models.supplier import Supplier


class Facility(TenantOwned, Base):
    __tablename__ = "facilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_code: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(200))
    facility_type: Mapped[FacilityType] = mapped_column(enum_column(FacilityType, "facility_type"))
    country_or_region: Mapped[str] = mapped_column(String(100))
    status: Mapped[EntityStatus] = mapped_column(enum_column(EntityStatus, "facility_status"))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "facility_provenance"))
    city: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_status: Mapped[LocationStatus] = mapped_column(
        enum_column(LocationStatus, "facility_location_status"), default=LocationStatus.UNKNOWN
    )
    capacity_value: Mapped[float | None] = mapped_column(Float)
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    owner_supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(300))
    name_normalized: Mapped[str] = mapped_column(String(255), default="")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    merged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    source: Mapped[DataSource | None] = relationship("DataSource", lazy="joined")

    @property
    def source_namespace(self) -> str | None:
        return self.source.namespace if self.source is not None else None

    owner: Mapped[Supplier | None] = relationship("Supplier", foreign_keys=[owner_supplier_id], lazy="joined")

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        UniqueConstraint("organization_id", "facility_code", name="uq_facilities_org_facility_code"),
        CheckConstraint("merged_into_id IS NULL OR merged_into_id <> id", name="merged_not_self"),
        CheckConstraint("(merged_into_id IS NULL) = (merged_at IS NULL)", name="merged_pair"),
        Index("ix_facilities_name_normalized", "name_normalized"),
        Index("ix_facilities_merged_into_id", "merged_into_id"),
        UniqueConstraint("source_id", "external_id", name="uq_facilities_source_external"),
        CheckConstraint("(source_id IS NULL) = (external_id IS NULL)", name="source_pair"),
        CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR (latitude BETWEEN -90 AND 90 "
            "AND longitude BETWEEN -180 AND 180)",
            name="geo_pair",
        ),
        CheckConstraint(
            "(capacity_value IS NULL AND capacity_unit IS NULL) OR "
            "(capacity_value IS NOT NULL AND capacity_unit IS NOT NULL AND capacity_value >= 0)",
            name="capacity_pair",
        ),
        Index("ix_facilities_owner_supplier_id", "owner_supplier_id"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(trim(facility_code)) > 0", name="code_not_blank"),
        Index("ix_facilities_status", "status"),
        Index("ix_facilities_facility_type", "facility_type"),
        Index("ix_facilities_country_or_region", "country_or_region"),
        Index("ix_facilities_provenance", "provenance"),
        Index("ix_facilities_name", "name"),
    )
