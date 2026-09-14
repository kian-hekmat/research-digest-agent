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
    """FastAPI dependency: yields a request-scoped DB session, always closed.

    Temporal Activities (app/temporal/activities.py) don't go through this -
    they're not part of a request, so each opens its own SessionLocal()
    directly and closes it when done.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
