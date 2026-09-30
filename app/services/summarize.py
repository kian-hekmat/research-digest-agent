"""LLM summarization, via a local Ollama model or the Anthropic API.

`Summarizer` wraps both backends so it can be injected as a FastAPI dependency
and faked in tests. Which one is used is `Settings.summary_backend`:

- "ollama" - a model served by Ollama on this machine (no per-call cost).
  Plain HTTP via httpx; the client is swappable via `http_client`.
- "anthropic" - the paid Anthropic API. The client is created lazily, so
  importing this module and constructing `Summarizer()` never touches the
  network or needs a key; it's swappable via `client`.

When summarization isn't configured (`Settings.summaries_enabled` is False),
both methods return `None` immediately - no network call, no retry/backoff, no
error. This is a deliberate "no-summary mode": digests and emails still work,
just without AI-written text.
"""
from __future__ import annotations

import httpx
from anthropic import Anthropic

from app.config import Settings, get_settings

_PER_PAPER_SYSTEM = (
    "You summarize academic paper abstracts for a research alerting digest. "
    "Given a title and abstract, reply with a single paragraph of 3-4 sentences "
    "covering the problem, the approach, and the headline result. Be concrete "
    "and neutral. No preamble, no lists, no LaTeX, no markdown."
)

_OVERVIEW_SYSTEM = (
    "You write the opening paragraph of a research digest for one topic. Given "
    "the topic and a list of paper summaries, synthesize the batch in no more "
    "than 5-6 sentences: the common threads, any tensions or disagreements, and "
    "what stands out. Do not walk through the papers one by one. No preamble, "
    "no lists, no markdown."
)


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
