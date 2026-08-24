"""Database engine + session helpers (SQLite via SQLModel)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
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
            connect_args={"check_same_thread": False},
        )
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
