"""LLM summarization, via a local Ollama model or the Anthropic API.

`Summarizer` wraps both backends so it can be injected as a FastAPI dependency
and faked in tests. Which one is used is `Settings.summary_backend`:

- "ollama" - a model served by Ollama on this machine (no per-call cost).
  Plain HTTP via httpx; the client is swappable via `http_client`.
- "anthropic" - the paid Anthropic API. The client is created lazily, so
  importing this module and constructing `Summarizer()` never touches the
  network or needs a key; it's swappable via `client`.

The digest pipeline uses `summarize_and_rate`, which also rates the paper's
relevance to the topic (1-10, feeding app.services.ranking) in the same call,
and `rate_relevance` for a paper that already has a summary from another
topic. `summarize_paper` (summary only) remains for the backfill script.

When summarization isn't configured (`Settings.summaries_enabled` is False),
every method returns `None` (or a RatedSummary of Nones) immediately - no network call, no retry/backoff, no
error. This is a deliberate "no-summary mode": digests and emails still work,
just without AI-written text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from anthropic import Anthropic

from app.config import Settings, get_settings

_PER_PAPER_SYSTEM = (
    "You summarize academic paper abstracts for a research alerting digest. "
    "Given a title and abstract, reply with a single paragraph of 3-4 sentences "
    "covering the problem, the approach, and the headline result. Be concrete "
    "and neutral. No preamble, no lists, no LaTeX, no markdown."
)

_RELEVANCE_RUBRIC = (
    "Rate how central the paper is to the reader's topic from 1 to 10: 10 = the "
    "topic is the paper's main subject; 5 = the topic is a substantial part of "
    "the paper but not its focus; 1 = the topic is mentioned only in passing."
)

_RATED_SUMMARY_SYSTEM = (
    "You summarize academic paper abstracts for a research alerting digest and "
    "rate their relevance to the reader's topic. "
    + _RELEVANCE_RUBRIC
    + " Reply in exactly this format: a first line `Relevance: N` with N an "
    "integer from 1 to 10, a blank line, then a single paragraph of 3-4 "
    "sentences covering the problem, the approach, and the headline result. "
    "The paragraph must not mention the reader's topic or the rating. Be "
    "concrete and neutral. No preamble, no lists, no LaTeX, no markdown."
)

_RELEVANCE_SYSTEM = (
    "You rate academic papers for a research alerting digest. "
    + _RELEVANCE_RUBRIC
    + " Reply with only the integer."
)

_OVERVIEW_SYSTEM = (
    "You write the opening paragraph of a research digest for one topic. Given "
    "the topic and a list of paper summaries, synthesize the batch in no more "
    "than 5-6 sentences: the common threads, any tensions or disagreements, and "
    "what stands out. Do not walk through the papers one by one. No preamble, "
    "no lists, no markdown."
)


# Tolerant of what models actually emit: "Relevance: 7", "**Relevance:** 7",
# "relevance = 7/10".
_RELEVANCE_LINE = re.compile(r"^\W*relevance\W*[:=]?\W*(\d{1,2})\b.*$", re.IGNORECASE | re.MULTILINE)
_FIRST_INT = re.compile(r"\b(\d{1,2})\b")


@dataclass(frozen=True)
class RatedSummary:
    summary: str | None
    relevance: int | None  # 1-10; None when missing or unparseable


def _valid_relevance(raw: str) -> int | None:
    value = int(raw)
    return value if 1 <= value <= 10 else None


def parse_rated_summary(text: str) -> RatedSummary:
    """Split a `Relevance: N` line off the summary. A reply without one keeps
    the whole text as the summary and an unknown relevance - a malformed
    rating must never cost the paper its summary."""
    match = _RELEVANCE_LINE.search(text)
    if match is None:
        return RatedSummary(summary=text.strip() or None, relevance=None)
    summary = (text[: match.start()] + text[match.end() :]).strip()
    return RatedSummary(summary=summary or None, relevance=_valid_relevance(match.group(1)))


def parse_relevance(text: str) -> int | None:
    match = _FIRST_INT.search(text)
    return _valid_relevance(match.group(1)) if match else None


def _topic_line(topic_name: str, topic_query: str) -> str:
    # The query adds real information when it's a category (`cat:math.NA`) or
    # differs from the name; when it's the name again it's just noise.
    if topic_query.strip().strip('"').lower() == topic_name.strip().lower():
        return f"Reader's topic: {topic_name}"
    return f"Reader's topic: {topic_name} (arXiv search: {topic_query})"


def _first_text(message) -> str:
    for block in message.content:
        if block.type == "text":
            return block.text.strip()
    return ""


class Summarizer:
    def __init__(
        self,
        client: Anthropic | None = None,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or get_settings()
        self._http_client = http_client

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic(api_key=self._settings.anthropic_api_key)
        return self._client

    def summarize_paper(self, title: str, abstract: str) -> str | None:
        if not self._settings.summaries_enabled:
            return None
        return self._complete(
            system=_PER_PAPER_SYSTEM,
            user=f"Title: {title}\n\nAbstract: {abstract}",
            anthropic_model=self._settings.summary_model,
            max_tokens=400,
        )

    def summarize_and_rate(
        self, title: str, abstract: str, topic_name: str, topic_query: str
    ) -> RatedSummary:
        if not self._settings.summaries_enabled:
            return RatedSummary(summary=None, relevance=None)
        text = self._complete(
            system=_RATED_SUMMARY_SYSTEM,
            user=f"{_topic_line(topic_name, topic_query)}\n\nTitle: {title}\n\nAbstract: {abstract}",
            anthropic_model=self._settings.summary_model,
            max_tokens=420,
        )
        return parse_rated_summary(text)

    def rate_relevance(
        self, title: str, abstract: str, topic_name: str, topic_query: str
    ) -> int | None:
        if not self._settings.summaries_enabled:
            return None
        text = self._complete(
            system=_RELEVANCE_SYSTEM,
            user=f"{_topic_line(topic_name, topic_query)}\n\nTitle: {title}\n\nAbstract: {abstract}",
            anthropic_model=self._settings.summary_model,
            max_tokens=10,
        )
        return parse_relevance(text)

    def write_overview(self, topic_name: str, summaries: list[str]) -> str | None:
        if not self._settings.summaries_enabled:
            return None
        joined = "\n\n".join(f"{i}. {s}" for i, s in enumerate(summaries, 1))
        return self._complete(
            system=_OVERVIEW_SYSTEM,
            user=f"Topic: {topic_name}\n\nPaper summaries:\n{joined}",
            anthropic_model=self._settings.overview_model,
            max_tokens=500,
        )

    def _complete(self, *, system: str, user: str, anthropic_model: str, max_tokens: int) -> str:
        if self._settings.summary_backend == "ollama":
            return self._complete_ollama(system, user, max_tokens)
        message = self.client.messages.create(
            model=anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return _first_text(message)

    def _complete_ollama(self, system: str, user: str, max_tokens: int) -> str:
        """One non-streaming /api/chat call. Left to raise on failure
        (server down, model not pulled, timeout) - the activity's retry
        policy governs attempts, same as for the Anthropic backend."""
        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(timeout=self._settings.ollama_timeout)
        try:
            resp = client.post(
                f"{self._settings.ollama_base_url.rstrip('/')}/api/chat",
                json={
                    "model": self._settings.ollama_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    # Low temperature: summaries should track the abstract,
                    # not get creative with it.
                    "options": {"num_predict": max_tokens, "temperature": 0.2},
                },
            )
            resp.raise_for_status()
        finally:
            if owns_client:
                client.close()
        return resp.json()["message"]["content"].strip()
