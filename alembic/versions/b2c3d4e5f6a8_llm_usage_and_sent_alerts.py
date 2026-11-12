"""llm_usage (every OpenAI call's tokens and cost) and sent_alerts

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-11-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same list as app.models.LLM_USAGE_PURPOSES (migrations don't import app code).
_PURPOSES = ("rating", "summary", "overview", "embedding", "preview")


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column(
            "topic_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("topics.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint(
            "purpose IN (" + ", ".join(f"'{p}'" for p in _PURPOSES) + ")",
            name="ck_llm_usage_purpose",
        ),
    )
    # The spend guard sums the month on every call: created_at is the filter.
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])
    op.create_index("ix_llm_usage_topic_id", "llm_usage", ["topic_id"])

    op.create_table(
        "sent_alerts",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("sent_alerts")
    op.drop_index("ix_llm_usage_topic_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_table("llm_usage")
