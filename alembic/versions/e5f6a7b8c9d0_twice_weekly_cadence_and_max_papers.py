"""add twice_weekly cadence (new default) and per-subscription max_papers

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-29 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres refuses to *use* a newly added enum value (e.g. as a column
    # default) in the same transaction that added it, so commit the ADD VALUE
    # on its own first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE subscription_cadence ADD VALUE IF NOT EXISTS 'twice_weekly'")
    op.alter_column("subscriptions", "cadence", server_default="twice_weekly")
    op.add_column("subscriptions", sa.Column("max_papers", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "max_papers")
    # Postgres can't drop a single enum value - rebuild the type without it,
    # folding any twice_weekly rows back to weekly first.
    op.execute("UPDATE subscriptions SET cadence = 'weekly' WHERE cadence = 'twice_weekly'")
    op.alter_column("subscriptions", "cadence", server_default=None)
    op.execute("ALTER TYPE subscription_cadence RENAME TO subscription_cadence_old")
    op.execute("CREATE TYPE subscription_cadence AS ENUM ('weekly', 'biweekly')")
    op.execute(
        "ALTER TABLE subscriptions ALTER COLUMN cadence TYPE subscription_cadence "
        "USING cadence::text::subscription_cadence"
    )
    op.execute("DROP TYPE subscription_cadence_old")
    op.alter_column("subscriptions", "cadence", server_default="weekly")
