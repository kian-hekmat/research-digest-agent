"""papers.fulltext_status / fulltext_checked_at for full-text retrieval

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-22 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("fulltext_status", sa.String(16), nullable=True))
    op.add_column("papers", sa.Column("fulltext_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_papers_fulltext_status",
        "papers",
        "fulltext_status IN ('ok', 'unavailable', 'empty', 'error')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_papers_fulltext_status", "papers", type_="check")
    op.drop_column("papers", "fulltext_checked_at")
    op.drop_column("papers", "fulltext_status")
