"""add paper_embeddings (pgvector) for retrieval

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-16 14:00:00.000000

Needs the pgvector extension available on the server: the docker-compose and
CI databases use the pgvector/pgvector image for this.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Raw DDL rather than sa.Column: the vector type lives in app.models, and
    # migrations shouldn't import the app.
    op.execute(
        """
        CREATE TABLE paper_embeddings (
            paper_id   uuid        NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
            model      varchar     NOT NULL,
            kind       varchar     NOT NULL DEFAULT 'abstract',
            chunk_ix   integer     NOT NULL DEFAULT 0,
            content    text        NOT NULL,
            embedding  vector(768) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (paper_id, model, kind, chunk_ix)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_paper_embeddings_embedding ON paper_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE paper_embeddings")
    # The extension is left installed: dropping it needs it to be unused
    # database-wide, and it's harmless to keep.
