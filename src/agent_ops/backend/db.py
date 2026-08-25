"""Database engine + session helpers (SQLite via SQLModel)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from agent_ops.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        s.db_file.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            s.sqlite_url,
            # check_same_thread=False + WAL + a busy timeout let the async worker
            # pool write concurrently without "database is locked" errors.
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return _engine


def init_db() -> None:
    """Create all tables. Import models so they register on the metadata."""
    import agent_ops.backend.models  # noqa: F401

    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session (no auto-commit; routes commit)."""
    with Session(get_engine()) as session:
        yield session
