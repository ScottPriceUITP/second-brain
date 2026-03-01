"""Database engine, session factory, and declarative base."""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    """Get database URL, defaulting to sqlite:///data/second_brain.db."""
    return os.environ.get("DATABASE_URL", "sqlite:///data/second_brain.db")


def create_db_engine(database_url: str | None = None):
    """Create a SQLAlchemy engine.

    Args:
        database_url: Override database URL. Uses get_database_url() if None.
    """
    url = database_url or get_database_url()

    # Ensure the data directory exists for SQLite
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        db_path = url.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    return create_engine(url, echo=False)


def create_session_factory(engine):
    """Create a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)
