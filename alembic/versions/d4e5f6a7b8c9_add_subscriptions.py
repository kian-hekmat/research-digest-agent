"""add subscriptions table for weekly/biweekly email delivery

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-21 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


subscription_cadence = sa.Enum("weekly", "biweekly", name="subscription_cadence")


def upgrade() -> None:
    # op.create_table below creates the enum type itself (Postgres ENUM types
    # are auto-managed by SQLAlchemy when a column uses one) - an explicit
    # .create() call here would double-create it and fail.
    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            sa.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "topic_id",
            sa.UUID(as_uuid=False),
            sa.ForeignKey("topics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column(
            "cadence",
            subscription_cadence,
            nullable=False,
            server_default="weekly",
        ),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "last_sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("topic_id", "email", name="uq_subscription_topic_email"),
    )
    op.create_index("ix_subscriptions_topic_id", "subscriptions", ["topic_id"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_topic_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    subscription_cadence.drop(op.get_bind(), checkfirst=True)
