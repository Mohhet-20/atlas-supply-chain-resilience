"""Alembic environment. URL precedence: config.attributes['override_url'] > ATLAS settings."""

from __future__ import annotations

from logging.config import fileConfig

import app.models  # noqa: F401  (register models on metadata)
from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import create_db_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("override_url") or get_settings().resolved_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_url())
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Batch migrations rebuild tables (copy, drop, rename). With foreign keys enforced, dropping a parent table
            # that still has child rows fails, so enforcement is paused for the run and the whole database is checked
            # afterwards instead. The PRAGMA only works outside a transaction, hence the raw driver connection.
            connection.connection.dbapi_connection.execute("PRAGMA foreign_keys=OFF")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=sqlite,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Migration left {len(violations)} foreign-key violations, e.g. {violations[:3]}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
