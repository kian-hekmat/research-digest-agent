import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# In docker-compose, this points at the postgres service.
# Locally without docker, override with an env var.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_db",
)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a DB session, always closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
