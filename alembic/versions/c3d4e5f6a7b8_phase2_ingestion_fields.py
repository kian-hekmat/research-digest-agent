"""phase 2 ingestion fields: topics.last_checked_at watermark, digests.overview

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-08-29 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-topic high-water mark for arXiv ingestion (nullable: NULL == never run).
    op.add_column(
        "topics",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # LLM-synthesized paragraph across a digest's papers.
    op.add_column("digests", sa.Column("overview", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("digests", "overview")
    op.drop_column("topics", "last_checked_at")
