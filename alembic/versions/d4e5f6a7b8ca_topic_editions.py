"""topic_editions: one stored send per topic and send slot, with chained windows

Revision ID: d4e5f6a7b8ca
Revises: c3d4e5f6a7b9
Create Date: 2026-11-12 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8ca"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE topic_editions (
            id           uuid        PRIMARY KEY,
            topic_id     uuid        NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
            slot         timestamptz NOT NULL,
            window_start timestamptz NOT NULL,
            window_end   timestamptz NOT NULL,
            status       varchar(16) NOT NULL,
            overview     text,
            k            integer     NOT NULL,
            prepared_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_topic_editions_topic_slot UNIQUE (topic_id, slot),
            CONSTRAINT ck_topic_editions_status CHECK (status IN ('ready', 'fallback'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE edition_papers (
            edition_id uuid    NOT NULL REFERENCES topic_editions (id) ON DELETE CASCADE,
            paper_id   uuid    NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
            rank       integer NOT NULL,
            PRIMARY KEY (edition_id, paper_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE edition_papers")
    op.execute("DROP TABLE topic_editions")
