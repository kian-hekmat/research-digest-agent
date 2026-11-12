"""Admin-only views for running the service. For now: what OpenAI cost this
month, by purpose and by topic - the numbers the topic cap is set from
(docs/public-launch-plan.md §1, "Cost model")."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
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
