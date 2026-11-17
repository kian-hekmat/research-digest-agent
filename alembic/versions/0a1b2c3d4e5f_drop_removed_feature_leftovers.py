"""drop what removed features left behind: papers.related_note,
papers.fulltext_status / fulltext_checked_at, topic_embeddings, and the
'preview' spend purpose

Revision ID: 0a1b2c3d4e5f
Revises: f0a1b2c3d4e5
Create Date: 2026-11-17 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PURPOSES = ("rating", "summary", "overview", "embedding")
_OLD_PURPOSES = _PURPOSES + ("preview",)


def _purpose_check(purposes) -> str:
    return "purpose IN (" + ", ".join(f"'{p}'" for p in purposes) + ")"


def upgrade() -> None:
    op.drop_column("papers", "related_note")
    op.drop_constraint("ck_papers_fulltext_status", "papers", type_="check")
    op.drop_column("papers", "fulltext_checked_at")
    op.drop_column("papers", "fulltext_status")
    op.execute("DROP TABLE topic_embeddings")
    op.drop_constraint("ck_llm_usage_purpose", "llm_usage", type_="check")
    op.create_check_constraint("ck_llm_usage_purpose", "llm_usage", _purpose_check(_PURPOSES))


def downgrade() -> None:
    op.drop_constraint("ck_llm_usage_purpose", "llm_usage", type_="check")
    op.create_check_constraint("ck_llm_usage_purpose", "llm_usage", _purpose_check(_OLD_PURPOSES))
    op.execute(
        """
        CREATE TABLE topic_embeddings (
            topic_id   uuid        NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
            model      varchar     NOT NULL,
            content    text        NOT NULL,
            embedding  vector(768) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (topic_id, model)
        )
        """
    )
    op.add_column("papers", sa.Column("fulltext_status", sa.String(16), nullable=True))
    op.add_column("papers", sa.Column("fulltext_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_papers_fulltext_status", "papers",
        "fulltext_status IN ('ok', 'unavailable', 'empty', 'error')",
    )
    op.add_column("papers", sa.Column("related_note", sa.Text(), nullable=True))
