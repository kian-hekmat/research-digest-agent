"""ranking signals: per-topic relevance, author h-index, arXiv comment/journal-ref

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-06 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("comment", sa.Text(), nullable=True))
    op.add_column("papers", sa.Column("journal_ref", sa.Text(), nullable=True))
    op.add_column("papers", sa.Column("max_author_h_index", sa.Integer(), nullable=True))
    # Relevance is to a topic, not a property of the paper - it lives on the
    # topic<->paper link. Existing links stay NULL (unknown), which the
    # ranking imputes rather than treating as irrelevant.
    op.add_column("topic_paper", sa.Column("relevance", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "ck_topic_paper_relevance_range", "topic_paper", "relevance BETWEEN 1 AND 10"
    )


def downgrade() -> None:
    op.drop_constraint("ck_topic_paper_relevance_range", "topic_paper", type_="check")
    op.drop_column("topic_paper", "relevance")
    op.drop_column("papers", "max_author_h_index")
    op.drop_column("papers", "journal_ref")
    op.drop_column("papers", "comment")
