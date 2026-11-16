"""weekly cadence for every subscription (launch plan v2.2: Monday-only sends)

Revision ID: e5f6a7b8c9db
Revises: d4e5f6a7b8ca
Create Date: 2026-11-16 12:00:00.000000

The downgrade restores the twice_weekly server default only. Which rows were
twice_weekly isn't recorded, so they stay weekly.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9db"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8ca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE subscriptions SET cadence = 'weekly' WHERE cadence = 'twice_weekly'")
    op.execute("ALTER TABLE subscriptions ALTER COLUMN cadence SET DEFAULT 'weekly'")


def downgrade() -> None:
    op.execute("ALTER TABLE subscriptions ALTER COLUMN cadence SET DEFAULT 'twice_weekly'")
