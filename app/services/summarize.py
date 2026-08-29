"""LLM summarization via the Anthropic SDK.

`Summarizer` wraps the client so it can be injected as a FastAPI dependency and
faked in tests. The Anthropic client is created lazily: importing this module
and constructing `Summarizer()` never touches the network or needs a key.
"""
from __future__ import annotations

from anthropic import Anthropic

from app.config import get_settings

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
    def __init__(self, client: Anthropic | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic(api_key=get_settings().anthropic_api_key)
        return self._client

    def summarize_paper(self, title: str, abstract: str) -> str:
        message = self.client.messages.create(
            model=get_settings().summary_model,
            max_tokens=400,
            system=_PER_PAPER_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Title: {title}\n\nAbstract: {abstract}",
                }
            ],
        )
        return _first_text(message)

    def write_overview(self, topic_name: str, summaries: list[str]) -> str:
        joined = "\n\n".join(f"{i}. {s}" for i, s in enumerate(summaries, 1))
        message = self.client.messages.create(
            model=get_settings().overview_model,
            max_tokens=500,
            system=_OVERVIEW_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Topic: {topic_name}\n\nPaper summaries:\n{joined}",
                }
            ],
        )
        return _first_text(message)
