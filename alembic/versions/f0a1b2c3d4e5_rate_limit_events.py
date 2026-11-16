"""rate_limit_events: Postgres-backed limits for the public pages (phase 3a)

Revision ID: f0a1b2c3d4e5
Revises: e5f6a7b8c9db
Create Date: 2026-11-16 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("key", sa.String(320), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_rate_limit_events_key_created_at", "rate_limit_events", ["key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_events_key_created_at", table_name="rate_limit_events")
    op.drop_table("rate_limit_events")
