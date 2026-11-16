"""Rate limits for the public pages, stored in Postgres so they survive
restarts and hold across app workers (phase 3a). A limit is "at most N rows
for this key inside the window"; each allowed request adds a row."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import models


def allow(db: Session, key: str, limit: int, window: timedelta, now: datetime) -> bool:
    """Count this request against `key`; False once `limit` requests fall in
    the window ending at `now`. Prunes the key's older rows. Commits."""
    q = db.query(models.RateLimitEvent).filter(models.RateLimitEvent.key == key)
    q.filter(models.RateLimitEvent.created_at <= now - window).delete(synchronize_session=False)
    recent = q.filter(models.RateLimitEvent.created_at > now - window).count()
    allowed = recent < limit
    if allowed:
        db.add(models.RateLimitEvent(key=key, created_at=now))
    db.commit()
    return allowed


def signup_ip_key(ip: str) -> str:
    return f"signup:ip:{ip}"


def email_key(address: str) -> str:
    return f"email:{address.strip().lower()}"
