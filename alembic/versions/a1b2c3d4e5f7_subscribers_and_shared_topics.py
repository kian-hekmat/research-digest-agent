"""subscribers table, subscriptions point at it, topics shared by normalized query

Revision ID: a1b2c3d4e5f7
Revises: e1f2a3b4c5d6
Create Date: 2026-11-12 10:00:00.000000

Converts live data: one subscriber per email (case-insensitive), and topics
whose queries normalize to the same thing are merged into the oldest one.
"""
import re
from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Copied from app.topic_query / app.services.arxiv rather than imported: a
# migration must keep producing the same result after the app code moves on.
# tests/test_migration_subscribers.py checks the two still agree.
_FIELD_PREFIX = re.compile(r"^(ti|au|abs|co|jr|cat|rn|id|all):")
_QUOTES = str.maketrans("", "", '"“”')


def normalize_query(query: str) -> str:
    stripped = query.strip()
    if _FIELD_PREFIX.match(stripped):
        return stripped
    phrase = " ".join(stripped.translate(_QUOTES).split()).lower()
    if not phrase:
        raise ValueError("empty query")
    return phrase


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. subscribers: one per lower(email), from existing subscriptions ---
    op.create_table(
        "subscribers",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waitlisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_manage_link_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("us_resident_attested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "uq_subscribers_email_lower", "subscribers", [sa.text("lower(email)")], unique=True
    )
    # Existing subscribers were added by the owner, so they count as confirmed
    # from their first subscription. The casing kept is the earliest one's.
    op.execute(
        """
        INSERT INTO subscribers (id, email, confirmed_at, created_at)
        SELECT DISTINCT ON (lower(email)) gen_random_uuid(), email, created_at, created_at
        FROM subscriptions
        ORDER BY lower(email), created_at, id
        """
    )

    # --- 2. subscriptions.subscriber_id replaces subscriptions.email ---
    op.add_column("subscriptions", sa.Column("subscriber_id", sa.UUID(as_uuid=False), nullable=True))
    op.execute(
        """
        UPDATE subscriptions s SET subscriber_id = u.id
        FROM subscribers u WHERE lower(u.email) = lower(s.email)
        """
    )
    # "Me@x" and "me@x" on one topic were two rows; now they would be one
    # subscriber twice. Keep the oldest.
    op.execute(
        """
        DELETE FROM subscriptions s USING subscriptions keep
        WHERE s.topic_id = keep.topic_id AND s.subscriber_id = keep.subscriber_id
          AND (keep.created_at, keep.id) < (s.created_at, s.id)
        """
    )
    op.alter_column("subscriptions", "subscriber_id", nullable=False)
    op.create_foreign_key(
        "subscriptions_subscriber_id_fkey",
        "subscriptions",
        "subscribers",
        ["subscriber_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_subscriptions_subscriber_id", "subscriptions", ["subscriber_id"])
    op.drop_constraint("uq_subscription_topic_email", "subscriptions", type_="unique")
    op.drop_column("subscriptions", "email")
    op.add_column("subscriptions", sa.Column("label", sa.String(200), nullable=True))
    op.add_column(
        "subscriptions", sa.Column("waitlisted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_subscription_topic_subscriber", "subscriptions", ["topic_id", "subscriber_id"]
    )

    # --- 3. topics.normalized_query, merging topics that now collide ---
    op.add_column("topics", sa.Column("normalized_query", sa.String(200), nullable=True))
    topics = bind.execute(
        sa.text("SELECT id, query, last_checked_at FROM topics ORDER BY created_at, id")
    ).all()
    groups: dict[str, list] = defaultdict(list)
    for topic in topics:
        groups[normalize_query(topic.query)].append(topic)

    for normalized, group in groups.items():
        keeper, duplicates = group[0], group[1:]  # oldest first
        for dup in duplicates:
            ids = {"keep": keeper.id, "dup": dup.id}
            # A subscriber already on the keeper keeps that subscription; their
            # one on the duplicate goes with the duplicate's delete below. It
            # stays active if either was, and resumes from the earlier send so
            # nothing either one hadn't been sent yet is skipped.
            bind.execute(
                sa.text(
                    """
                    UPDATE subscriptions k
                    SET active = k.active OR d.active,
                        last_sent_at = least(k.last_sent_at, d.last_sent_at)
                    FROM subscriptions d
                    WHERE k.topic_id = :keep AND d.topic_id = :dup
                      AND d.subscriber_id = k.subscriber_id
                    """
                ),
                ids,
            )
            bind.execute(
                sa.text(
                    """
                    UPDATE subscriptions SET topic_id = :keep
                    WHERE topic_id = :dup AND subscriber_id NOT IN (
                        SELECT subscriber_id FROM subscriptions WHERE topic_id = :keep
                    )
                    """
                ),
                ids,
            )
            bind.execute(sa.text("UPDATE digests SET topic_id = :keep WHERE topic_id = :dup"), ids)
            # A paper on both keeps the keeper's ratings.
            bind.execute(
                sa.text(
                    """
                    INSERT INTO topic_paper
                        (topic_id, paper_id, relevance, reference_relevance, reference_labeler)
                    SELECT :keep, paper_id, relevance, reference_relevance, reference_labeler
                    FROM topic_paper WHERE topic_id = :dup
                    ON CONFLICT (topic_id, paper_id) DO NOTHING
                    """
                ),
                ids,
            )
            # The oldest duplicate's description fills a keeper that has none.
            bind.execute(
                sa.text(
                    """
                    UPDATE topics SET description = (SELECT description FROM topics WHERE id = :dup)
                    WHERE id = :keep AND description IS NULL
                    """
                ),
                ids,
            )
            bind.execute(sa.text("DELETE FROM topics WHERE id = :dup"), ids)
        # The keeper now stands for every member's papers, so resume ingestion
        # from the earliest watermark: nothing a member hadn't fetched yet is
        # skipped. NULL (never run) members fetched nothing, so they don't count.
        watermarks = [t.last_checked_at for t in group if t.last_checked_at is not None]
        bind.execute(
            sa.text(
                "UPDATE topics SET normalized_query = :normalized, last_checked_at = :watermark "
                "WHERE id = :keep"
            ),
            {
                "normalized": normalized,
                "watermark": min(watermarks) if watermarks else None,
                "keep": keeper.id,
            },
        )

    # --- 4. topics are unique by normalized query, no longer by name ---
    op.alter_column("topics", "normalized_query", nullable=False)
    op.create_unique_constraint("uq_topics_normalized_query", "topics", ["normalized_query"])
    # Created unnamed by the init migration, so Postgres chose the name.
    name_constraint = next(
        uc["name"]
        for uc in sa.inspect(bind).get_unique_constraints("topics")
        if uc["column_names"] == ["name"]
    )
    op.drop_constraint(name_constraint, "topics", type_="unique")
    op.alter_column(
        "topics",
        "name",
        existing_type=sa.String(length=100),
        type_=sa.String(length=200),
        existing_nullable=False,
    )

    # --- 5. one cadence, and a cap on every subscription ---
    op.execute("UPDATE subscriptions SET cadence = 'twice_weekly'")
    op.execute("UPDATE subscriptions SET max_papers = 10 WHERE max_papers IS NULL")


def downgrade() -> None:
    # Merged topics, cadence and max_papers are not restored: the merged-away
    # rows are gone, and the old values are not kept anywhere. Topics that
    # share a name are renamed (below) so the old unique name fits again.
    #
    # The old code emails every active subscription, so first switch off any
    # that the new code wouldn't email: the person never confirmed, has
    # unsubscribed or is waitlisted, or the subscription itself is waitlisted.
    op.execute(
        """
        UPDATE subscriptions s SET active = false
        FROM subscribers u
        WHERE u.id = s.subscriber_id
          AND (s.waitlisted_at IS NOT NULL OR u.confirmed_at IS NULL
               OR u.unsubscribed_at IS NOT NULL OR u.waitlisted_at IS NOT NULL)
        """
    )
    op.add_column("subscriptions", sa.Column("email", sa.String(), nullable=True))
    op.execute(
        "UPDATE subscriptions s SET email = u.email FROM subscribers u WHERE u.id = s.subscriber_id"
    )
    op.alter_column("subscriptions", "email", nullable=False)
    op.drop_constraint("uq_subscription_topic_subscriber", "subscriptions", type_="unique")
    op.create_unique_constraint(
        "uq_subscription_topic_email", "subscriptions", ["topic_id", "email"]
    )
    op.drop_index("ix_subscriptions_subscriber_id", table_name="subscriptions")
    op.drop_constraint("subscriptions_subscriber_id_fkey", "subscriptions", type_="foreignkey")
    op.drop_column("subscriptions", "subscriber_id")
    op.drop_column("subscriptions", "label")
    op.drop_column("subscriptions", "waitlisted_at")
    op.drop_index("uq_subscribers_email_lower", table_name="subscribers")
    op.drop_table("subscribers")

    op.drop_constraint("uq_topics_normalized_query", "topics", type_="unique")
    op.drop_column("topics", "normalized_query")
    # Names stopped being unique, and truncating to 100 can make more
    # collide. The oldest topic keeps its name; the others get their id
    # appended (63 + 1 + 36 = 100 characters), or the constraint below fails.
    op.execute(
        """
        UPDATE topics t SET name = left(t.name, 63) || ' ' || t.id::text
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY left(name, 100) ORDER BY created_at, id
            ) AS n
            FROM topics
        ) ranked
        WHERE ranked.id = t.id AND ranked.n > 1
        """
    )
    op.alter_column(
        "topics",
        "name",
        existing_type=sa.String(length=200),
        type_=sa.String(length=100),
        existing_nullable=False,
        postgresql_using="left(name, 100)",
    )
    # Postgres' own name for the original, unnamed constraint.
    op.create_unique_constraint("topics_name_key", "topics", ["name"])
