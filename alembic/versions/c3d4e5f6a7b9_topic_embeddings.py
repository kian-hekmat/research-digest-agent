"""topic_embeddings: each topic's normalized query, embedded, per model

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-11-12 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Raw DDL, like paper_embeddings: the vector type lives in app.models and
    # migrations don't import the app. No vector index: topics number in the
    # hundreds at most, so a sequential scan is instant.
    op.execute(
        """
        CREATE TABLE topic_embeddings (
            topic_id   uuid        NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
            model      varchar     NOT NULL,
            content    text        NOT NULL,
            embedding  vector(768) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (topic_id, model)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE topic_embeddings")
