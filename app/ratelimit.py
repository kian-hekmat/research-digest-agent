"""Rate limits for the public pages, stored in Postgres so they survive
restarts and hold across app workers (phase 3a). A limit is "at most N rows
for this key inside the window"; each allowed request adds a row."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import models
from app.config import get_settings

# Rows older than this are pruned for every key: the longest window is a day.
_KEEP = timedelta(days=2)


def allow(db: Session, key: str, limit: int, window: timedelta, now: datetime) -> bool:
    """Count this request against `key`; False once `limit` requests fall in
    the window ending at `now`. Prunes the key's older rows. Commits."""
    db.query(models.RateLimitEvent).filter(
        models.RateLimitEvent.created_at <= now - max(window, _KEEP)
    ).delete(synchronize_session=False)
    q = db.query(models.RateLimitEvent).filter(models.RateLimitEvent.key == key)
    q.filter(models.RateLimitEvent.created_at <= now - window).delete(synchronize_session=False)
    recent = q.filter(models.RateLimitEvent.created_at > now - window).count()
    allowed = recent < limit
    if allowed:
        db.add(models.RateLimitEvent(key=key, created_at=now))
    db.commit()
    return allowed


def _digest(value: str) -> str:
    """Keys hold a keyed hash, never the address or IP itself, so "delete my
    data" leaves nothing readable behind."""
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]


def signup_ip_key(ip: str) -> str:
    return f"signup:ip:{_digest(ip)}"


def manage_link_ip_key(ip: str) -> str:
    return f"manage-link:ip:{_digest(ip)}"


def email_key(address: str) -> str:
    return f"email:{_digest(address.strip().lower())}"
