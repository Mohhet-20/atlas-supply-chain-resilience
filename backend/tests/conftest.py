from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import Settings
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from app.main import create_app
from app.services.seed import seed_demo_data

# ATLAS_TEST_POSTGRES_URL (an administrative URL such as postgresql+psycopg://postgres@127.0.0.1:5432/postgres) runs the
# suite on PostgreSQL: one database is migrated once and cloned per test (CREATE DATABASE ... TEMPLATE). Tests that are
# about SQLite itself carry the ``sqlite_only`` marker and are skipped there.
PG_ADMIN_URL = os.environ.get("ATLAS_TEST_POSTGRES_URL")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "sqlite_only: exercises SQLite-specific behaviour (files, PRAGMAs, triggers)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if PG_ADMIN_URL:
        skip = pytest.mark.skip(reason="SQLite-specific; the suite is running on PostgreSQL")
        for item in items:
            if "sqlite_only" in item.keywords:
                item.add_marker(skip)


def _pg_admin(sql: str) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(sql))
    eng.dispose()


def _pg_url(database: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(PG_ADMIN_URL).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path | str:
    """One migrated database (built via Alembic, not create_all) copied per test: a SQLite file, or a PostgreSQL
    template database."""
    if PG_ADMIN_URL:
        name = f"atlas_tpl_{uuid.uuid4().hex[:8]}"
        _pg_admin(f"CREATE DATABASE {name}")
        command.upgrade(alembic_config(_pg_url(name)), "head")
        yield name
        _pg_admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        return
    path = tmp_path_factory.mktemp("template") / "template.db"
    url = f"sqlite:///{path.as_posix()}"
    command.upgrade(alembic_config(url), "head")
    yield path


@pytest.fixture
def db_url(tmp_path: Path, template_db: Path | str) -> Iterator[str]:
    if PG_ADMIN_URL:
        name = f"atlas_t_{uuid.uuid4().hex[:12]}"
        _pg_admin(f"CREATE DATABASE {name} TEMPLATE {template_db}")
        yield _pg_url(name)
        _pg_admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        return
    target = tmp_path / "atlas_test.db"
    shutil.copy(template_db, target)
    yield f"sqlite:///{target.as_posix()}"


@pytest.fixture
def settings(tmp_path: Path, db_url: str) -> Settings:
    return Settings(_env_file=None, environment="test", database_url=db_url, data_dir=tmp_path, log_level="WARNING")


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = create_db_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def client(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(settings, engine)) as c:
        yield c


@pytest.fixture
def seeded(settings: Settings, engine: Engine):
    return seed_demo_data(engine, settings)
