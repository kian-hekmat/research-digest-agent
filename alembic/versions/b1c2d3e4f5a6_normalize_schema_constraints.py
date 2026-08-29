"""normalize schema: digest_status enum, cascade FKs, tz-aware timestamps, updated_at, indexes

Revision ID: b1c2d3e4f5a6
Revises: eaed2624a49d
Create Date: 2026-08-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "eaed2624a49d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


digest_status = sa.Enum("pending", "completed", "failed", name="digest_status")

_TIMESTAMP_COLS = [
    ("topics", "created_at"),
    ("papers", "created_at"),
    ("digests", "generated_at"),
]

# (constraint name, table, referred table, local col, ondelete)
_FKS = [
    ("digests_topic_id_fkey", "digests", "topics", "topic_id"),
    ("topic_paper_topic_id_fkey", "topic_paper", "topics", "topic_id"),
    ("topic_paper_paper_id_fkey", "topic_paper", "papers", "paper_id"),
    ("digest_paper_digest_id_fkey", "digest_paper", "digests", "digest_id"),
    ("digest_paper_paper_id_fkey", "digest_paper", "papers", "paper_id"),
]


def upgrade() -> None:
    bind = op.get_bind()

    # --- digests.status -> native enum, NOT NULL, default 'pending' ---
    digest_status.create(bind, checkfirst=True)
    op.execute("UPDATE digests SET status = 'pending' WHERE status IS NULL")
    op.alter_column(
        "digests",
        "status",
        existing_type=sa.String(),
        type_=digest_status,
        postgresql_using="status::text::digest_status",
        nullable=False,
        server_default="pending",
    )
    op.create_index("ix_digests_status", "digests", ["status"])
    op.add_column("digests", sa.Column("error", sa.Text(), nullable=True))

    # --- topics.name: cap length at the DB, matching the API validation ---
    op.alter_column(
        "topics",
        "name",
        existing_type=sa.String(),
        type_=sa.String(length=100),
        existing_nullable=False,
    )

    # --- published_at: timezone-aware (stays nullable, no default) ---
    op.alter_column(
        "papers",
        "published_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
    )

    # --- timestamps: timezone-aware, NOT NULL, server default now() ---
    for table, col in _TIMESTAMP_COLS:
        op.execute(f"UPDATE {table} SET {col} = now() WHERE {col} IS NULL")
        op.alter_column(
            table,
            col,
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        )

    # --- updated_at on every mutable entity ---
    for table in ("topics", "papers", "digests"):
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # --- referential integrity: ON DELETE CASCADE everywhere ---
    for name, table, referred, col in _FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, referred, [col], ["id"], ondelete="CASCADE"
        )

    # --- FK / lookup indexes Postgres does not create automatically ---
    op.create_index("ix_digests_topic_id", "digests", ["topic_id"])
    op.create_index("ix_papers_published_at", "papers", ["published_at"])


def downgrade() -> None:
    op.drop_index("ix_papers_published_at", table_name="papers")
    op.drop_index("ix_digests_topic_id", table_name="digests")

    for name, table, referred, col in _FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, referred, [col], ["id"])

    for table in ("digests", "papers", "topics"):
        op.drop_column(table, "updated_at")

    for table, col in _TIMESTAMP_COLS:
        op.alter_column(
            table,
            col,
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            nullable=True,
            server_default=None,
        )

    op.alter_column(
        "papers",
        "published_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        existing_nullable=True,
    )
    op.alter_column(
        "topics",
        "name",
        existing_type=sa.String(length=100),
        type_=sa.String(),
        existing_nullable=False,
    )

    op.drop_column("digests", "error")
    op.drop_index("ix_digests_status", table_name="digests")
    op.alter_column(
        "digests",
        "status",
        existing_type=digest_status,
        type_=sa.String(),
        postgresql_using="status::text",
        nullable=True,
        server_default=None,
    )
    digest_status.drop(op.get_bind(), checkfirst=True)
