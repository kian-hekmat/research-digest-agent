"""The send (`SendDigestEmailsWorkflow`) after Phase 2c: it assembles emails
from stored editions, with no LLM, arXiv or h-index call. A topic whose prep
never ran gets a fallback edition without AI text rather than being held
back. Every subscriber of a topic gets the same edition, cut to their own cap.
A topic with no edition is prepped by the send itself (or waited for, when
the scheduled prep is already on it); only a failed prep falls back.

Runs on the same time-skipping Temporal harness as test_temporal_workflows.py.
The slot is passed explicitly: the time-skipping server's clock can jump far
ahead between workflows.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app import crud, models
from app.cadence import latest_slot
from app.temporal import activities as temporal_activities
from app.temporal.types import PrepTopicInput
from app.temporal.workflows import (
    PrepTopicEditionWorkflow,
    SendDigestEmailsWorkflow,
    prep_workflow_id,
)
from tests.test_temporal_workflows import (  # noqa: F401 - fixtures used by name
    TASK_QUEUE,
    FakeEmailSender,
    FakeSummarizer,
    _make_topic,
    _reset_fakes,
    temporal_client,
)

NOW = datetime.now(timezone.utc)
SLOT = latest_slot(NOW)


def _sub(db_session, topic, email, *, last_sent_at=None, max_papers=None):
    sub = crud.create_subscription(
        db_session, topic.id, email, models.SubscriptionCadence.twice_weekly, max_papers
    )
    sub.last_sent_at = last_sent_at or NOW - timedelta(days=5)
    db_session.commit()
    return sub


def _papers(db_session, topic, n, *, completed_at, prefix="2611", summary=True):
    """n papers ingested for `topic` by one digest completed at `completed_at`."""
    digest = models.Digest(
        topic_id=topic.id, status=models.DigestStatus.completed, completed_at=completed_at
    )
    db_session.add(digest)
    papers = []
    for i in range(1, n + 1):
        paper = models.Paper(
            arxiv_id=f"{prefix}.{i:05d}", title=f"{prefix} P{i}",
            summary=f"Summary {prefix} {i}." if summary else None,
            published_at=completed_at - timedelta(minutes=i),
        )
        db_session.add(paper)
        topic.papers.append(paper)
        digest.papers.append(paper)
        papers.append(paper)
    db_session.commit()
    return papers


def _edition(db_session, topic, papers, *, start, end, overview="Stored overview.", slot=SLOT):
    return crud.save_edition(
        db_session, topic_id=topic.id, slot=slot, window_start=start, window_end=end,
        status="ready", k=5, overview=overview, ranked_paper_ids=[p.id for p in papers],
    )


async def _send(client, slot=SLOT) -> int:
    return await client.execute_workflow(
        SendDigestEmailsWorkflow.run, slot.isoformat(),
        id=f"send-test-{uuid.uuid4()}", task_queue=TASK_QUEUE,
    )


def _no_external_calls(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the send must not call this")

    monkeypatch.setattr(temporal_activities, "Summarizer", boom)
    monkeypatch.setattr(temporal_activities, "search_arxiv", boom)
    monkeypatch.setattr(temporal_activities, "fetch_max_author_h_index", boom)


async def test_send_uses_the_stored_edition_and_makes_no_llm_arxiv_or_hindex_calls(
    db_session, temporal_client, monkeypatch
):
    _no_external_calls(monkeypatch)
    topic = _make_topic(db_session)
    sub = _sub(db_session, topic, "me@example.com")
    p1, p2 = _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=2))
    _edition(db_session, topic, [p2, p1], start=NOW - timedelta(days=5), end=NOW - timedelta(days=1))

    assert await _send(temporal_client) == 1

    [(to, content)] = FakeEmailSender.sent
    assert to == "me@example.com"
    text = content.text_body
    assert "Stored overview." in text
    assert text.index("2611 P2") < text.index("2611 P1")  # the edition's order, not the scoring's
    db_session.expire_all()
    assert db_session.get(models.Subscription, sub.id).last_sent_at > NOW - timedelta(days=1)


async def test_a_topic_whose_prep_never_ran_is_prepped_by_the_send(db_session, temporal_client):
    """The laptop slept through 01:00: the send runs the topic's prep itself
    (same workflow id, so never twice) and the email keeps its AI text."""
    topic = _make_topic(db_session)
    _sub(db_session, topic, "me@example.com")
    _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=2), summary=False)

    assert await _send(temporal_client) == 1

    [(_, content)] = FakeEmailSender.sent
    assert "Overview of RLHF: 2 papers." in content.text_body
    assert "Summary of 2611 P1." in content.text_body
    db_session.expire_all()
    assert crud.get_edition(db_session, topic.id, SLOT).status == "ready"


async def test_the_send_waits_for_a_prep_already_running(db_session, temporal_client):
    topic = _make_topic(db_session)
    _sub(db_session, topic, "me@example.com")
    _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=2), summary=False)
    FakeSummarizer.delay = 1.0
    prep = await temporal_client.start_workflow(
        PrepTopicEditionWorkflow.run, PrepTopicInput(topic_id=topic.id, slot=SLOT),
        id=prep_workflow_id(topic.id, SLOT), task_queue=TASK_QUEUE,
    )

    assert await _send(temporal_client) == 1

    assert await prep.result() == "written"
    [(_, content)] = FakeEmailSender.sent
    assert "Overview of RLHF: 2 papers." in content.text_body
    assert len(FakeSummarizer.summarized) == 2  # prepped once, not twice


async def test_a_topic_whose_prep_fails_gets_a_fallback_without_ai_text(
    db_session, temporal_client, monkeypatch
):
    _no_external_calls(monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("prep is broken")

    monkeypatch.setattr(temporal_activities, "SummarizePaperInput", broken)  # plan_edition fails
    topic = _make_topic(db_session)
    _sub(db_session, topic, "me@example.com")
    _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=2), summary=False)

    assert await _send(temporal_client) == 1

    [(_, content)] = FakeEmailSender.sent
    assert "2611 P1" in content.text_body and "2611 P2" in content.text_body
    db_session.expire_all()
    edition = crud.get_edition(db_session, topic.id, SLOT)
    assert edition.status == "fallback"
    assert edition.overview is None
    assert len(edition.papers) == 2


async def test_an_uncapped_subscriber_gets_the_top_k(db_session, temporal_client):
    topic = _make_topic(db_session)
    _sub(db_session, topic, "me@example.com", max_papers=None)
    papers = _papers(db_session, topic, 8, completed_at=NOW - timedelta(days=2))
    _edition(db_session, topic, papers, start=NOW - timedelta(days=5), end=NOW - timedelta(days=1))  # k=5

    await _send(temporal_client)

    [(_, content)] = FakeEmailSender.sent
    assert content.text_body.count("https://arxiv.org/abs/") == 5


async def test_new_subscriber_mid_window_gets_the_whole_edition(db_session, temporal_client):
    topic = _make_topic(db_session)
    papers = _papers(db_session, topic, 3, completed_at=SLOT - timedelta(days=2))
    _edition(db_session, topic, papers, start=SLOT - timedelta(days=4), end=SLOT - timedelta(hours=7))
    # Confirmed after those papers were ingested, an hour before the slot.
    _sub(db_session, topic, "new@example.com", last_sent_at=SLOT - timedelta(hours=1))

    assert await _send(temporal_client) == 1

    [(_, content)] = FakeEmailSender.sent
    assert all(f"2611 P{i}" in content.text_body for i in (1, 2, 3))


async def test_failed_send_gets_the_current_edition_next_time_not_a_backlog(
    db_session, temporal_client
):
    topic = _make_topic(db_session)
    # Its last send failed, so last_sent_at is long ago.
    _sub(db_session, topic, "me@example.com", last_sent_at=NOW - timedelta(days=12))
    old = _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=8), prefix="2610")
    _edition(
        db_session, topic, old, start=NOW - timedelta(days=12), end=NOW - timedelta(days=6),
        slot=SLOT - timedelta(days=7),
    )
    current = _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=3), prefix="2611")
    _edition(db_session, topic, current, start=NOW - timedelta(days=6), end=NOW - timedelta(days=1))

    await _send(temporal_client)

    [(_, content)] = FakeEmailSender.sent
    assert "2611 P1" in content.text_body
    assert "2610 P1" not in content.text_body


async def test_slot_firing_twice_sends_once(db_session, temporal_client):
    topic = _make_topic(db_session)
    _sub(db_session, topic, "me@example.com")
    papers = _papers(db_session, topic, 2, completed_at=NOW - timedelta(days=2))
    _edition(db_session, topic, papers, start=NOW - timedelta(days=5), end=NOW - timedelta(days=1))

    assert await _send(temporal_client) == 1
    assert await _send(temporal_client) == 0

    assert len(FakeEmailSender.sent) == 1
    assert db_session.query(models.TopicEdition).count() == 1


async def test_each_subscriber_gets_their_own_cap_from_one_edition(db_session, temporal_client):
    topic = _make_topic(db_session)
    _sub(db_session, topic, "five@example.com", max_papers=5)
    _sub(db_session, topic, "ten@example.com", max_papers=10)
    papers = _papers(db_session, topic, 12, completed_at=NOW - timedelta(days=2))
    _edition(db_session, topic, papers, start=NOW - timedelta(days=5), end=NOW - timedelta(days=1))

    await _send(temporal_client)

    sent = dict(FakeEmailSender.sent)
    assert sent["five@example.com"].text_body.count("https://arxiv.org/abs/") == 5
    assert sent["ten@example.com"].text_body.count("https://arxiv.org/abs/") == 10
    # Both get the edition's best first.
    assert "2611 P1\n" in sent["five@example.com"].text_body


async def test_a_topic_whose_edition_has_no_papers_is_left_out_and_not_marked_sent(
    db_session, temporal_client
):
    busy = _make_topic(db_session, name="RLHF", query="rlhf")
    quiet = _make_topic(db_session, name="PINNs", query="pinns")
    busy_sub = _sub(db_session, busy, "me@example.com")
    quiet_sub = _sub(db_session, quiet, "me@example.com")
    quiet_before = quiet_sub.last_sent_at
    papers = _papers(db_session, busy, 1, completed_at=NOW - timedelta(days=2))
    _edition(db_session, busy, papers, start=NOW - timedelta(days=5), end=NOW - timedelta(days=1))

    await _send(temporal_client)

    [(_, content)] = FakeEmailSender.sent
    assert "PINNs" not in content.text_body
    db_session.expire_all()
    assert db_session.get(models.Subscription, busy_sub.id).last_sent_at > NOW - timedelta(days=1)
    assert db_session.get(models.Subscription, quiet_sub.id).last_sent_at == quiet_before
