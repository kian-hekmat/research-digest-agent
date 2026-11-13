"""The cross-field eval set and how the harness loads rows that aren't in the DB."""
import pytest

from app import models
from evals import rag

DIVERSE_LABELS = [
    10, 9, 5, 6, 10, 5, 8, 4, 7, 6, 6, 5, 10, 8, 3, 6, 9, 9, 2, 4, 2, 10, 10, 10,
    5, 9, 8, 3, 3, 9, 9, 9, 8, 10, 8, 8, 2, 10, 9, 9, 8, 5, 4, 5, 5, 5, 4,
]


def test_labels_diverse_is_complete():
    rows = rag.read_labels(rag.EVALS_DIR / "labels_diverse.csv")

    assert len(rows) == 47
    assert len({r["topic"] for r in rows}) == 8
    assert all(rag.parse_label(r["label"]) is not None for r in rows)
    assert {r["labeler"] for r in rows} == {"claude-opus-5-5"}
    assert all(r["abstract"].strip() for r in rows)
    assert [int(r["label"]) for r in rows] == DIVERSE_LABELS


def test_load_items_builds_rows_missing_from_the_db_from_the_csv(db_session):
    topic = models.Topic(name="PINNs", query="pinns")
    paper = models.Paper(arxiv_id="2609.00001", title="In DB", abstract="db abstract")
    db_session.add_all([topic, paper])
    db_session.flush()
    db_session.execute(
        models.topic_paper_association.insert().values(topic_id=topic.id, paper_id=paper.id, relevance=6)
    )
    db_session.commit()
    labels = [
        {"topic": "PINNs", "arxiv_id": "2609.00001", "title": "csv title", "abstract": "csv", "label": "8"},
        {"topic": "Protein folding", "arxiv_id": "W123", "title": "File only", "abstract": "From the csv.", "label": "4"},
    ]

    items = rag.load_items(db_session, labels)

    assert [i.arxiv_id for i in items] == ["2609.00001", "W123"]
    assert items[0].paper_id == paper.id and items[0].stored_relevance == 6
    extra = items[1]
    assert extra.paper_id == "" and extra.topic_id == ""
    assert extra.topic_name == extra.topic_query == "Protein folding"
    assert (extra.title, extra.abstract) == ("File only", "From the csv.")
    assert extra.stored_relevance is None and extra.label == 4


def test_run_refuses_non_baseline_variants_on_file_only_rows(tmp_path, monkeypatch):
    path = tmp_path / "labels_x.csv"
    rag.write_labels(
        [{"topic": "T", "arxiv_id": "W1", "title": "t", "abstract": "a", "label": "5",
          "labeler": "x", "notes": ""}],
        path,
    )
    calls = []

    class Boom:
        def __init__(self, *a, **k):
            calls.append("summarizer")

    class FakeSettings:
        summaries_enabled = True
        summary_model = "m"

    class FakeDb:
        def close(self):
            pass

    monkeypatch.setattr(rag, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(rag, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(rag, "load_items", lambda db, labels: [
        rag.EvalItem("", "T", "T", "", "W1", "t", "a", None, 5)])
    monkeypatch.setattr(rag, "Summarizer", Boom)
    args = rag.argparse.Namespace(variant="rag-related", repeats=1, summaries=0, labels=path)

    with pytest.raises(SystemExit, match="rag-related"):
        rag.cmd_run(args)
    assert calls == []
