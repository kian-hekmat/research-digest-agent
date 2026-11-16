"""The v2.2 cadence migration (e5f6a7b8c9db) against seeded rows: every
twice_weekly subscription becomes weekly and weekly becomes the default.
The live database is converted by exactly this migration."""
import pytest
from alembic import command
from sqlalchemy import text

from app.database import engine
from tests.conftest import _ALEMBIC_CFG

BEFORE = "d4e5f6a7b8ca"
SUBSCRIBER = "00000000-0000-0000-0000-0000000000a1"
TOPICS = {
    "twice_weekly": "00000000-0000-0000-0000-0000000000b1",
    "weekly": "00000000-0000-0000-0000-0000000000b2",
    "biweekly": "00000000-0000-0000-0000-0000000000b3",
}
EXTRA_TOPIC = "00000000-0000-0000-0000-0000000000b4"


def _cadences(conn):
    rows = conn.execute(text("select topic_id, cadence from subscriptions")).all()
    return {str(t): c for t, c in rows}


def _insert_without_cadence(conn):
    conn.execute(
        text(
            "insert into subscriptions (id, topic_id, subscriber_id) "
            "values (gen_random_uuid(), :t, :s)"
        ),
        {"t": EXTRA_TOPIC, "s": SUBSCRIBER},
    )
    return conn.execute(
        text("select cadence from subscriptions where topic_id = :t"), {"t": EXTRA_TOPIC}
    ).scalar()


@pytest.fixture
def seeded_then_upgraded():
    command.downgrade(_ALEMBIC_CFG, "base")
    command.upgrade(_ALEMBIC_CFG, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            text("insert into subscribers (id, email) values (:s, 'me@example.com')"),
            {"s": SUBSCRIBER},
        )
        for i, (cadence, topic) in enumerate([*TOPICS.items(), ("", EXTRA_TOPIC)]):
            conn.execute(
                text(
                    "insert into topics (id, name, query, normalized_query) "
                    "values (:t, :n, :n, :n)"
                ),
                {"t": topic, "n": f"topic {i}"},
            )
            if cadence:
                conn.execute(
                    text(
                        "insert into subscriptions (id, topic_id, subscriber_id, cadence) "
                        "values (gen_random_uuid(), :t, :s, :c)"
                    ),
                    {"t": topic, "s": SUBSCRIBER, "c": cadence},
                )
    command.upgrade(_ALEMBIC_CFG, "head")
    try:
        yield
    finally:
        command.downgrade(_ALEMBIC_CFG, "base")


def test_upgrade_moves_every_subscription_to_weekly(seeded_then_upgraded):
    with engine.connect() as conn:
        assert _cadences(conn) == {
            TOPICS["twice_weekly"]: "weekly",
            TOPICS["weekly"]: "weekly",
            TOPICS["biweekly"]: "biweekly",  # left alone
        }


def test_upgrade_makes_weekly_the_default(seeded_then_upgraded):
    with engine.begin() as conn:
        assert _insert_without_cadence(conn) == "weekly"


def test_downgrade_restores_the_default_but_not_the_rows(seeded_then_upgraded):
    command.downgrade(_ALEMBIC_CFG, BEFORE)
    with engine.begin() as conn:
        # Which rows were twice_weekly isn't recorded, so they stay weekly.
        assert _cadences(conn)[TOPICS["twice_weekly"]] == "weekly"
        assert _insert_without_cadence(conn) == "twice_weekly"
