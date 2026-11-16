"""The phase 2d email-prompts eval's pure parts: weekly windows rebuilt from
stored digests, the automatic overview checks, and the blind blurb pairs."""
from datetime import datetime, timedelta

from app import models
from app.cadence import PREP_LEAD, SEND_TIME_ZONE
from app.services.summarize import OverviewAndPicks, OverviewPaper
from evals.email_prompts import blind_pairs, overview_checks, weekly_windows

ET = SEND_TIME_ZONE


def _digest(db, topic, completed_at, arxiv_ids):
    digest = models.Digest(
        topic_id=topic.id, status=models.DigestStatus.completed,
        generated_at=completed_at, completed_at=completed_at,
    )
    db.add(digest)
    db.commit()
    for arxiv_id in arxiv_ids:
        paper = models.Paper(arxiv_id=arxiv_id, title=arxiv_id, abstract=f"About {arxiv_id}.")
        db.add(paper)
        digest.papers.append(paper)
    db.commit()


def test_weekly_windows_are_monday_to_monday_and_skip_small_ones(db_session):
    topic = models.Topic(name="PINNs", query="pinns", normalized_query="pinns")
    db_session.add(topic)
    db_session.commit()
    _digest(db_session, topic, datetime(2026, 11, 3, 21, 35, tzinfo=ET), ["2611.00001", "2611.00002", "2611.00003"])
    _digest(db_session, topic, datetime(2026, 11, 5, 21, 35, tzinfo=ET), ["2611.00004", "2611.00005", "2611.00006"])
    _digest(db_session, topic, datetime(2026, 11, 11, 21, 35, tzinfo=ET), ["2611.00007", "2611.00008"])

    windows = weekly_windows(db_session)

    assert len(windows) == 1
    [w] = windows
    mon_8 = datetime(2026, 11, 2, 8, tzinfo=ET)
    assert (w.start, w.end) == (mon_8 - PREP_LEAD, mon_8 + timedelta(days=7) - PREP_LEAD)
    assert w.topic_name == "PINNs" and w.topic_query == "pinns"
    assert len(w.paper_ids) == 6


def test_overview_checks_counts():
    overview = "We saw 37 gains [2611.00001]. " + " ".join(["word"] * 95)
    result = OverviewAndPicks(
        overview=overview, picks=[], dropped_citations=["2611.99999"], dropped_picks=["2611.00009"]
    )
    papers = [OverviewPaper("2611.00001", "A title", "An abstract with 12 results.")]

    assert overview_checks(result, papers) == {
        "words": 100, "dropped_citations": 1, "dropped_picks": 1, "unsupported_numbers": ["37"],
    }


def test_blind_pairs_is_seeded_and_keyed():
    items = [{"paper": f"p{i}", "old": f"o{i}", "new": f"n{i}"} for i in range(10)]

    blind, key = blind_pairs(items, seed=1)

    assert blind_pairs(items, seed=1) == (blind, key)
    assert all({"A", "B"} <= set(b) and not {"old", "new"} & set(b) for b in blind)
    assert set(key.values()) == {"A=new", "A=old"}
    for i, b in enumerate(blind):
        new_side = "A" if key[str(i)] == "A=new" else "B"
        assert b[new_side] == f"n{i}"
        assert b["paper"] == f"p{i}"
