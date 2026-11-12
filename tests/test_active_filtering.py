from datetime import datetime, timedelta, timezone

import pytest

from app import crud, models
from tests.conftest import make_subscriber, subscribe

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(days=30)


def _topic(db, name="t"):
    topic = models.Topic(name=name, query=name)
    db.add(topic)
    db.commit()
    db.refresh(topic)
    return topic


@pytest.mark.parametrize(
    "state",
    ["unconfirmed", "unsubscribed", "subscriber_waitlisted", "subscription_waitlisted", "inactive"],
)
def test_topic_with_only_an_ineligible_subscriber_is_not_fetched_or_emailed(db_session, state):
    topic = _topic(db_session)
    subscriber = make_subscriber(
        db_session,
        confirmed=state != "unconfirmed",
        unsubscribed=state == "unsubscribed",
        waitlisted=state == "subscriber_waitlisted",
    )
    subscribe(
        db_session,
        topic,
        subscriber,
        active=state != "inactive",
        waitlisted=state == "subscription_waitlisted",
        last_sent_at=LONG_AGO,
    )

    assert crud.list_active_topic_ids(db_session) == []
    assert crud.list_due_subscriptions(db_session, NOW) == []


def test_topic_shared_with_an_eligible_subscriber_is_fetched_but_only_they_are_emailed(db_session):
    topic = _topic(db_session)
    ok = make_subscriber(db_session, "confirmed@example.com")
    pending = make_subscriber(db_session, "pending@example.com", confirmed=False)
    subscribe(db_session, topic, ok, last_sent_at=LONG_AGO)
    subscribe(db_session, topic, pending, last_sent_at=LONG_AGO)

    assert crud.list_active_topic_ids(db_session) == [topic.id]
    assert [s.email for s in crud.list_due_subscriptions(db_session, NOW)] == [
        "confirmed@example.com"
    ]


def test_topic_with_no_subscriptions_is_not_fetched(db_session):
    _topic(db_session)
    assert crud.list_active_topic_ids(db_session) == []


def test_topic_with_two_eligible_subscribers_is_listed_once(db_session):
    topic = _topic(db_session)
    subscribe(db_session, topic, make_subscriber(db_session, "a@example.com"))
    subscribe(db_session, topic, make_subscriber(db_session, "b@example.com"))
    assert crud.list_active_topic_ids(db_session) == [topic.id]


def test_due_subscriptions_keep_oldest_first_order(db_session):
    topic = _topic(db_session)
    first = subscribe(db_session, topic, make_subscriber(db_session, "a@example.com"), last_sent_at=LONG_AGO)
    second = subscribe(db_session, topic, make_subscriber(db_session, "b@example.com"), last_sent_at=LONG_AGO)
    first.created_at = NOW - timedelta(days=2)
    second.created_at = NOW - timedelta(days=1)
    db_session.commit()
    assert [s.id for s in crud.list_due_subscriptions(db_session, NOW)] == [first.id, second.id]
