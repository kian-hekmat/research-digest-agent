"""What an OpenAI call costs, and the contract for recording it.

Pure functions and types, no DB imports: `Summarizer` (and, from Phase 2b,
the OpenAI embedder) depend on the `SpendLedger` protocol, and
app.spend_ledger.DbLedger implements it against Postgres. Tests pass a fake
ledger, or none.

Prices are USD per 1M tokens from `Settings.openai_prices`. Cached input
tokens are part of the prompt-token count OpenAI reports, billed at the
cached rate instead of the full one.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

import httpx

PURPOSES = ("rating", "summary", "overview", "embedding", "preview")

# 429 error codes meaning OpenAI won't take more paid calls: a project's or
# organization's hard monthly spend limit is reached, or the account is out
# of credits. Retrying can't help until the limit resets or money is added.
_SPEND_LIMIT_CODES = {
    "project_spend_limit_exceeded",
    "organization_spend_limit_exceeded",
    "insufficient_quota",
}
_PER_MILLION = Decimal(1_000_000)
_MICRO_DOLLAR = Decimal("0.000001")


class UnknownModelPrice(RuntimeError):
    """The model has no entry in Settings.openai_prices."""

    def __init__(self, model: str) -> None:
        super().__init__(f"No price configured for model {model!r} (OPENAI_PRICES)")
        self.model = model


class BudgetExhausted(Exception):
    """The monthly budget is spent: chat calls are skipped until the 1st."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    # True when the response carried no usage and the tokens were estimated.
    estimated: bool = False


class SpendLedger(Protocol):
    def allow(self, purpose: str) -> bool:
        """Whether a call for `purpose` may be made now."""

    def record(self, *, model: str, purpose: str, topic_id: str | None, usage: Usage) -> None:
        """Record one call's usage. Called as soon as the call has been paid
        for, before its reply is parsed."""

    def hard_limit_hit(self) -> None:
        """OpenAI refused a call because its own spend limit is reached."""


def usage_from_openai(body: dict) -> Usage:
    """Token counts from a chat-completions or embeddings response body.
    Raises ValueError when the body has no `usage`."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("response has no usage")
    details = usage.get("prompt_tokens_details") or {}
    return Usage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        cached_input_tokens=int(details.get("cached_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def cost_usd(model: str, usage: Usage, prices: dict[str, dict[str, float]]) -> Decimal:
    """Dollar cost of one call, to the micro-dollar (half-up)."""
    price = prices.get(model)
    if price is None:
        raise UnknownModelPrice(model)
    uncached = usage.input_tokens - usage.cached_input_tokens
    total = (
        uncached * Decimal(str(price["input"]))
        + usage.cached_input_tokens * Decimal(str(price["cached_input"]))
        + usage.output_tokens * Decimal(str(price["output"]))
    ) / _PER_MILLION
    return total.quantize(_MICRO_DOLLAR, rounding=ROUND_HALF_UP)


def is_spend_limit_error(response: httpx.Response) -> bool:
    """True for OpenAI's 429 meaning "spend limit reached", as opposed to an
    ordinary rate limit (which is worth retrying)."""
    if response.status_code != 429:
        return False
    try:
        code = response.json().get("error", {}).get("code")
    except (ValueError, AttributeError):
        return False
    return code in _SPEND_LIMIT_CODES
