"""Admin-only views for running the service. For now: what OpenAI cost this
month, by purpose and by topic - the numbers the topic cap is set from
(docs/public-launch-plan.md §1, "Cost model")."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.auth import require_admin
from app.config import get_settings
from app.database import get_db
from app.spend_ledger import month_start, spend_state

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    match = _MONTH.match(month)
    if not match:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mon = int(match[1]), int(match[2])
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def _money(value) -> float:
    return round(float(value or 0), 6)


@router.get("/usage", response_model=schemas.UsageReport)
def usage(
    month: str | None = Query(None, description="YYYY-MM (UTC); defaults to the current month"),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    current = month_start(now).strftime("%Y-%m")
    month = month or current
    start, end = _month_bounds(month)
    usage = models.LlmUsage
    in_month = (usage.created_at >= start, usage.created_at < end)

    by_purpose = {
        purpose: schemas.PurposeUsage(
            calls=calls, input_tokens=i, cached_input_tokens=c, output_tokens=o, cost_usd=_money(cost)
        )
        for purpose, calls, i, c, o, cost in db.query(
            usage.purpose,
            func.count(),
            func.sum(usage.input_tokens),
            func.sum(usage.cached_input_tokens),
            func.sum(usage.output_tokens),
            func.sum(usage.cost_usd),
        )
        .filter(*in_month)
        .group_by(usage.purpose)
    }

    total_cost = func.sum(usage.cost_usd)
    by_topic = [
        schemas.TopicUsage(topic_id=topic_id, topic_name=name, calls=calls, cost_usd=_money(cost))
        for topic_id, name, calls, cost in db.query(
            usage.topic_id, models.Topic.name, func.count(), total_cost
        )
        .outerjoin(models.Topic, models.Topic.id == usage.topic_id)
        .filter(*in_month)
        .group_by(usage.topic_id, models.Topic.name)
        .order_by(total_cost.desc(), models.Topic.name)
    ]

    return schemas.UsageReport(
        month=month,
        budget_usd=get_settings().openai_monthly_budget_usd,
        spent_usd=_money(sum(p.cost_usd for p in by_purpose.values())),
        state=spend_state(db, now) if month == current else None,
        by_purpose=by_purpose,
        by_topic=by_topic,
    )


# ---------- waitlist (phase 3a: admitted by hand) ----------
@router.get("/waitlist", response_model=schemas.WaitlistReport)
def waitlist(db: Session = Depends(get_db)):
    """Waitlisted subscribers, and waitlisted subscriptions of subscribers
    who aren't themselves waitlisted, oldest first."""
    subscribers = (
        db.query(models.Subscriber)
        .filter(models.Subscriber.waitlisted_at.is_not(None))
        .order_by(models.Subscriber.waitlisted_at)
        .all()
    )
    subscriptions = (
        db.query(models.Subscription)
        .join(models.Subscription.subscriber)
        .filter(models.Subscription.waitlisted_at.is_not(None), models.Subscriber.waitlisted_at.is_(None))
        .order_by(models.Subscription.waitlisted_at)
        .all()
    )
    return schemas.WaitlistReport(
        subscribers=[
            schemas.WaitlistedSubscriber(id=s.id, email=s.email, waitlisted_at=s.waitlisted_at)
            for s in subscribers
        ],
        subscriptions=[
            schemas.WaitlistedSubscription(
                id=s.id, subscriber_email=s.subscriber.email, topic=s.topic.name, waitlisted_at=s.waitlisted_at
            )
            for s in subscriptions
        ],
    )


def _admit(db: Session, model, row_id: str) -> Response:
    row = db.get(model, row_id) if crud._is_valid_uuid(row_id) else None
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    row.waitlisted_at = None
    db.commit()
    return Response(status_code=204)


@router.post("/subscribers/{subscriber_id}/admit", status_code=204)
def admit_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    """Let a waitlisted person in. The caps aren't re-checked: the owner's call."""
    return _admit(db, models.Subscriber, subscriber_id)


@router.post("/subscriptions/{subscription_id}/admit", status_code=204)
def admit_subscription(subscription_id: str, db: Session = Depends(get_db)):
    return _admit(db, models.Subscription, subscription_id)
