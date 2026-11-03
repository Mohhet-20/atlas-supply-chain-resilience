from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance, SourceStatus, SourceType
from app.models.types import UTCDateTime, enum_column, utcnow


class DataSource(TenantOwned, Base):
    """An external (or synthetic) system of record. `namespace` scopes external identifiers."""

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    namespace: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[SourceType] = mapped_column(enum_column(SourceType, "data_source_type"))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "data_source_provenance"))
    status: Mapped[SourceStatus] = mapped_column(enum_column(SourceStatus, "data_source_status"))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "namespace", name="uq_data_sources_org_namespace"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(namespace) > 0", name="namespace_not_blank"),
        Index("ix_data_sources_status", "status"),
    )
