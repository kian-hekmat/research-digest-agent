"""Account emails for the public pages: the confirmation link
and the manage link. Sent by SendAccountEmailWorkflow so a failed SMTP send
retries; started from the API's background task."""
from __future__ import annotations

from datetime import datetime
from html import escape

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.config import get_settings
from app.temporal.types import AccountEmailInput, DigestEmailContent

_COPY = {
    "confirm": (
        "Confirm your research digest subscription",
        "Someone (hopefully you) asked to subscribe this address to a weekly research digest. "
        "To confirm, open this link within 48 hours:",
        "If this wasn't you, ignore this email and nothing will be sent.",
    ),
    "manage": (
        "Your research digest manage link",
        "Here's the link to manage your research digest: edit your topics, pause, unsubscribe "
        "or delete your data:",
        "Keep it private: anyone with this link can change your subscription.",
    ),
}


def render_account_email(kind: str, url: str) -> DigestEmailContent:
    subject, lead, tail = _COPY[kind]
    text = f"{lead}\n\n{url}\n\n{tail}\n"
    html = (
        f"<p>{escape(lead)}</p>"
        f'<p><a href="{escape(url, quote=True)}">{escape(url)}</a></p>'
        f"<p>{escape(tail)}</p>"
    )
    return DigestEmailContent(subject=subject, text_body=text, html_body=html)


async def start_account_email(
    client, kind: str, subscriber_id: str, to: str, url: str, now: datetime
) -> None:
    """Start the send. The id is bucketed to 10 minutes so a double-submitted
    form sends once. The workflow is named, not imported, to keep this module
    clear of app.temporal.workflows (which imports the activities)."""
    bucket = f"{now:%Y%m%dT%H}{now.minute // 10}"
    try:
        await client.start_workflow(
            "SendAccountEmailWorkflow",
            AccountEmailInput(kind=kind, to=to, url=url),
            id=f"account-{kind}-{subscriber_id}-{bucket}",
            task_queue=get_settings().temporal_task_queue,
            # The default (allow duplicate) would start a second send once
            # the first finished - within a second of the first submit.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass
