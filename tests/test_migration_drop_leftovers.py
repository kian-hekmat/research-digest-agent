"""Migration 0a1b2c3d4e5f drops what the removed features left behind:
papers.related_note (LLM related-work notes), papers.fulltext_status and
fulltext_checked_at (full text), the topic_embeddings table (similar-topic
suggestions) and the 'preview' spend purpose (the topic preview). The
db_session fixture runs every migration down again after each test, so the
downgrade is exercised too."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _columns(db, table):
    return {
        r[0] for r in db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table}
        )
    }


def test_unused_paper_columns_are_gone(db_session):
    assert not {"related_note", "fulltext_status", "fulltext_checked_at"} & _columns(db_session, "papers")


def test_topic_embeddings_table_is_gone(db_session):
    assert db_session.execute(text("SELECT to_regclass('topic_embeddings')")).scalar() is None


def test_preview_is_no_longer_a_spend_purpose(db_session):
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO llm_usage (model, purpose, input_tokens, cached_input_tokens, output_tokens, cost_usd) "
            "VALUES ('m', 'preview', 1, 0, 1, 0)"
        ))
        db_session.flush()
    db_session.rollback()
