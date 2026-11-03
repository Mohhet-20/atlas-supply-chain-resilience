"""Column helpers: timezone-safe datetimes and string-backed enums."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Stores UTC; rejects naive datetimes on write; returns aware UTC datetimes on read.

    Works identically on SQLite (which drops tzinfo) and PostgreSQL.
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Naive datetime rejected: timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_column(enum_cls: type[Enum], name: str) -> SAEnum:
    """Portable string enum with a named CHECK constraint (no native DB enum)."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [member.value for member in e],
    )
