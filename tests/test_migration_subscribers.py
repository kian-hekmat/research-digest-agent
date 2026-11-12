"""The subscribers/shared-topics migration (a1b2c3d4e5f7) against seeded
pre-migration data: the live database is converted by exactly this
migration, so merges, casing and the downgrade are pinned here."""
import importlib.util
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from app.database import SessionLocal, engine
from app.topic_query import normalize_query
from tests.conftest import _ALEMBIC_CFG

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic/versions/a1b2c3d4e5f7_subscribers_and_shared_topics.py"
)

TOPIC_A = "00000000-0000-0000-0000-00000000000a"
TOPIC_B = "00000000-0000-0000-0000-00000000000b"
TOPIC_C = "00000000-0000-0000-0000-00000000000c"
PAPER_P = "00000000-0000-0000-0000-0000000000f1"
PAPER_Q = "00000000-0000-0000-0000-0000000000f2"

# Explicit ascending created_at, so "oldest topic" and "earliest casing" are
# deterministic: A before B before C, and me@example.com's subscription on A
# is the earliest subscription.
_SEED = [
    (
        "INSERT INTO topics (id, name, query, created_at) VALUES "
        f"('{TOPIC_A}', 'RLHF', 'RLHF', '2026-01-01T00:00:00Z'), "
        f"('{TOPIC_B}', 'rlhf dup', ' \"rlhf\" ', '2026-01-02T00:00:00Z'), "
        f"('{TOPIC_C}', 'Numerical Analysis', 'cat:math.NA', '2026-01-03T00:00:00Z')"
    ),
    (
        "INSERT INTO subscriptions (id, topic_id, email, cadence, max_papers, created_at) VALUES "
        f"(gen_random_uuid(), '{TOPIC_A}', 'me@example.com', 'weekly', NULL, '2026-02-01T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_B}', 'Me@Example.com', 'biweekly', 5, '2026-02-02T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_C}', 'me@example.com', 'twice_weekly', 10, '2026-02-03T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_B}', 'other@example.com', 'weekly', 7, '2026-02-04T00:00:00Z')"
    ),
    (
        "INSERT INTO papers (id, arxiv_id, title) VALUES "
        f"('{PAPER_P}', '2601.00001', 'Paper P'), ('{PAPER_Q}', '2601.00002', 'Paper Q')"
    ),
    (
        "INSERT INTO topic_paper (topic_id, paper_id, relevance) VALUES "
        f"('{TOPIC_A}', '{PAPER_P}', 8), ('{TOPIC_B}', '{PAPER_P}', 3), "
        f"('{TOPIC_B}', '{PAPER_Q}', 6)"
    ),
    (
        "INSERT INTO digests (id, topic_id, status, completed_at) VALUES "
        f"(gen_random_uuid(), '{TOPIC_B}', 'completed', '2026-02-05T00:00:00Z')"
    ),
]


@pytest.fixture
def alembic_at_head_from_seed():
    command.downgrade(_ALEMBIC_CFG, "base")
    command.upgrade(_ALEMBIC_CFG, "e1f2a3b4c5d6")
    with engine.begin() as conn:
        for stmt in _SEED:
            conn.execute(text(stmt))
    command.upgrade(_ALEMBIC_CFG, "head")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        command.downgrade(_ALEMBIC_CFG, "base")


def _assert_merged_counts(db):
    assert db.execute(text("select count(*) from subscribers")).scalar() == 2
    assert db.execute(text("select count(*) from topics")).scalar() == 2
    assert db.execute(text("select count(*) from subscriptions")).scalar() == 3


def test_upgrade_merges_and_preserves(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed  # a Session after upgrade to head
    assert db.execute(text("select count(*) from subscribers")).scalar() == 2
    me = db.execute(
        text("select id, email, confirmed_at from subscribers where lower(email)='me@example.com'")
    ).one()
    assert me.email == "me@example.com" and me.confirmed_at is not None
    topics = db.execute(text("select name, normalized_query from topics order by created_at")).all()
    assert [t.normalized_query for t in topics] == ["rlhf", "cat:math.NA"]
    a = db.execute(text("select id from topics where normalized_query='rlhf'")).scalar()
    assert str(a) == TOPIC_A  # the oldest topic in the group is kept
    assert db.execute(text("select count(*) from subscriptions where topic_id=:a"), {"a": a}).scalar() == 2  # me (once) + other
    assert db.execute(text("select count(*) from topic_paper where topic_id=:a"), {"a": a}).scalar() == 2  # P once, Q
    assert db.execute(text("select count(*) from digests where topic_id=:a"), {"a": a}).scalar() == 1
    assert {r[0] for r in db.execute(text("select distinct cadence from subscriptions"))} == {"twice_weekly"}
    assert db.execute(text("select count(*) from subscriptions where max_papers is null")).scalar() == 0
    assert db.execute(text("select count(*) from papers")).scalar() == 2


def test_upgrade_keeps_the_keepers_rows_on_conflict(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed
    # P was linked to both A (relevance 8) and B (relevance 3): A's row wins.
    assert db.execute(
        text("select relevance from topic_paper where topic_id=:a and paper_id=:p"),
        {"a": TOPIC_A, "p": PAPER_P},
    ).scalar() == 8
    # me's subscription on A (oldest, max_papers NULL -> 10) is kept, not B's (5).
    me_on_a = db.execute(
        text(
            "select s.max_papers from subscriptions s join subscribers u on u.id = s.subscriber_id "
            "where s.topic_id=:a and lower(u.email)='me@example.com'"
        ),
        {"a": TOPIC_A},
    ).one()
    assert me_on_a.max_papers == 10


def test_downgrade_then_upgrade_round_trips(alembic_at_head_from_seed):
    alembic_at_head_from_seed.close()
    command.downgrade(_ALEMBIC_CFG, "e1f2a3b4c5d6")

    with engine.connect() as conn:
        rows = conn.execute(text("select topic_id, email from subscriptions")).all()
        assert sorted((str(t), e) for t, e in rows) == sorted(
            [(TOPIC_A, "me@example.com"), (TOPIC_C, "me@example.com"), (TOPIC_A, "other@example.com")]
        )
        topic_cols = {c["name"] for c in inspect(conn).get_columns("topics")}
        assert "normalized_query" not in topic_cols
        assert not inspect(conn).has_table("subscribers")

    command.upgrade(_ALEMBIC_CFG, "head")
    with SessionLocal() as db:
        _assert_merged_counts(db)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_a1b2c3d4e5f7", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "raw",
    ['"RLHF"', "“RLHF”", "  physics-informed\t  neural\nnetworks ", "cat:math.NA", "ti:Foo AND au:Bar", "Bayes' rule"],
)
def test_migration_normalizer_matches_the_app(raw):
    assert _load_migration().normalize_query(raw) == normalize_query(raw)


def test_downgrade_renames_topics_that_share_a_name(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed
    db.execute(
        text("INSERT INTO topics (id, name, query, normalized_query) VALUES (:id, 'RLHF', 'dpo', 'dpo')"),
        {"id": "00000000-0000-0000-0000-00000000000d"},
    )
    db.commit()
    db.close()

    command.downgrade(_ALEMBIC_CFG, "e1f2a3b4c5d6")

    with engine.connect() as conn:
        names = dict(conn.execute(text("select id::text, name from topics")).all())
    assert names[TOPIC_A] == "RLHF"  # the oldest keeps its name
    assert names["00000000-0000-0000-0000-00000000000d"] == "RLHF 00000000-0000-0000-0000-00000000000d"
