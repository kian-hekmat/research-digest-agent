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
# Summarization defaults to the local Gemma model; no test may call a real
# Ollama server. Tests that exercise the Ollama path pass their own Settings
# and fake the HTTP layer with respx.
os.environ["SUMMARY_BACKEND"] = "none"
# The JSON API is admin-only and fails closed, so the suite needs a token set
# before Settings is first built; `client` sends it, `anon_client` does not.
os.environ["ADMIN_TOKEN"] = "test-admin-token"
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

# Settings would otherwise silently pick up whatever real .env the developer
# has locally for every field a test doesn't override - a real credential
# could end up in an assertion and from there in a failure message, and a
# setting like SUMMARY_BACKEND=ollama makes "real Summarizer" tests call the
# developer's actual local model. Environment variables (like DATABASE_URL
# above) are unaffected - only the local .env file is disabled, process-wide.
#
# This must happen BEFORE anything below is imported: app.database calls
# get_settings() at import time, and get_settings is cached - disabling the
# .env afterwards (as this file used to) leaves the cached, .env-loaded
# Settings in place for the whole session.
Settings.model_config["env_file"] = None
get_settings.cache_clear()

from app.database import SessionLocal, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.arxiv import ArxivResult  # noqa: E402
from app.temporal.client import get_temporal_client  # noqa: E402

# Build the test schema by running the real migrations (not create_all), so a
# drift between models and migrations fails the suite.
_ALEMBIC_CFG = Config("alembic.ini")
_ALEMBIC_CFG.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)


class EmbedderTextMixin:
    """For hand-written fake embedders: the Embedder text methods, in the
    EmbeddingGemma formats the fakes were written against, and a constructor
    that accepts what activities pass (ledger=, topic_id=)."""

    def __init__(self, *args, **kwargs):
        pass

    def document_text(self, title, abstract):
        return f"title: {title} | text: {abstract}"

    def query_text(self, query):
        return f"task: search result | query: {query}"


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

    with TestClient(app, headers=ADMIN_HEADERS) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="function")
def anon_client(client):
    """The same app and overrides as `client`, but sends no admin token."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def make_subscriber(
    db, email="me@example.com", *, confirmed=True, unsubscribed=False, waitlisted=False
):
    """A subscriber in the given lifecycle state; commits. Imported by tests
    (`from tests.conftest import make_subscriber`), not a fixture, so a test
    can create several."""
    from datetime import datetime, timezone

    from app import models

    now = datetime.now(timezone.utc)
    subscriber = models.Subscriber(
        email=email,
        confirmed_at=now if confirmed else None,
        unsubscribed_at=now if unsubscribed else None,
        waitlisted_at=now if waitlisted else None,
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def subscribe(
    db,
    topic,
    subscriber,
    *,
    active=True,
    waitlisted=False,
    cadence="twice_weekly",
    max_papers=None,
    last_sent_at=None,
):
    """Subscribe `subscriber` to `topic`; commits. last_sent_at=None keeps the
    column's default (now)."""
    from datetime import datetime, timezone

    from app import models

    sub = models.Subscription(
        topic_id=topic.id,
        subscriber_id=subscriber.id,
        active=active,
        waitlisted_at=datetime.now(timezone.utc) if waitlisted else None,
        cadence=models.SubscriptionCadence(cadence),
        max_papers=max_papers,
    )
    if last_sent_at is not None:
        sub.last_sent_at = last_sent_at
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub
