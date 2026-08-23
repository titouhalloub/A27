"""Database engine and session management (SQLAlchemy 2.0 style)."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Create all tables. Used by tests and dev bootstrap; production uses Alembic."""
    from app import models  # ensure models are imported before create_all

    Base.metadata.create_all(bind=engine)