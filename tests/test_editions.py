from datetime import datetime, timedelta, timezone

from app import crud, models
from tests.conftest import make_subscriber, subscribe

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # Mon 08:00 EDT


def _topic(db, query="rlhf"):
    topic = models.Topic(name=query, query=query)
    db.add(topic)
    db.commit()
    return topic


def _digest(db, topic, completed_at, arxiv_ids):
    digest = models.Digest(
        topic_id=topic.id,
        status=models.DigestStatus.completed,
        generated_at=completed_at,
        completed_at=completed_at,
    )
    db.add(digest)
    db.commit()
    for arxiv_id in arxiv_ids:
        paper = db.query(models.Paper).filter_by(arxiv_id=arxiv_id).one_or_none()
        if paper is None:
            paper = models.Paper(arxiv_id=arxiv_id, title=arxiv_id)
            db.add(paper)
            db.commit()
        digest.papers.append(paper)
    db.commit()
    return digest


def _save(db, topic, slot, start, end, ranked=(), **kw):
    return crud.save_edition(
        db,
        topic_id=topic.id,
        slot=slot,
        window_start=start,
        window_end=end,
        status=kw.get("status", "ready"),
        k=kw.get("k", 5),
        overview=kw.get("overview"),
        ranked_paper_ids=list(ranked),
    )


def test_window_start_chains_from_the_previous_edition(db_session):
    topic = _topic(db_session)
    _save(db_session, topic, T0, T0 - timedelta(days=3), T0)
    later = T0 + timedelta(days=3)
    _save(db_session, topic, later, T0, later)
    # An earlier-slot edition saved last must not win: the latest window_end does.
    _save(db_session, topic, T0 - timedelta(days=3), T0 - timedelta(days=6), T0 - timedelta(days=3))
    assert crud.edition_window_start(db_session, topic.id, later + timedelta(days=4)) == later


def test_window_start_falls_back_to_earliest_active_last_sent(db_session):
    topic = _topic(db_session)
    early, late = T0 - timedelta(days=5), T0 - timedelta(days=2)
    subscribe(db_session, topic, make_subscriber(db_session, "a@example.com"), last_sent_at=late)
    subscribe(db_session, topic, make_subscriber(db_session, "b@example.com"), last_sent_at=early)
    # Inactive subscriptions are ignored, even with an older last_sent_at.
    subscribe(
        db_session, topic, make_subscriber(db_session, "c@example.com"),
        active=False, last_sent_at=T0 - timedelta(days=30),
    )
    assert crud.edition_window_start(db_session, topic.id, T0) == early


def test_a_previous_edition_beats_subscription_send_times(db_session):
    topic = _topic(db_session)
    subscribe(
        db_session, topic, make_subscriber(db_session, "a@example.com"),
        last_sent_at=T0 - timedelta(days=10),
    )
    _save(db_session, topic, T0, T0 - timedelta(days=3), T0)
    assert crud.edition_window_start(db_session, topic.id, T0 + timedelta(days=3)) == T0


def test_window_start_defaults_to_a_week(db_session):
    topic = _topic(db_session)
    assert crud.edition_window_start(db_session, topic.id, T0) == T0 - timedelta(days=7)


def test_window_papers_bounds_are_open_closed(db_session):
    topic = _topic(db_session)
    start, end = T0, T0 + timedelta(days=3)
    _digest(db_session, topic, start, ["at-start"])
    _digest(db_session, topic, start + timedelta(seconds=1), ["after-start"])
    _digest(db_session, topic, end, ["at-end"])
    _digest(db_session, topic, end + timedelta(seconds=1), ["after-end"])
    papers = crud.window_papers(db_session, topic.id, start, end)
    assert [p.arxiv_id for p in papers] == ["after-start", "at-end"]


def test_window_papers_dedupes_and_orders_by_arxiv_id(db_session):
    topic = _topic(db_session)
    other = _topic(db_session, "other")
    t = T0 + timedelta(hours=1)
    _digest(db_session, topic, t, ["2.b", "1.a"])
    _digest(db_session, topic, t + timedelta(hours=1), ["1.a", "3.c"])
    _digest(db_session, other, t, ["9.z"])
    papers = crud.window_papers(db_session, topic.id, T0, T0 + timedelta(days=1))
    assert [p.arxiv_id for p in papers] == ["1.a", "2.b", "3.c"]


def test_consecutive_windows_share_a_boundary_without_overlap(db_session):
    topic = _topic(db_session)
    boundary = T0 + timedelta(days=3)
    end2 = T0 + timedelta(days=7)
    _digest(db_session, topic, boundary - timedelta(seconds=1), ["before"])
    _digest(db_session, topic, boundary, ["on-boundary"])
    _digest(db_session, topic, boundary + timedelta(seconds=1), ["after"])

    first = _save(db_session, topic, boundary, T0, boundary)
    start2 = crud.edition_window_start(db_session, topic.id, end2)
    assert start2 == first.window_end

    w1 = {p.arxiv_id for p in crud.window_papers(db_session, topic.id, T0, boundary)}
    w2 = {p.arxiv_id for p in crud.window_papers(db_session, topic.id, start2, end2)}
    assert w1 == {"before", "on-boundary"}
    assert w2 == {"after"}
    assert not (w1 & w2)


def test_save_edition_is_idempotent_per_topic_and_slot(db_session):
    topic = _topic(db_session)
    p1 = _digest(db_session, topic, T0, ["1.a"]).papers[0]
    first = _save(db_session, topic, T0, T0 - timedelta(days=3), T0, [p1.id], overview="first", k=3)
    second = _save(db_session, topic, T0, T0 - timedelta(days=1), T0, [], overview="second", k=9,
                   status="fallback")
    assert second.id == first.id
    assert second.overview == "first"
    assert second.k == 3 and second.status == "ready"
    assert [p.id for p in second.papers] == [p1.id]
    assert db_session.query(models.TopicEdition).count() == 1
    assert crud.get_edition(db_session, topic.id, T0).id == first.id
    assert crud.get_edition(db_session, topic.id, T0 + timedelta(days=3)) is None


def test_edition_papers_keep_rank_order(db_session):
    topic = _topic(db_session)
    papers = _digest(db_session, topic, T0, ["1.a", "2.b", "3.c"]).papers
    by_id = {p.arxiv_id: p.id for p in papers}
    ranked = [by_id["3.c"], by_id["1.a"], by_id["2.b"]]
    edition = _save(db_session, topic, T0, T0 - timedelta(days=3), T0, ranked)
    db_session.expire_all()
    assert [p.arxiv_id for p in crud.get_edition(db_session, topic.id, T0).papers] == [
        "3.c", "1.a", "2.b"
    ]
    assert edition.id


def test_deleting_a_topic_deletes_its_editions(db_session):
    topic = _topic(db_session)
    paper = _digest(db_session, topic, T0, ["1.a"]).papers[0]
    _save(db_session, topic, T0, T0 - timedelta(days=3), T0, [paper.id])
    assert crud.delete_topic(db_session, topic.id)
    assert db_session.query(models.TopicEdition).count() == 0
    n = db_session.execute(models.edition_papers.select()).fetchall()
    assert n == []
    assert db_session.query(models.Paper).count() == 1
