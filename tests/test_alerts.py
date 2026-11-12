"""send_alert: one email per key, never when no address is configured."""
import logging

import pytest

from app import models
from app.alerts import send_alert
from app.config import get_settings


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent = []
        self.fail = fail

    def send(self, to, content):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((to, content))


@pytest.fixture
def with_address(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_email", "owner@example.com")


def test_sends_once_per_key(db_session, with_address):
    sender = FakeSender()
    assert send_alert(db_session, "k:1", "Subject", "Body <b>", sender=sender) is True
    assert send_alert(db_session, "k:1", "Subject", "Body <b>", sender=sender) is False

    assert len(sender.sent) == 1
    to, content = sender.sent[0]
    assert to == "owner@example.com"
    assert content.subject == "Subject"
    assert content.text_body == "Body <b>"
    assert "&lt;b&gt;" in content.html_body  # escaped
    assert [a.key for a in db_session.query(models.SentAlert).all()] == ["k:1"]


def test_different_keys_both_send(db_session, with_address):
    sender = FakeSender()
    send_alert(db_session, "k:1", "S", "B", sender=sender)
    send_alert(db_session, "k:2", "S", "B", sender=sender)
    assert len(sender.sent) == 2


def test_no_alert_email_logs_and_records_nothing(db_session, caplog):
    sender = FakeSender()
    with caplog.at_level(logging.WARNING):
        assert send_alert(db_session, "k:1", "S", "B", sender=sender) is False
    assert sender.sent == []
    assert db_session.query(models.SentAlert).count() == 0
    assert "ALERT_EMAIL" in caplog.text


def test_failed_send_does_not_mark_alert_sent(db_session, with_address):
    with pytest.raises(OSError):
        send_alert(db_session, "k:1", "S", "B", sender=FakeSender(fail=True))
    assert db_session.query(models.SentAlert).count() == 0

    sender = FakeSender()
    assert send_alert(db_session, "k:1", "S", "B", sender=sender) is True
    assert len(sender.sent) == 1
