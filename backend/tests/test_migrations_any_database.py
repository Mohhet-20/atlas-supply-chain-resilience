"""Migration checks for whichever database the suite uses (SQLite by default, PostgreSQL when ATLAS_TEST_POSTGRES_URL
is set): the template database was built by ``alembic upgrade head`` and must match the models."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

import app.models  # noqa: F401
from app.db.base import Base
from app.db.migrations_check import current_revision, head_revision


def test_database_is_at_head_and_matches_the_models(engine):
    assert current_revision(engine) == head_revision()
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []
