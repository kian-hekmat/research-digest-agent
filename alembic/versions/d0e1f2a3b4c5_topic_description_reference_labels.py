"""topics.description and reference relevance labels on topic_paper

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-16 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("topic_paper", sa.Column("reference_relevance", sa.SmallInteger(), nullable=True))
    op.add_column("topic_paper", sa.Column("reference_labeler", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_topic_paper_reference_relevance_range",
        "topic_paper",
        "reference_relevance BETWEEN 1 AND 10",
    )


def downgrade() -> None:
    op.drop_constraint("ck_topic_paper_reference_relevance_range", "topic_paper", type_="check")
    op.drop_column("topic_paper", "reference_labeler")
    op.drop_column("topic_paper", "reference_relevance")
    op.drop_column("topics", "description")
