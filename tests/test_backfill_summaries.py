from datetime import datetime, timedelta, timezone

from app import crud, models
from app.backfill_summaries import backfill

LAST_SENT = datetime(2026, 9, 28, 13, 17, tzinfo=timezone.utc)


class RecordingSummarizer:
    def __init__(self, fail_titles=()):
        self.fail_titles = set(fail_titles)
        self.summarized: list[str] = []
        self.overviews: list[tuple[str, list[str]]] = []

    def summarize_paper(self, title, abstract):
        if title in self.fail_titles:
            raise RuntimeError("ollama hiccup")
        self.summarized.append(title)
        return f"Summary of {title}."

    def write_overview(self, topic_name, summaries):
        self.overviews.append((topic_name, summaries))
        return f"Overview of {topic_name}."


def _topic(db, name):
    topic = models.Topic(name=name, query=name.lower())
    db.add(topic)
    db.commit()
    return topic


def _digest(db, topic, generated_at, titles, *, summary=None, overview=None):
    digest = models.Digest(
        topic_id=topic.id,
        status=models.DigestStatus.completed,
        generated_at=generated_at,
        overview=overview,
    )
    db.add(digest)
    for title in titles:
        digest.papers.append(
            models.Paper(arxiv_id=f"{topic.name}-{title}", title=title, abstract="Abs.", summary=summary)
        )
    db.commit()
    return digest


def _subscribe(db, topic, *, active=True):
    sub = crud.create_subscription(db, topic.id, "me@example.com", models.SubscriptionCadence.twice_weekly)
    sub.last_sent_at = LAST_SENT
    sub.active = active
    db.commit()
    return sub


def test_backfills_only_content_not_yet_emailed(db_session):
    topic = _topic(db_session, "RLHF")
    _subscribe(db_session, topic)
    emailed = _digest(db_session, topic, LAST_SENT - timedelta(days=1), ["Old"])
    pending = _digest(db_session, topic, LAST_SENT + timedelta(days=1), ["New A", "New B"])
    fake = RecordingSummarizer()

    result = backfill(db_session, fake)

    assert sorted(fake.summarized) == ["New A", "New B"]
    assert result.summaries_written == 2
    assert result.overviews_written == 1
    db_session.expire_all()
    assert all(p.summary for p in db_session.get(models.Digest, pending.id).papers)
    assert db_session.get(models.Digest, pending.id).overview == "Overview of RLHF."
    assert db_session.get(models.Digest, emailed.id).papers[0].summary is None  # history untouched


def test_skips_papers_and_overviews_that_already_exist(db_session):
    topic = _topic(db_session, "RLHF")
    _subscribe(db_session, topic)
    _digest(
        db_session, topic, LAST_SENT + timedelta(days=1), ["Done"],
        summary="Existing.", overview="Existing overview.",
    )
    fake = RecordingSummarizer()

    result = backfill(db_session, fake)

    assert fake.summarized == [] and fake.overviews == []
    assert (result.summaries_written, result.overviews_written) == (0, 0)


def test_empty_digest_gets_no_overview(db_session):
    topic = _topic(db_session, "RLHF")
    _subscribe(db_session, topic)
    empty = _digest(db_session, topic, LAST_SENT + timedelta(days=1), [])

    backfill(db_session, RecordingSummarizer())

    db_session.expire_all()
    assert db_session.get(models.Digest, empty.id).overview is None


def test_one_failure_is_counted_skipped_and_retried_on_rerun(db_session):
    topic = _topic(db_session, "RLHF")
    _subscribe(db_session, topic)
    digest = _digest(db_session, topic, LAST_SENT + timedelta(days=1), ["Flaky", "Fine"])

    first = backfill(db_session, RecordingSummarizer(fail_titles={"Flaky"}))
    assert (first.summaries_written, first.failures) == (1, 1)

    second = backfill(db_session, RecordingSummarizer())
    assert (second.summaries_written, second.failures) == (1, 0)
    db_session.expire_all()
    assert all(p.summary for p in db_session.get(models.Digest, digest.id).papers)


def test_ignores_topics_nobody_is_actively_subscribed_to(db_session):
    unsubscribed = _topic(db_session, "PINNs")
    paused = _topic(db_session, "NA")
    _subscribe(db_session, paused, active=False)
    _digest(db_session, unsubscribed, LAST_SENT + timedelta(days=1), ["X"])
    _digest(db_session, paused, LAST_SENT + timedelta(days=1), ["Y"])
    fake = RecordingSummarizer()

    backfill(db_session, fake)

    assert fake.summarized == []
