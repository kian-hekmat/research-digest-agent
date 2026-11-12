"""Prices, cost arithmetic and OpenAI response parsing for the spend guard
(app.services.spend). Pure functions: no database, no network."""
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.services.spend import (
    UnknownModelPrice,
    Usage,
    cost_usd,
    is_spend_limit_error,
    usage_from_openai,
)

PRICES = Settings().openai_prices


def test_default_prices_and_budget():
    s = Settings()
    assert s.openai_monthly_budget_usd == 20.0
    assert s.spend_alert_fraction == 0.8
    assert s.alert_email == ""
    assert PRICES["gpt-5.4-mini"] == {"input": 0.75, "cached_input": 0.075, "output": 4.50}
    assert PRICES["text-embedding-3-small"]["input"] == 0.02


def test_cost_splits_cached_input():
    u = Usage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=100_000)
    # 600k * 0.75 + 400k * 0.075 + 100k * 4.50, per 1M
    assert cost_usd("gpt-5.4-mini", u, PRICES) == Decimal("0.930000")


def test_tiny_call_keeps_precision():
    # 0.0003225 rounds half-up, not half-even
    assert cost_usd("gpt-5.4-mini", Usage(400, 0, 5), PRICES) == Decimal("0.000323")


def test_unknown_model_raises():
    with pytest.raises(UnknownModelPrice):
        cost_usd("gpt-9", Usage(1, 0, 1), PRICES)


def test_usage_from_openai_reads_cached_tokens():
    body = {
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 128},
        }
    }
    assert usage_from_openai(body) == Usage(500, 128, 40)


def test_usage_from_openai_without_details():
    assert usage_from_openai({"usage": {"prompt_tokens": 9, "completion_tokens": 1}}) == Usage(9, 0, 1)


def test_usage_from_openai_reads_embedding_usage():
    # Embedding responses report prompt_tokens only.
    assert usage_from_openai({"usage": {"prompt_tokens": 12, "total_tokens": 12}}) == Usage(12, 0, 0)


def test_usage_from_openai_missing_usage_raises():
    with pytest.raises(ValueError):
        usage_from_openai({"choices": []})


@pytest.mark.parametrize(
    "code,expected",
    [
        ("project_spend_limit_exceeded", True),
        ("organization_spend_limit_exceeded", True),
        ("rate_limit_exceeded", False),
    ],
)
def test_spend_limit_detection(code, expected):
    r = httpx.Response(429, json={"error": {"code": code, "message": "x"}})
    assert is_spend_limit_error(r) is expected


def test_spend_limit_detection_needs_a_429():
    r = httpx.Response(400, json={"error": {"code": "project_spend_limit_exceeded"}})
    assert is_spend_limit_error(r) is False


def test_spend_limit_detection_tolerates_non_json():
    assert is_spend_limit_error(httpx.Response(429, text="Too Many Requests")) is False


def test_prices_overridable_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_PRICES", '{"m": {"input": 1, "cached_input": 1, "output": 2}}')
    assert Settings().openai_prices == {"m": {"input": 1.0, "cached_input": 1.0, "output": 2.0}}
