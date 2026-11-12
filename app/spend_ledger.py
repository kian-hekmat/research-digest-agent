"""The spend guard's memory: OpenAI usage in Postgres, month-to-date.

`DbLedger` implements app.services.spend.SpendLedger. Each call opens and
commits its own session, so a call's cost is recorded even when the activity
that made it fails afterwards - the money was spent either way.

A "month" is the UTC calendar month, the period OpenAI bills in.
docs/public-launch-plan.md §3.6.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.alerts import send_alert
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.services.spend import Usage, cost_usd

logger = logging.getLogger(__name__)

SpendState = Literal["ok", "soft", "hard"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def month_start(now: datetime) -> datetime:
    """The first instant of `now`'s UTC calendar month."""
    now = now.astimezone(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _month_key(now: datetime) -> str:
    return month_start(now).strftime("%Y-%m")


def month_to_date_usd(db: Session, now: datetime) -> Decimal:
    total = (
        db.query(func.coalesce(func.sum(models.LlmUsage.cost_usd), 0))
        .filter(models.LlmUsage.created_at >= month_start(now), models.LlmUsage.created_at <= now)
        .scalar()
    )
    return Decimal(total)


def _state_for(spent: Decimal, settings: Settings) -> SpendState:
    budget = Decimal(str(settings.openai_monthly_budget_usd))
    if spent >= budget:
        return "hard"
    if spent >= budget * Decimal(str(settings.spend_alert_fraction)):
        return "soft"
    return "ok"


def spend_state(db: Session, now: datetime, settings: Settings | None = None) -> SpendState:
    """"hard" at or over the monthly budget, "soft" at or over the alert line
    (new topics wait, from Phase 3), else "ok"."""
    return _state_for(month_to_date_usd(db, now), settings or get_settings())


class DbLedger:
    def __init__(
        self,
        settings: Settings | None = None,
        now: Callable[[], datetime] = utcnow,
        alert: Callable[..., bool] = send_alert,
    ) -> None:
        self._settings = settings or get_settings()
        self._now = now
        self._alert = alert

    def allow(self, purpose: str) -> bool:
        """Embeddings always run (they cost a fraction of a cent and ranking,
        previews and suggestions depend on them); chat calls stop at 100%."""
        if purpose == "embedding":
            return True
        db = SessionLocal()
        try:
            return spend_state(db, self._now(), self._settings) != "hard"
        finally:
            db.close()

    def record(self, *, model: str, purpose: str, topic_id: str | None, usage: Usage) -> None:
        now = self._now()
        db = SessionLocal()
        try:
            db.add(
                models.LlmUsage(
                    created_at=now,
                    model=model,
                    purpose=purpose,
                    topic_id=topic_id,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=cost_usd(model, usage, self._settings.openai_prices),
                    estimated=usage.estimated,
                )
            )
            db.commit()
            self._alert_on_thresholds(db, now)
        finally:
            db.close()

    def hard_limit_hit(self) -> None:
        now = self._now()
        db = SessionLocal()
        try:
            self._safe_alert(
                db,
                f"openai-hard-limit:{_month_key(now)}",
                "Research digest: OpenAI's spend limit was hit",
                "OpenAI refused a call because the project's own hard spend limit "
                "is reached. AI summaries are paused until it resets or is raised; "
                "digests still go out without AI text.",
            )
        finally:
            db.close()

    def _alert_on_thresholds(self, db: Session, now: datetime) -> None:
        spent = month_to_date_usd(db, now)
        state = _state_for(spent, self._settings)
        if state == "ok":
            return
        budget = self._settings.openai_monthly_budget_usd
        month = _month_key(now)
        pct = round(self._settings.spend_alert_fraction * 100)
        self._safe_alert(
            db,
            f"spend-80:{month}",
            f"Research digest: OpenAI spend at {pct}% of ${budget:g}",
            f"Month-to-date OpenAI spend is ${spent:.2f} of the ${budget:g} budget "
            f"({month}, UTC). New topics now wait until the 1st; existing topics run as usual.",
        )
        if state == "hard":
            self._safe_alert(
                db,
                f"spend-100:{month}",
                f"Research digest: OpenAI spend at 100% of ${budget:g}",
                f"Month-to-date OpenAI spend is ${spent:.2f}, at or over the ${budget:g} "
                f"budget ({month}, UTC). AI summaries are paused until the 1st; digests "
                "still go out without AI text.",
            )

    def _safe_alert(self, db: Session, key: str, subject: str, body: str) -> None:
        """A failed alert email must never fail the call that triggered it."""
        try:
            self._alert(db, key, subject, body, settings=self._settings)
        except Exception:
            logger.exception("Alert %r could not be sent", key)
