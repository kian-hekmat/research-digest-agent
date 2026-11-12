"""GET /admin/usage: a month's OpenAI spend by purpose and by topic."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import models
from app.spend_ledger import month_start


def _row(db, when, cost, purpose, topic_id=None, tokens=(100, 10, 20)):
    i, c, o = tokens
    db.add(
        models.LlmUsage(
            created_at=when, model="gpt-5.4-mini", purpose=purpose, topic_id=topic_id,
            input_tokens=i, cached_input_tokens=c, output_tokens=o, cost_usd=Decimal(cost),
        )
    )
    db.commit()


def _topic(db, name, query):
    t = models.Topic(name=name, query=query)
    db.add(t)
    db.commit()
    return t


def _seed(db):
    now = datetime.now(timezone.utc)
    last_month = month_start(now) - timedelta(days=3)
    rlhf, pinns = _topic(db, "RLHF", "rlhf"), _topic(db, "PINNs", "pinns")
    _row(db, now, "0.50", "summary", rlhf.id)
    _row(db, now, "0.25", "summary", pinns.id)
    _row(db, now, "0.10", "rating", rlhf.id)
    _row(db, now, "0.05", "overview", pinns.id)
    _row(db, now, "0.01", "rating", None)
    _row(db, last_month, "3.00", "summary", rlhf.id)
    return now, last_month, rlhf, pinns


def test_current_month_by_purpose_and_topic(client, db_session):
    now, _, rlhf, pinns = _seed(db_session)

    resp = client.get("/admin/usage")

    assert resp.status_code == 200
    body = resp.json()
    assert body["month"] == now.strftime("%Y-%m")
    assert body["budget_usd"] == 20.0
    assert body["spent_usd"] == 0.91
    assert body["state"] == "ok"
    assert body["by_purpose"]["summary"] == {
        "calls": 2, "input_tokens": 200, "cached_input_tokens": 20, "output_tokens": 40, "cost_usd": 0.75,
    }
    assert body["by_purpose"]["rating"]["calls"] == 2
    assert body["by_purpose"]["overview"]["cost_usd"] == 0.05
    assert body["by_topic"] == [
        {"topic_id": rlhf.id, "topic_name": "RLHF", "calls": 2, "cost_usd": 0.6},
        {"topic_id": pinns.id, "topic_name": "PINNs", "calls": 2, "cost_usd": 0.3},
        {"topic_id": None, "topic_name": None, "calls": 1, "cost_usd": 0.01},
    ]


def test_past_month_has_no_state(client, db_session):
    _, last_month, rlhf, _ = _seed(db_session)

    body = client.get(f"/admin/usage?month={last_month:%Y-%m}").json()

    assert body["spent_usd"] == 3.0
    assert body["state"] is None
    assert body["by_topic"] == [{"topic_id": rlhf.id, "topic_name": "RLHF", "calls": 1, "cost_usd": 3.0}]


def test_state_reflects_the_budget(client, db_session):
    _row(db_session, datetime.now(timezone.utc), "17", "summary")
    assert client.get("/admin/usage").json()["state"] == "soft"


def test_empty_month(client):
    body = client.get("/admin/usage?month=2020-01").json()
    assert body == {
        "month": "2020-01", "budget_usd": 20.0, "spent_usd": 0.0, "state": None,
        "by_purpose": {}, "by_topic": [],
    }


def test_invalid_month_is_422(client):
    assert client.get("/admin/usage?month=2026-13").status_code == 422
    assert client.get("/admin/usage?month=nov").status_code == 422


def test_requires_the_admin_token(anon_client):
    assert anon_client.get("/admin/usage").status_code == 401
