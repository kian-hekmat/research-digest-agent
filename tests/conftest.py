import os
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db, get_session_factory
from app.main import app
from app.routers.digests import get_arxiv_fetcher, get_summarizer
from app.services.arxiv import ArxivResult

# Point at a separate test database so tests never touch dev data.
# In CI this is set to the Postgres service defined in the workflow.
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_test_db",
)

engine = create_engine(TEST_DATABASE_URL)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Build the test schema by running the real migrations (not create_all), so a
# drift between models and migrations fails the suite.
_ALEMBIC_CFG = Config("alembic.ini")
_ALEMBIC_CFG.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)


# ---------- test doubles for the external services ----------
class FakeFetcher:
    """Stand-in for `search_arxiv`. Set `.results` or `.exc`."""

    def __init__(self):
        self.results: list[ArxivResult] = []
        self.exc: Exception | None = None
        self.calls: list[tuple] = []

    def __call__(self, query, since=None):
        self.calls.append((query, since))
        if self.exc is not None:
            raise self.exc
        return list(self.results)


class FakeSummarizer:
    """Stand-in for `Summarizer`. Titles in `fail_titles` raise."""

    def __init__(self, fail_titles=()):
        self.fail_titles = set(fail_titles)
        self.paper_calls: list[str] = []
        self.overview_calls: list[tuple] = []

    def summarize_paper(self, title, abstract):
        self.paper_calls.append(title)
        if title in self.fail_titles:
            raise RuntimeError(f"summary failed for {title}")
        return f"Summary of {title}."

    def write_overview(self, topic_name, summaries):
        self.overview_calls.append((topic_name, tuple(summaries)))
        return f"Overview of {topic_name}: {len(summaries)} papers."


def make_result(arxiv_id, title, published_at=None) -> ArxivResult:
    return ArxivResult(
        arxiv_id=arxiv_id,
        title=title,
        abstract=f"Abstract for {title}",
        published_at=published_at or datetime(2024, 8, 1, tzinfo=timezone.utc),
    )


@pytest.fixture(scope="function")
def db_session():
    command.upgrade(_ALEMBIC_CFG, "head")
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        command.downgrade(_ALEMBIC_CFG, "base")


@pytest.fixture
def fake_fetcher():
    return FakeFetcher()


@pytest.fixture
def fake_summarizer():
    return FakeSummarizer()


@pytest.fixture(scope="function")
def client(db_session, fake_fetcher, fake_summarizer):
    def override_get_db():
        # Reflect changes committed by the background task's own session.
        db_session.expire_all()
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: TestingSessionLocal
    app.dependency_overrides[get_arxiv_fetcher] = lambda: fake_fetcher
    app.dependency_overrides[get_summarizer] = lambda: fake_summarizer

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
