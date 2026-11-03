"""Check that a database is migrated to the Alembic head revision."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from app.core.errors import SchemaNotMigratedError

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(url: str | None = None) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    if url:
        config.attributes["override_url"] = url
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("No Alembic revisions found")
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def assert_schema_current(engine: Engine) -> str:
    """Return the revision if the DB is at head; raise SchemaNotMigratedError otherwise."""
    current = current_revision(engine)
    head = head_revision()
    if current is None:
        raise SchemaNotMigratedError(
            "Database has not been migrated. Run 'alembic upgrade head' from the backend directory first."
        )
    if current != head:
        raise SchemaNotMigratedError(
            f"Database is at revision {current} but the application expects {head}. "
            "Run 'alembic upgrade head' from the backend directory."
        )
    return current
