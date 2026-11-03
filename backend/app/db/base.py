"""Declarative base with a deterministic constraint naming convention (needed for Alembic)."""

from __future__ import annotations

from sqlalchemy import ForeignKey, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TenantOwned:
    """Mixin for domain rows owned by one organisation (suppliers, facilities, inventory, runs, ...).

    Every query on a subclass is filtered to the session's organisation and new rows are stamped with it; see
    ``app.db.tenancy``. Existing rows were assigned to the default organisation (id 1) by migration 0022."""

    @declared_attr
    def organization_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey("organizations.id"), default=1, server_default="1", index=True)
