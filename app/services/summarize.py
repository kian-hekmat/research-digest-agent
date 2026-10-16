"""LLM summarization with a local Gemma model (`gemma3:12b`) served by Ollama.

Runs entirely on the host machine: no API key and no per-call cost. Each call
is one non-streaming request to Ollama's `/api/chat` over plain HTTP (httpx).
`Summarizer` can be injected as a FastAPI dependency and faked in tests, and
its HTTP client is swappable via `http_client`.

The digest pipeline uses `summarize_and_rate`, which also rates the paper's
relevance to the topic (1-10, feeding app.services.ranking) in the same call,
and `rate_relevance` for a paper that already has a summary from another
topic. `summarize_paper` (summary only) remains for the backfill script.

Given `related` papers (earlier ones retrieved by embedding similarity, see
app.services.retrieval), `summarize_and_rate` may also return one sentence
on how the paper relates to them. Its citations are checked against the
papers actually offered: a note citing anything else is dropped, never shown.
Without `related`, the prompt is exactly the one used before retrieval.

With `SUMMARY_BACKEND=none` (`Settings.summaries_enabled` is False), every
method returns `None` (or a RatedSummary of Nones) immediately - no network
call, no retry/backoff, no error. This is a deliberate "no-summary mode":
digests and emails still work, just without AI-written text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

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

_RATED_SUMMARY_WITH_RELATED_SYSTEM = (
    _RATED_SUMMARY_SYSTEM
    + " You are also given earlier papers the reader has already seen, each "
    "with its arXiv id in square brackets. Only if the new paper tackles the "
    "same specific problem as one of them, or proposes a competing method for "
    "it - not merely the same field or the same general technique - add a "
    "final line `Related: ` followed by one sentence naming that shared problem "
    "or method and how the new paper differs, citing the earlier paper's arXiv "
    "id in square brackets, e.g. [2609.01234]. Never say the new paper builds "
    "on, extends, or follows an earlier one unless its abstract explicitly says "
    "so. Otherwise leave the line out; most papers should not have one. Never "
    "cite a paper that is not in the list. The summary paragraph describes "
    "only the new paper and never mentions the earlier ones."
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


# On its own line, or tacked onto the end of the paragraph after a sentence.
_RELATED_LINE = re.compile(
    r"(?:^|(?<=[.!?])[ \t]+)\W*related\W*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
# A note has to say something beyond its citations.
_MIN_NOTE_WORDS = 5
_ARXIV_ID = re.compile(r"\b\d{4}\.\d{4,5}\b")
# Each related paper's text in the prompt is capped, so 4 of them can't crowd
# out the paper being summarized.
_RELATED_TEXT_CHARS = 700

# Tolerant of what models actually emit: "Relevance: 7", "**Relevance:** 7",
# "relevance = 7/10".
_RELEVANCE_LINE = re.compile(r"^\W*relevance\W*[:=]?\W*(\d{1,2})\b.*$", re.IGNORECASE | re.MULTILINE)
_FIRST_INT = re.compile(r"\b(\d{1,2})\b")


@dataclass(frozen=True)
class RelatedPaper:
    """An earlier paper offered to the summarizer as possible related work."""

    arxiv_id: str
    title: str
    text: str  # its summary, or its abstract when it has none yet


@dataclass(frozen=True)
class RatedSummary:
    summary: str | None
    relevance: int | None  # 1-10; None when missing or unparseable
    # One sentence relating the paper to earlier ones, and the arXiv ids it
    # cites (all among the papers offered). None/() when there's no note.
    related_note: str | None = None
    related_ids: tuple[str, ...] = ()
    # The model wrote a Related line but it failed the citation check.
    related_rejected: bool = False


def _valid_relevance(raw: str) -> int | None:
    value = int(raw)
    return value if 1 <= value <= 10 else None


def parse_rated_summary(text: str, offered_ids: set[str] | None = None) -> RatedSummary:
    """Split a `Relevance: N` line off the summary. A reply without one keeps
    the whole text as the summary and an unknown relevance - a malformed
    rating must never cost the paper its summary.

    With `offered_ids` (related papers were in the prompt), a `Related:` line
    is split off too (on its own line or tacked onto the paragraph's end) and
    kept only if it cites at least one arXiv id, every id it cites was
    offered, and it says something beyond the citations. A failed check drops
    the note, never the summary."""
    match = _RELEVANCE_LINE.search(text)
    relevance = None
    if match is not None:
        relevance = _valid_relevance(match.group(1))
        text = text[: match.start()] + text[match.end() :]
    note, ids, rejected = None, (), False
    if offered_ids is not None:
        related = _RELATED_LINE.search(text)
        if related is not None:
            text = text[: related.start()] + text[related.end() :]
            cited = tuple(dict.fromkeys(_ARXIV_ID.findall(related.group(1))))
            words = len(re.findall(r"[A-Za-z]{2,}", _ARXIV_ID.sub("", related.group(1))))
            if cited and set(cited) <= offered_ids and words >= _MIN_NOTE_WORDS:
                note, ids = related.group(1), cited
            else:
                rejected = True
    return RatedSummary(
        summary=text.strip() or None,
        relevance=relevance,
        related_note=note,
        related_ids=ids,
        related_rejected=rejected,
    )


def parse_relevance(text: str) -> int | None:
    match = _FIRST_INT.search(text)
    return _valid_relevance(match.group(1)) if match else None


def _topic_line(topic_name: str, topic_query: str) -> str:
    # The query adds real information when it's a category (`cat:math.NA`) or
    # differs from the name; when it's the name again it's just noise.
    if topic_query.strip().strip('"').lower() == topic_name.strip().lower():
        return f"Reader's topic: {topic_name}"
    return f"Reader's topic: {topic_name} (arXiv search: {topic_query})"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


class Summarizer:
    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client

    def summarize_paper(self, title: str, abstract: str) -> str | None:
        if not self._settings.summaries_enabled:
            return None
        return self._complete(
            system=_PER_PAPER_SYSTEM,
            user=f"Title: {title}\n\nAbstract: {abstract}",
            max_tokens=400,
        )

    def summarize_and_rate(
        self,
        title: str,
        abstract: str,
        topic_name: str,
        topic_query: str,
        related: list[RelatedPaper] | None = None,
    ) -> RatedSummary:
        if not self._settings.summaries_enabled:
            return RatedSummary(summary=None, relevance=None)
        paper = f"Title: {title}\n\nAbstract: {abstract}"
        if not related:
            text = self._complete(
                system=_RATED_SUMMARY_SYSTEM,
                user=f"{_topic_line(topic_name, topic_query)}\n\n{paper}",
                max_tokens=420,
            )
            return parse_rated_summary(text)
        earlier = "\n".join(
            f"[{r.arxiv_id}] {r.title}: {_clip(r.text, _RELATED_TEXT_CHARS)}" for r in related
        )
        text = self._complete(
            system=_RATED_SUMMARY_WITH_RELATED_SYSTEM,
            user=(
                f"{_topic_line(topic_name, topic_query)}\n\n"
                f"Earlier papers the reader has seen:\n{earlier}\n\n"
                f"New paper:\n{paper}"
            ),
            max_tokens=500,
        )
        return parse_rated_summary(text, offered_ids={r.arxiv_id for r in related})

    def rate_relevance(
        self, title: str, abstract: str, topic_name: str, topic_query: str
    ) -> int | None:
        if not self._settings.summaries_enabled:
            return None
        text = self._complete(
            system=_RELEVANCE_SYSTEM,
            user=f"{_topic_line(topic_name, topic_query)}\n\nTitle: {title}\n\nAbstract: {abstract}",
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
            max_tokens=500,
        )

    def _complete(self, *, system: str, user: str, max_tokens: int) -> str:
        """One non-streaming /api/chat call. Left to raise on failure
        (server down, model not pulled, timeout) - the activity's retry
        policy governs attempts."""
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
