"""
SQLAlchemy engine/session for the HR backend's OWN database - local users,
roles, committees, sessions, evaluations, results, visibility/classification
overrides, HR cache snapshots. This is NOT the HR Oracle database (reached
read-only via hr_client.py) and NOT the MCP tool's generic query target
(db/adapter.py) - three separate databases, three separate concerns.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from hr_backend.config import get_settings

_engine = None
_SessionFactory: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


def get_db() -> Iterator[Session]:
    """FastAPI dependency - yields a session, always closed after the request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-manager form for use outside of a FastAPI request (background jobs, scripts)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Test-only: force get_engine()/get_session_factory() to rebuild from current settings."""
    global _engine, _SessionFactory
    _engine = None
    _SessionFactory = None
