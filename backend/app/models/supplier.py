from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantOwned
from app.models.enums import EntityStatus, Provenance
from app.models.source import DataSource  # noqa: F401
from app.models.types import UTCDateTime, enum_column, utcnow


class Supplier(TenantOwned, Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_code: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(200))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    country_or_region: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(100))
    status: Mapped[EntityStatus] = mapped_column(enum_column(EntityStatus, "supplier_status"))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "supplier_provenance"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(300))
    parent_supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    name_normalized: Mapped[str] = mapped_column(String(255), default="")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    merged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    source: Mapped[DataSource | None] = relationship("DataSource", lazy="joined")

    @property
    def source_namespace(self) -> str | None:
        return self.source.namespace if self.source is not None else None

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        UniqueConstraint("organization_id", "supplier_code", name="uq_suppliers_org_supplier_code"),
        CheckConstraint("parent_supplier_id IS NULL OR parent_supplier_id <> id", name="parent_not_self"),
        CheckConstraint("merged_into_id IS NULL OR merged_into_id <> id", name="merged_not_self"),
        CheckConstraint("(merged_into_id IS NULL) = (merged_at IS NULL)", name="merged_pair"),
        Index("ix_suppliers_name_normalized", "name_normalized"),
        Index("ix_suppliers_merged_into_id", "merged_into_id"),
        Index("ix_suppliers_parent_supplier_id", "parent_supplier_id"),
        UniqueConstraint("source_id", "external_id", name="uq_suppliers_source_external"),
        CheckConstraint("(source_id IS NULL) = (external_id IS NULL)", name="source_pair"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(trim(supplier_code)) > 0", name="code_not_blank"),
        Index("ix_suppliers_status", "status"),
        Index("ix_suppliers_country_or_region", "country_or_region"),
        Index("ix_suppliers_category", "category"),
        Index("ix_suppliers_provenance", "provenance"),
        Index("ix_suppliers_name", "name"),
    )
