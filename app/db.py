"""Database engine and session management (SQLAlchemy 2.0 style)."""

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _ensure_sqlite_parent(url: str) -> None:
    """Create the parent directory for a file-backed SQLite URL.

    SQLAlchemy will not create missing directories -- without this, a URL
    like sqlite:////var/data/a27.db crashes the app at startup with
    "unable to open database file" if /var/data was never mounted (e.g.
    Render free tier, which has no persistent disks). Creating the parents
    lets the app boot on ephemeral storage; data just won't survive a
    redeploy without a real disk/Postgres behind the URL.
    """
    if not url.startswith("sqlite:"):
        return
    raw = url.split(":///", 1)[-1].split("?", 1)[0].strip()
    if not raw or raw == ":memory:":
        return
    Path(raw).expanduser().parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(settings.database_url)
engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Create all tables. Used by tests and dev bootstrap; production uses Alembic."""
    from app import models  # ensure models are imported before create_all

    Base.metadata.create_all(bind=engine)