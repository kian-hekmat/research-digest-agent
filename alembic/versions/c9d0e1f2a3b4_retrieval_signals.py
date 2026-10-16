"""retrieval signals: related-work notes and citations, novelty, near duplicates

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-16 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("related_note", sa.Text(), nullable=True))
    op.add_column("papers", sa.Column("prior_similarity", sa.Float(), nullable=True))
    op.add_column(
        "papers",
        sa.Column(
            "near_duplicate_of_id",
            UUID(as_uuid=False),
            sa.ForeignKey("papers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_table(
        "paper_related",
        sa.Column(
            "paper_id",
            UUID(as_uuid=False),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "related_paper_id",
            UUID(as_uuid=False),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.CheckConstraint("paper_id <> related_paper_id", name="ck_paper_related_not_self"),
    )


def downgrade() -> None:
    op.drop_table("paper_related")
    op.drop_column("papers", "near_duplicate_of_id")
    op.drop_column("papers", "prior_similarity")
    op.drop_column("papers", "related_note")
