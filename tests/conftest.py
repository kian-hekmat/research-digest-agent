import os

# Temporal Activities open DB sessions directly via app.database.SessionLocal -
# they don't go through FastAPI's request-scoped `get_db` dependency, so
# overriding that dependency (as the `client` fixture below does for the API
# itself) doesn't reach them. DATABASE_URL has to point at the test DB before
# app.database / app.config are first imported anywhere in this test session,
# since Settings() and the engine are both constructed once, at import time.
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_test_db",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.database import SessionLocal, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.arxiv import ArxivResult  # noqa: E402
from app.temporal.client import get_temporal_client  # noqa: E402

# Build the test schema by running the real migrations (not create_all), so a
# drift between models and migrations fails the suite.
_ALEMBIC_CFG = Config("alembic.ini")
_ALEMBIC_CFG.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)


def make_result(arxiv_id, title, published_at=None):
    from datetime import datetime, timezone

    return ArxivResult(
        arxiv_id=arxiv_id,
        title=title,
        abstract=f"Abstract for {title}",
        published_at=published_at or datetime(2024, 8, 1, tzinfo=timezone.utc),
    )


@pytest.fixture(scope="function")
def db_session():
    command.upgrade(_ALEMBIC_CFG, "head")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        command.downgrade(_ALEMBIC_CFG, "base")


class FakeWorkflowHandle:
    def __init__(self, workflow_id, arg):
        self.workflow_id = workflow_id
        self.arg = arg


class FakeTemporalClient:
    """Stand-in for router-level tests: records `start_workflow` calls instead
    of talking to a real Temporal server. The actual pipeline (fetch,
    summarize, overview, retries, fan-out) is exercised against a real
    (time-skipping) Temporal test server in test_temporal_workflows.py - this
    fake only proves the API layer wires the workflow call correctly."""

    def __init__(self):
        self.started: list[tuple] = []  # (workflow_fn, arg, id, task_queue)

    async def start_workflow(self, workflow, arg, *, id, task_queue, **kwargs):
        self.started.append((workflow, arg, id, task_queue))
        return FakeWorkflowHandle(id, arg)


@pytest.fixture
def fake_temporal_client():
    return FakeTemporalClient()


@pytest.fixture(scope="function")
def client(db_session, fake_temporal_client):
    def override_get_db():
        # Reflect changes committed by a Temporal activity's own session.
        db_session.expire_all()
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_temporal_client] = lambda: fake_temporal_client

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
