"""Account emails (confirmation and manage links): rendering, the retrying
workflow, and one send per form double-submit."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.account_emails import render_account_email, start_account_email
from app.temporal import activities as temporal_activities
from app.temporal.types import AccountEmailInput
from app.temporal.workflows import SendAccountEmailWorkflow
from tests.conftest import FakeTemporalClient
from tests.test_temporal_workflows import TASK_QUEUE, _reset_fakes, temporal_client  # noqa: F401


def test_render_escapes_url_and_names_kind():
    url = "http://testserver/confirm?token=a&b<c"
    c = render_account_email("confirm", url)
    assert c.subject == "Confirm your research digest subscription"
    assert url in c.text_body
    assert "a&amp;b&lt;c" in c.html_body and "b<c" not in c.html_body
    assert render_account_email("manage", "http://x/m").subject == "Your research digest manage link"


class FlakySender:
    attempts = 0
    sent: list = []

    def send(self, to, content):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("smtp hiccup")
        type(self).sent.append((to, content))


async def test_workflow_retries_a_failed_send(temporal_client, monkeypatch):  # noqa: F811
    FlakySender.attempts, FlakySender.sent = 0, []
    monkeypatch.setattr(temporal_activities, "EmailSender", FlakySender)

    await temporal_client.execute_workflow(
        SendAccountEmailWorkflow.run,
        AccountEmailInput(kind="confirm", to="new@example.com", url="http://testserver/confirm?token=t"),
        id="account-test", task_queue=TASK_QUEUE,
    )

    assert FlakySender.attempts == 2
    [(to, content)] = FlakySender.sent
    assert to == "new@example.com" and "http://testserver/confirm?token=t" in content.text_body


def test_same_bucket_starts_once():
    client = FakeTemporalClient()
    now = datetime(2026, 11, 16, 17, 3, tzinfo=timezone.utc)

    async def go():
        await start_account_email(client, "confirm", "sub-1", "a@example.com", "http://x", now)
        await start_account_email(client, "confirm", "sub-1", "a@example.com", "http://x", now + timedelta(minutes=5))
        await start_account_email(client, "manage", "sub-1", "a@example.com", "http://x", now)
        await start_account_email(client, "confirm", "sub-1", "a@example.com", "http://x", now + timedelta(minutes=10))

    asyncio.run(go())
    ids = [s[2] for s in client.started]
    assert ids == ["account-confirm-sub-1-20261116T170", "account-manage-sub-1-20261116T170",
                   "account-confirm-sub-1-20261116T171"]
