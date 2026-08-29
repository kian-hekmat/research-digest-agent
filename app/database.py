from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import get_settings

# Sourced from Settings (env var DATABASE_URL, or the local default).
# In docker-compose this points at the postgres service.
DATABASE_URL = get_settings().database_url

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a request-scoped DB session, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_factory():
    """FastAPI dependency: the session factory to use for work that outlives the
    request (background tasks own their session). Overridable in tests."""
    return SessionLocal
