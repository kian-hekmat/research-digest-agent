"""add digests.completed_at - email delivery keys off completion, not start

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-06 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("digests", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_digests_completed_at", "digests", ["completed_at"])
    # Existing completed digests: updated_at is stamped by finalize_digest, the
    # last write a completed digest gets, so it's the best available record of
    # when it completed.
    op.execute("UPDATE digests SET completed_at = updated_at WHERE status = 'completed'")


def downgrade() -> None:
    op.drop_index("ix_digests_completed_at", table_name="digests")
    op.drop_column("digests", "completed_at")
