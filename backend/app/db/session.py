"""Engine and session management. Schema creation is NOT done here - use Alembic."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db import tenancy  # noqa: F401  (registers the tenant query filter on Session)


def create_db_engine(url: str, *, ensure_sqlite_dir: bool = True, settings: Settings | None = None) -> Engine:
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    kwargs: dict = {}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        if ensure_sqlite_dir and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    elif settings is not None:
        kwargs.update(
            pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds, pool_pre_ping=True,
        )  # fmt: skip
    busy = settings.sqlite_busy_timeout_ms if settings is not None else 5000
    wal = bool(settings and settings.sqlite_wal)
    engine = create_engine(url, **kwargs)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            # Take over transaction control from pysqlite so SAVEPOINTs behave correctly
            # (SQLAlchemy documented recipe); needed for per-row rollback during ingestion.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy)}")  # wait for a competing writer instead of failing at once
            if wal:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(connection) -> None:
            # Writers that must not fail on a stale read snapshot ask for BEGIN IMMEDIATE (see ``write_transaction``).
            immediate = connection.get_execution_options().get("sqlite_immediate", False)
            connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def create_engine_from_settings(settings: Settings) -> Engine:
    return create_db_engine(settings.resolved_database_url, settings=settings)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def immediate_bind(engine: Engine) -> Engine:
    """The engine for sessions that write: on SQLite every transaction starts with BEGIN IMMEDIATE (see the ``begin``
    listener), so concurrent writers queue behind ``busy_timeout`` instead of failing when a read upgrades to a write.
    Other databases get the engine unchanged."""
    return engine.execution_options(sqlite_immediate=True) if engine.dialect.name == "sqlite" else engine


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed, rolled back on error. Requests that can change data
    get a writer session (``immediate_bind``)."""
    from app.db.tenancy import bind_request

    factory: sessionmaker[Session] = request.app.state.session_factory
    writes = request.method not in _READ_METHODS
    session = factory(bind=immediate_bind(factory.kw["bind"])) if writes else factory()
    bind_request(session, request.state)  # queries are scoped to the authenticated actor's organisation
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def write_transaction(db: Session) -> None:
    """Start the session's next transaction as a SQLite ``BEGIN IMMEDIATE`` (a no-op for other databases).

    A deferred transaction that reads and then writes is refused at once with "database is locked" when another
    connection wrote in between, regardless of the busy timeout. Taking the write lock first makes competing
    writers queue behind ``busy_timeout`` instead. Call it before the first statement of a short write unit; do not
    hold it across slow network calls.
    """
    db.connection(execution_options={"sqlite_immediate": True})
