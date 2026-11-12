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
# is the earliest subscription. me is on both A and B: the keeper's (A)
# subscription is inactive and was sent later, the duplicate's (B) is active
# and was sent earlier. Only B has a description. x@example.com is on C twice,
# in two casings, so the same-topic dedupe has something to delete.
_SEED = [
    (
        "INSERT INTO topics (id, name, query, description, created_at) VALUES "
        f"('{TOPIC_A}', 'RLHF', 'RLHF', NULL, '2026-01-01T00:00:00Z'), "
        f"('{TOPIC_B}', 'rlhf dup', ' \"rlhf\" ', 'Alignment from human preferences', '2026-01-02T00:00:00Z'), "
        f"('{TOPIC_C}', 'Numerical Analysis', 'cat:math.NA', NULL, '2026-01-03T00:00:00Z')"
    ),
    (
        "INSERT INTO subscriptions "
        "(id, topic_id, email, cadence, max_papers, active, last_sent_at, created_at) VALUES "
        f"(gen_random_uuid(), '{TOPIC_A}', 'me@example.com', 'weekly', NULL, false, '2026-03-02T00:00:00Z', '2026-02-01T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_B}', 'Me@Example.com', 'biweekly', 5, true, '2026-03-01T00:00:00Z', '2026-02-02T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_C}', 'me@example.com', 'twice_weekly', 10, true, '2026-03-01T00:00:00Z', '2026-02-03T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_B}', 'other@example.com', 'weekly', 7, true, '2026-03-01T00:00:00Z', '2026-02-04T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_C}', 'x@example.com', 'weekly', 3, true, '2026-03-01T00:00:00Z', '2026-02-06T00:00:00Z'), "
        f"(gen_random_uuid(), '{TOPIC_C}', 'X@Example.com', 'weekly', 4, true, '2026-03-01T00:00:00Z', '2026-02-07T00:00:00Z')"
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
    # me, other, x (x's two casings on C are one subscriber, one subscription)
    assert db.execute(text("select count(*) from subscribers")).scalar() == 3
    assert db.execute(text("select count(*) from topics")).scalar() == 2
    assert db.execute(text("select count(*) from subscriptions")).scalar() == 4


def test_upgrade_merges_and_preserves(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed  # a Session after upgrade to head
    assert db.execute(text("select count(*) from subscribers")).scalar() == 3
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
            [
                (TOPIC_A, "me@example.com"),
                (TOPIC_C, "me@example.com"),
                (TOPIC_A, "other@example.com"),
                (TOPIC_C, "x@example.com"),
            ]
        )
        topic_cols = {c["name"] for c in inspect(conn).get_columns("topics")}
        assert "normalized_query" not in topic_cols
        assert not inspect(conn).has_table("subscribers")

    command.upgrade(_ALEMBIC_CFG, "head")
    with SessionLocal() as db:
        _assert_merged_counts(db)


def test_upgrade_merges_the_duplicates_subscription_into_the_keepers(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed
    # me's keeper subscription on A was inactive, the duplicate's on B active:
    # the merged one is active, and resumes from the earlier last send.
    me_on_a = db.execute(
        text(
            "select s.active, s.last_sent_at from subscriptions s "
            "join subscribers u on u.id = s.subscriber_id "
            "where s.topic_id=:a and lower(u.email)='me@example.com'"
        ),
        {"a": TOPIC_A},
    ).one()
    assert me_on_a.active is True
    assert me_on_a.last_sent_at.isoformat() == "2026-03-01T00:00:00+00:00"


def test_upgrade_keeps_a_description_only_the_duplicate_had(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed
    assert db.execute(
        text("select description from topics where id=:a"), {"a": TOPIC_A}
    ).scalar() == "Alignment from human preferences"


def test_upgrade_dedupes_one_email_in_two_casings_on_one_topic(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed
    rows = db.execute(
        text(
            "select u.email, s.max_papers from subscriptions s "
            "join subscribers u on u.id = s.subscriber_id "
            "where s.topic_id=:c and lower(u.email)='x@example.com'"
        ),
        {"c": TOPIC_C},
    ).all()
    # One subscription, the oldest (x@example.com's, max_papers 3).
    assert [(r.email, r.max_papers) for r in rows] == [("x@example.com", 3)]


TOPIC_E = "00000000-0000-0000-0000-00000000000e"


def test_downgrade_keeps_only_confirmed_live_subscriptions_active(alembic_at_head_from_seed):
    # The pre-Phase-1 code emails every active subscription, so the downgrade
    # must not leave anyone active who never opted in or has opted out.
    db = alembic_at_head_from_seed
    db.execute(
        text("INSERT INTO topics (id, name, query, normalized_query) VALUES (:id, 'DPO', 'dpo', 'dpo')"),
        {"id": TOPIC_E},
    )
    people = {
        # email: (confirmed_at, unsubscribed_at, subscriber waitlisted_at, subscription waitlisted_at)
        "unconfirmed@example.com": ("NULL", "NULL", "NULL", "NULL"),
        "unsubscribed@example.com": ("now()", "now()", "NULL", "NULL"),
        "waitlisted@example.com": ("now()", "NULL", "now()", "NULL"),
        "subwaitlisted@example.com": ("now()", "NULL", "NULL", "now()"),
        "ok@example.com": ("now()", "NULL", "NULL", "NULL"),
    }
    for email, (confirmed, unsubscribed, waitlisted, sub_waitlisted) in people.items():
        subscriber_id = db.execute(
            text(
                "INSERT INTO subscribers (id, email, confirmed_at, unsubscribed_at, waitlisted_at) "
                f"VALUES (gen_random_uuid(), :email, {confirmed}, {unsubscribed}, {waitlisted}) "
                "RETURNING id"
            ),
            {"email": email},
        ).scalar()
        db.execute(
            text(
                "INSERT INTO subscriptions (id, topic_id, subscriber_id, active, waitlisted_at) "
                f"VALUES (gen_random_uuid(), :t, :s, true, {sub_waitlisted})"
            ),
            {"t": TOPIC_E, "s": subscriber_id},
        )
    db.commit()
    db.close()

    command.downgrade(_ALEMBIC_CFG, "e1f2a3b4c5d6")

    with engine.connect() as conn:
        active = dict(
            conn.execute(
                text("select email, active from subscriptions where topic_id=:t"), {"t": TOPIC_E}
            ).all()
        )
    assert active == {
        "unconfirmed@example.com": False,
        "unsubscribed@example.com": False,
        "waitlisted@example.com": False,
        "subwaitlisted@example.com": False,
        "ok@example.com": True,
    }


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
