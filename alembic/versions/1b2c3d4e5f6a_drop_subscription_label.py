"""drop subscriptions.label: it was never shown anywhere, and the
redesigned signup and manage pages no longer ask for it

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-11-17 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "1b2c3d4e5f6a"
down_revision: Union[str, Sequence[str], None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("subscriptions", "label")


def downgrade() -> None:
    op.add_column("subscriptions", sa.Column("label", sa.String(200), nullable=True))
