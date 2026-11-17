"""Signed links for the public pages.

Every link is an itsdangerous URL-safe timed token signed with
`settings.secret_key`, salted with its purpose so one kind can't stand in
for another:

- confirm      {"s": subscriber_id, "v": token_version}  48 hours
- manage       {"s": subscriber_id, "v": token_version}  no expiry
- unsubscribe  {"s": subscriber_id, "t": topic_id|None}  no expiry (None = all)
- csrf         {"n": random nonce}                       2 hours

Bumping a subscriber's `token_version` kills their confirm and manage links.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Literal

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings

CONFIRM_MAX_AGE = 48 * 3600
CSRF_MAX_AGE = 2 * 3600
RESUBSCRIBE_MAX_AGE = 3600


class TokenError(Exception):
    """A link that's tampered, expired, of the wrong kind, or stale."""


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    key = get_settings().secret_key
    if not key:
        raise TokenError("no secret key configured")
    return URLSafeTimedSerializer(key, salt=f"research-digest:{purpose}")


def make_token(purpose: str, payload: dict) -> str:
    return _serializer(purpose).dumps(payload)


def read_token(purpose: str, token: str, max_age: int | None) -> tuple[dict, datetime]:
    """The payload and when it was signed (UTC); TokenError otherwise."""
    try:
        data, signed_at = _serializer(purpose).loads(token, max_age=max_age, return_timestamp=True)
    except (BadData, TypeError, ValueError) as exc:
        raise TokenError(str(exc)) from exc
    if not isinstance(data, dict):
        raise TokenError("unexpected payload")
    if signed_at.tzinfo is None:
        signed_at = signed_at.replace(tzinfo=timezone.utc)
    return data, signed_at


def confirm_token(sub: models.Subscriber) -> str:
    return make_token("confirm", {"s": sub.id, "v": sub.token_version})


def manage_token(sub: models.Subscriber) -> str:
    return make_token("manage", {"s": sub.id, "v": sub.token_version})


def unsubscribe_token(subscriber_id: str, topic_id: str | None) -> str:
    return make_token("unsubscribe", {"s": subscriber_id, "t": topic_id})


def resubscribe_token(subscriber_id: str, topic_id: str | None) -> str:
    """Issued only on the "you're unsubscribed" page and good for an hour, so
    a digest's never-expiring unsubscribe link can't undo an opt-out."""
    return make_token("resubscribe", {"s": subscriber_id, "t": topic_id})


def csrf_token() -> str:
    return make_token("csrf", {"n": secrets.token_urlsafe(16)})


def csrf_ok(token: str | None) -> bool:
    if not token:
        return False
    try:
        read_token("csrf", token, max_age=CSRF_MAX_AGE)
    except TokenError:
        return False
    return True


def subscriber_for(
    db: Session, purpose: Literal["confirm", "manage"], token: str
) -> tuple[models.Subscriber, datetime]:
    """The subscriber a confirm or manage link belongs to, and when it was
    signed; TokenError if the subscriber is gone or the link is stale."""
    data, signed_at = read_token(purpose, token, CONFIRM_MAX_AGE if purpose == "confirm" else None)
    sub = db.get(models.Subscriber, data.get("s")) if isinstance(data.get("s"), str) else None
    if sub is None or sub.token_version != data.get("v"):
        raise TokenError("subscriber gone or link superseded")
    return sub, signed_at


def link(path: str, token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{path}?token={token}"
