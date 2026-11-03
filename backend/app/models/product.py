from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantOwned
from app.models.enums import EdgeCriticality, EdgeStatus, EntityStatus, ProductLinkType, Provenance
from app.models.facility import Facility
from app.models.source import DataSource  # noqa: F401
from app.models.supplier import Supplier
from app.models.types import UTCDateTime, enum_column, utcnow


class Product(TenantOwned, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_code: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100))
    unit_of_measure: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[EntityStatus] = mapped_column(enum_column(EntityStatus, "product_status"))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "product_provenance"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(1000))
    manufacturer_supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    name_normalized: Mapped[str] = mapped_column(String(255), default="")
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
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
        UniqueConstraint("organization_id", "product_code", name="uq_products_org_product_code"),
        UniqueConstraint("source_id", "external_id", name="uq_products_source_external"),
        CheckConstraint("merged_into_id IS NULL OR merged_into_id <> id", name="merged_not_self"),
        CheckConstraint("(merged_into_id IS NULL) = (merged_at IS NULL)", name="merged_pair"),
        Index("ix_products_name_normalized", "name_normalized"),
        Index("ix_products_merged_into_id", "merged_into_id"),
        Index("ix_products_manufacturer_supplier_id", "manufacturer_supplier_id"),
        CheckConstraint("(source_id IS NULL) = (external_id IS NULL)", name="source_pair"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(trim(product_code)) > 0", name="code_not_blank"),
        Index("ix_products_status", "status"),
        Index("ix_products_category", "category"),
        Index("ix_products_name", "name"),
    )


class ProductLink(TenantOwned, Base):
    """Typed product association.

    SUPPLIES_PRODUCT: supplier -> product;  PRODUCES / STORES: facility -> product.
    """

    __tablename__ = "product_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    link_code: Mapped[str] = mapped_column(String(128))
    link_type: Mapped[ProductLinkType] = mapped_column(enum_column(ProductLinkType, "product_link_type"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    quantity_value: Mapped[float | None] = mapped_column(Float)
    quantity_unit: Mapped[str | None] = mapped_column(String(32))
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "product_link_provenance"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    # Network-graph attributes (optional values stay NULL when unknown).
    status: Mapped[EdgeStatus] = mapped_column(
        enum_column(EdgeStatus, "product_link_status"), default=EdgeStatus.CONFIRMED, server_default="CONFIRMED"
    )
    status_reason: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Float)
    criticality: Mapped[EdgeCriticality | None] = mapped_column(
        enum_column(EdgeCriticality, "product_link_criticality")
    )
    supply_share_pct: Mapped[float | None] = mapped_column(Float)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}

    source: Mapped[DataSource | None] = relationship("DataSource", lazy="joined")

    @property
    def source_namespace(self) -> str | None:
        return self.source.namespace if self.source is not None else None

    product: Mapped[Product] = relationship()
    supplier: Mapped[Supplier | None] = relationship()
    facility: Mapped[Facility | None] = relationship()

    __table_args__ = (
        UniqueConstraint("organization_id", "link_code", name="uq_product_links_org_link_code"),
        UniqueConstraint("source_id", "external_id", name="uq_product_links_source_external"),
        CheckConstraint("(source_id IS NULL) = (external_id IS NULL)", name="source_pair"),
        CheckConstraint(
            "(link_type = 'SUPPLIES_PRODUCT' AND supplier_id IS NOT NULL AND facility_id IS NULL) OR "
            "(link_type IN ('PRODUCES','STORES') AND facility_id IS NOT NULL AND supplier_id IS NULL)",
            name="valid_endpoints",
        ),
        CheckConstraint(
            "(quantity_value IS NULL AND quantity_unit IS NULL) OR "
            "(quantity_value IS NOT NULL AND quantity_unit IS NOT NULL AND quantity_value >= 0)",
            name="quantity_pair",
        ),
        CheckConstraint("lead_time_days IS NULL OR lead_time_days >= 0", name="lead_time_nonnegative"),
        CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range",
        ),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
        CheckConstraint(
            "supply_share_pct IS NULL OR (supply_share_pct >= 0 AND supply_share_pct <= 100)", name="share_range"
        ),
        Index("ix_product_links_status", "status"),
        Index("ix_product_links_product_id", "product_id"),
        Index("ix_product_links_supplier_id", "supplier_id"),
        Index("ix_product_links_facility_id", "facility_id"),
        Index(
            "uq_product_links_supplier_pair",
            "link_type",
            "supplier_id",
            "product_id",
            unique=True,
            sqlite_where=text("supplier_id IS NOT NULL"),
            postgresql_where=text("supplier_id IS NOT NULL"),
        ),
        Index(
            "uq_product_links_facility_pair",
            "link_type",
            "facility_id",
            "product_id",
            unique=True,
            sqlite_where=text("facility_id IS NOT NULL"),
            postgresql_where=text("facility_id IS NOT NULL"),
        ),
    )
