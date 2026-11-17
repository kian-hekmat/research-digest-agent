"""LLM summarization through the OpenAI API.

Each call is one non-streaming request to OpenAI's `/chat/completions` over
plain HTTP (httpx). `Summarizer` can be faked in tests, and its HTTP client is
swappable via `http_client`.

Ingest rates each new paper with `rate_relevance`. Send prep summarizes each
topic's top papers with `summarize_and_rate` (storing only the summary) and
writes the topic's overview with `write_overview`. Both rating calls accept a
`RatingContext`: the topic's description in the reader's words.

With `SUMMARY_BACKEND=none` (`Settings.summaries_enabled` is False), every
method returns `None` (or a RatedSummary of Nones) immediately - no network
call, no retry/backoff, no error. This is a deliberate "no-summary mode":
digests and emails still work, just without AI-written text.

Spend (app.services.spend): given a `ledger`, every call is checked against
the monthly budget first and recorded, with its purpose and topic, as soon
as it's paid for. A call the budget doesn't allow - or one OpenAI refuses
because its own hard spend limit is reached - returns no text, exactly like
no-summary mode, instead of raising.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence

import logging

import httpx

from app.config import Settings, get_settings
from app.services.spend import (
    SpendLedger,
    Usage,
    cost_usd,
    is_spend_limit_error,
    usage_from_openai,
)

logger = logging.getLogger(__name__)

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

# The rate-only rubric audited in docs/rag-plan.md 5.10 (cross-field eval set).
# The combined summarize-and-rate prompt keeps _RELEVANCE_RUBRIC, the wording
# it was evaluated with.
_RELEVANCE_BANDS = "\n".join([
    "Rate how central the reader's topic is to the paper, from 1 to 10. Judge by what the paper contributes, not by whether the topic's words appear in it.",
    "10: the topic is the paper's main subject - new methods, theory, analysis or results for the topic itself.",
    "8-9: a major focus - the main contribution directly advances or applies the topic.",
    "6-7: one of several substantial parts that a reader of this topic would likely want to see.",
    "4-5: the topic is used as a tool, a dataset, an application domain or one component of a larger system, and the paper's contribution lies elsewhere.",
    "2-3: the topic appears only as motivation, background, an example or a point of comparison.",
    "1: mentioned only in passing, or the words mean something else here than in the reader's field.",
])

_RELEVANCE_SYSTEM = (
    "You rate academic papers for a research alerting digest. "
    + _RELEVANCE_BANDS
    + "\nReply with only the integer."
)

_OVERVIEW_SYSTEM = (
    "You write the opening paragraph of a research digest for one topic. Given "
    "the topic and a list of paper summaries, synthesize the batch in no more "
    "than 5-6 sentences: the common threads, any tensions or disagreements, and "
    "what stands out. Do not walk through the papers one by one. No preamble, "
    "no lists, no markdown."
)

# History used only to avoid repetition: the writer sees what earlier
# digests already said but may not mention or compare with them - a style
# that let it compare invented cross-references in 28% of overviews.
_OVERVIEW_AVOID_REPEATS_SYSTEM = (
    _OVERVIEW_SYSTEM
    + " You are also shown how this topic's previous digests opened. The reader "
    "has already read those, so do not repeat their general points (for "
    "example that the field pursues efficiency, accuracy and stability); say "
    "what is specific to this batch instead. Do not mention the previous "
    "digests, earlier papers, or how this batch compares with them. Do not "
    "refer to papers by number."
)
# Each previous overview is clipped so history can't crowd out the new batch.
_HISTORY_OVERVIEW_CHARS = 900


# Tolerant of what models actually emit: "Relevance: 7", "**Relevance:** 7",
# "relevance = 7/10".
_RELEVANCE_LINE = re.compile(r"^\W*relevance\W*[:=]?\W*(\d{1,2})\b.*$", re.IGNORECASE | re.MULTILINE)
_FIRST_INT = re.compile(r"\b(\d{1,2})\b")


@dataclass(frozen=True)
class RatingContext:
    """What the rater knows about the reader's topic beyond its name/query."""

    description: str | None = None

    def __bool__(self) -> bool:
        return bool(self.description)


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
    relevance = None
    if match is not None:
        relevance = _valid_relevance(match.group(1))
        text = text[: match.start()] + text[match.end() :]
    return RatedSummary(summary=text.strip() or None, relevance=relevance)


def parse_relevance(text: str) -> int | None:
    match = _FIRST_INT.search(text)
    return _valid_relevance(match.group(1)) if match else None


# ---------- the picks-first email (not wired in yet; docs/rag-plan.md §5.11) ----------
# The paper blurb: one per paper, shared by every topic that includes it, so
# it never names a topic and carries no rating (ingest rates).
_BLURB_SYSTEM = (
    "You write the entry for one academic paper in a research digest read by "
    "people who work in the paper's field. Reply in JSON with two fields. "
    "`hook`: one line, at most 20 words, on why this paper is worth a "
    "researcher's time - the specific result, method or claim that makes it "
    "stand out. `what`: one or two sentences, at most 40 words, on what the "
    "paper did and what it found, concretely, with at most three of the "
    "numbers the abstract gives, copied exactly. Sell through specifics, not "
    "adjectives: no 'novel', 'groundbreaking' or 'promising'. Never state "
    "more than the abstract supports: no results, comparisons or implications "
    "it does not give. Keep its hedges and conditions ('often', 'on synthetic "
    "data', 'in one case', 'to our knowledge'), and never credit a result to "
    "one component when the abstract does not isolate it. Do not open either "
    "field with 'The paper', 'This paper', 'This work' or 'The authors'. No "
    "LaTeX, no markdown, no preamble."
)
_BLURB_SCHEMA = {
    "type": "object",
    "properties": {"hook": {"type": "string"}, "what": {"type": "string"}},
    "required": ["hook", "what"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class PaperBlurb:
    hook: str
    what: str


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_SPACES = re.compile(r"\s+")


def _load_json_object(text: str) -> dict | None:
    """The JSON object in a structured reply, tolerating a ```json fence;
    None for anything else."""
    fenced = _FENCE.match(text or "")
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clean(value) -> str:
    return _SPACES.sub(" ", value).strip() if isinstance(value, str) else ""


def parse_paper_blurb(text: str) -> PaperBlurb | None:
    data = _load_json_object(text)
    if data is None:
        return None
    hook, what = _clean(data.get("hook")), _clean(data.get("what"))
    return PaperBlurb(hook, what) if hook and what else None


# ---------- the picks-first email: overview and picks ----------
_OVERVIEW_AND_PICKS_SYSTEM = (
    "You write one topic's section opener in a weekly research digest read by "
    "people who work in the field. You are given this period's papers, best "
    "first, each with its arXiv id in square brackets, its title and its "
    "abstract, and a pick pool of up to five ids. Reply in JSON with two "
    "fields. `overview`: 70-110 words on what this period's research found - "
    "concrete findings, methods and numbers, the threads that connect papers "
    "and any results that pull against each other. Open with a specific "
    "finding, not a verdict on the period as a whole: claim that a theme "
    "dominates, splits or connects the papers only when several of the "
    "abstracts given show it. Keep each paper's hedges, conditions and scope "
    "(synthetic data, a single benchmark or case, 'often'), copy counts and "
    "numbers exactly, state a comparison only when the abstract reports it, "
    "and describe each paper only as its abstract describes itself. Whenever you state a "
    "specific paper's finding, cite it right after with its id in square "
    "brackets, e.g. [2611.01234]; cite only ids you were given, and only for "
    "what that paper's abstract says. Do not walk through the papers one by "
    "one and do not refer to them by number. `picks`: zero to two papers from "
    "the pick pool that a researcher in this field should start with, each "
    "with `arxiv_id` and a one-line `hook` saying why it is worth reading "
    "first, held to the same rules as the overview; a pick need not be the "
    "first in the list. Sell through specifics, "
    "not adjectives, and never state more than the abstracts support. No "
    "LaTeX, no markdown, no preamble. You may also be shown how this topic's "
    "previous digests opened. The reader has already read those, so do not "
    "repeat their general points; say what is specific to this period. Do not "
    "mention the previous digests or how this period compares with them."
)
_OVERVIEW_AND_PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"arxiv_id": {"type": "string"}, "hook": {"type": "string"}},
                "required": ["arxiv_id", "hook"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "picks"],
    "additionalProperties": False,
}
_OVERVIEW_MAX_PAPERS = 25
_PICK_POOL = 5
_MAX_PICKS = 2
_OVERVIEW_ABSTRACT_CHARS = 1500
# An arXiv id with an optional `arXiv:` prefix and version suffix.
_ID = r"(?:arXiv:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?"
_CITATION = re.compile(rf"\b{_ID}\b", re.IGNORECASE)
_ID_ONLY = re.compile(rf"\s*{_ID}\s*", re.IGNORECASE)
# A bracket or parenthesis group of ids: "[A]", "[A, B]", "(arXiv:A; B)".
_ID_GROUP = re.compile(r"[\[(]([^\[\]()]*)[\])]")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?)\]])")
_EMPTY_GROUP = re.compile(r"[\[(]\s*[\])]")


def _plausible_arxiv_id(arxiv_id: str) -> bool:
    """New-style arXiv ids: YYMM with a real month from 0704 on, four digits
    after the dot until 1412 and five from 1501. Rules out prose numbers
    like 1024.5000 that only look like ids."""
    yymm, number = arxiv_id.split(".")
    month = int(yymm[2:])
    if not (1 <= month <= 12 and yymm >= "0704"):
        return False
    return len(number) == (5 if yymm >= "1501" else 4)


@dataclass(frozen=True)
class OverviewPaper:
    arxiv_id: str
    title: str
    abstract: str


@dataclass(frozen=True)
class Pick:
    arxiv_id: str
    hook: str


@dataclass
class OverviewAndPicks:
    overview: str
    picks: list[Pick]
    dropped_citations: list[str]  # ids the model cited that it wasn't given
    dropped_picks: list[str]  # picks outside the pick pool


def _check_citations(text: str, offered: set[str]) -> tuple[str, list[str]]:
    """Keep citations of offered papers and remove any other arXiv id.

    A bracket or parenthesis group made only of ids ("[A, B]", "(arXiv:C)")
    is rebuilt as "[offered ids]" or removed when none were offered. A bare
    id counts as a citation only if it has an arXiv id's shape (see
    `_plausible_arxiv_id`): offered ones stay, others are removed. Then the
    gaps removal left are tidied."""
    dropped: list[str] = []

    def drop(arxiv_id: str) -> None:
        if arxiv_id not in dropped:
            dropped.append(arxiv_id)

    def group(m: re.Match) -> str:
        parts = re.split(r"[,;]", m.group(1))
        ids = [_ID_ONLY.fullmatch(part) for part in parts]
        if not parts or not all(ids):
            return m.group(0)  # not a citation group: leave it to the bare pass
        kept = []
        for arxiv_id in (i.group(1) for i in ids):
            if arxiv_id in offered:
                if arxiv_id not in kept:
                    kept.append(arxiv_id)
            else:
                drop(arxiv_id)
        return f"[{', '.join(kept)}]" if kept else ""

    def bare(m: re.Match) -> str:
        arxiv_id = m.group(1)
        if arxiv_id in offered:
            return arxiv_id
        if not _plausible_arxiv_id(arxiv_id):
            return m.group(0)
        drop(arxiv_id)
        return ""

    # Rebuilt groups hold only offered ids, which the bare pass leaves as is.
    text = _CITATION.sub(bare, _ID_GROUP.sub(group, text))
    text = _EMPTY_GROUP.sub("", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", _SPACES.sub(" ", text)).strip()
    return text, dropped


def parse_overview_and_picks(
    text: str, offered_ids: list[str], top_ids: list[str]
) -> OverviewAndPicks | None:
    data = _load_json_object(text)
    if data is None or not _clean(data.get("overview")):
        return None
    overview, dropped_citations = _check_citations(_clean(data["overview"]), set(offered_ids))
    if not overview:
        return None
    pool = set(top_ids)
    picks: list[Pick] = []
    dropped_picks: list[str] = []
    for item in data.get("picks") or []:
        if not isinstance(item, dict):
            continue
        m = _ID_ONLY.fullmatch(_clean(item.get("arxiv_id")).strip("[]"))
        arxiv_id, hook = (m.group(1) if m else _clean(item.get("arxiv_id"))), _clean(item.get("hook"))
        if arxiv_id not in pool:
            dropped_picks.append(arxiv_id)
        elif hook and len(picks) < _MAX_PICKS and all(p.arxiv_id != arxiv_id for p in picks):
            picks.append(Pick(arxiv_id, hook))
    return OverviewAndPicks(overview, picks, dropped_citations, dropped_picks)


def _topic_line(topic_name: str, topic_query: str) -> str:
    # The query adds real information when it's a category (`cat:math.NA`) or
    # differs from the name; when it's the name again it's just noise.
    if topic_query.strip().strip('"').lower() == topic_name.strip().lower():
        return f"Reader's topic: {topic_name}"
    return f"Reader's topic: {topic_name} (arXiv search: {topic_query})"


def _topic_block(topic_name: str, topic_query: str, context: RatingContext | None) -> str:
    """The topic line, plus the reader's description when there is one.
    Without it the block is exactly `_topic_line`."""
    block = _topic_line(topic_name, topic_query)
    if context and context.description:
        block += f"\nWhat the reader wants from this topic: {' '.join(context.description.split())}"
    return block


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _JSON_FORMAT(name: str, schema: dict) -> dict:
    """OpenAI structured output for `schema`: strict JSON Schema, as
    gpt-5.4-mini accepts it on /chat/completions (probed 2026-11-16)."""
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


class Summarizer:
    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        *,
        ledger: SpendLedger | None = None,
        topic_id: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client
        # None: nothing recorded or budget-checked (tests, ad-hoc use). Every
        # production caller passes one - tests/test_spend_coverage.py.
        self._ledger = ledger
        self._topic_id = topic_id

    def summarize_and_rate(
        self,
        title: str,
        abstract: str,
        topic_name: str,
        topic_query: str,
        rating_context: RatingContext | None = None,
    ) -> RatedSummary:
        if not self._settings.summaries_enabled:
            return RatedSummary(summary=None, relevance=None)
        text = self._complete(
            system=_RATED_SUMMARY_SYSTEM,
            user=f"{_topic_block(topic_name, topic_query, rating_context)}\n\nTitle: {title}\n\nAbstract: {abstract}",
            max_tokens=420,
            purpose="summary",
        )
        if text is None:
            return RatedSummary(summary=None, relevance=None)
        return parse_rated_summary(text)

    def rate_relevance(
        self,
        title: str,
        abstract: str,
        topic_name: str,
        topic_query: str,
        rating_context: RatingContext | None = None,
    ) -> int | None:
        if not self._settings.summaries_enabled:
            return None
        text = self._complete(
            system=_RELEVANCE_SYSTEM,
            user=f"{_topic_block(topic_name, topic_query, rating_context)}\n\nTitle: {title}\n\nAbstract: {abstract}",
            max_tokens=10,
            purpose="rating",
        )
        return None if text is None else parse_relevance(text)

    def write_overview(
        self, topic_name: str, summaries: list[str], previous: Sequence[str] = ()
    ) -> str | None:
        """`previous`: the topic's earlier overviews, oldest first, shown
        only so they aren't repeated. Without them the prompt is the plain
        one."""
        if not self._settings.summaries_enabled:
            return None
        joined = "\n\n".join(f"{i}. {s}" for i, s in enumerate(summaries, 1))
        if not previous:
            return self._complete(
                system=_OVERVIEW_SYSTEM,
                user=f"Topic: {topic_name}\n\nPaper summaries:\n{joined}",
                max_tokens=500,
                purpose="overview",
            )
        said = "\n".join(f"- {_clip(text, _HISTORY_OVERVIEW_CHARS)}" for text in previous)
        return self._complete(
            system=_OVERVIEW_AVOID_REPEATS_SYSTEM,
            user=(
                f"Topic: {topic_name}\n\nWhat previous digests already said (do not repeat):\n"
                f"{said}\n\nPaper summaries:\n{joined}"
            ),
            max_tokens=500,
            purpose="overview",
        )

    def write_overview_and_picks(
        self, topic_name: str, papers: list[OverviewPaper], previous: list[str] = ()
    ) -> OverviewAndPicks | None:
        """The picks-first email's overview and picks for one topic's period. `papers`
        is best first: the first 25 are offered and the first 5 are the pick
        pool. `previous` (earlier overviews) is shown only so they aren't
        repeated. None in no-summary mode, when the budget stopped it,
        or the reply didn't parse."""
        if not self._settings.summaries_enabled or not papers:
            return None
        offered = list(papers)[:_OVERVIEW_MAX_PAPERS]
        pool = [p.arxiv_id for p in offered[:_PICK_POOL]]
        parts = [f"Topic: {topic_name}"]
        if previous:
            said = "\n".join(f"- {_clip(t, _HISTORY_OVERVIEW_CHARS)}" for t in previous)
            parts.append(f"What previous digests already said (do not repeat):\n{said}")
        listing = "\n\n".join(
            f"[{p.arxiv_id}] {p.title}\n{_clip(p.abstract, _OVERVIEW_ABSTRACT_CHARS)}".rstrip()
            for p in offered
        )
        parts.append(f"Papers this period, best first:\n{listing}")
        parts.append(f"Pick pool (choose picks only from these): {', '.join(pool)}")
        text = self._complete(
            system=_OVERVIEW_AND_PICKS_SYSTEM,
            user="\n\n".join(parts),
            max_tokens=700,
            purpose="overview",
            response_format=_JSON_FORMAT("overview_and_picks", _OVERVIEW_AND_PICKS_SCHEMA),
        )
        if text is None:
            return None
        return parse_overview_and_picks(text, [p.arxiv_id for p in offered], pool)

    def write_blurb(self, title: str, abstract: str) -> PaperBlurb | None:
        """The picks-first email's blurb (hook + what) for one paper, or None when the
        summaries are off, the budget stopped it, or the reply didn't parse."""
        if not self._settings.summaries_enabled:
            return None
        text = self._complete(
            system=_BLURB_SYSTEM,
            user=f"Title: {title}\n\nAbstract: {abstract}",
            max_tokens=300,
            purpose="summary",
            response_format=_JSON_FORMAT("paper_blurb", _BLURB_SCHEMA),
        )
        return parse_paper_blurb(text) if text is not None else None

    def _complete(
        self, *, system: str, user: str, max_tokens: int, purpose: str,
        response_format: dict | None = None,
    ) -> str | None:
        """One non-streaming chat call. Left to raise on failure (server
        down, bad key, rate limit, timeout) - the activity's retry policy
        governs attempts. None means the spend guard stopped the call:
        callers treat it like no-summary mode."""
        if not self._settings.openai_api_key:
            raise RuntimeError("SUMMARY_BACKEND=openai needs OPENAI_API_KEY to be set")
        model = self._settings.openai_model
        if self._ledger is not None:
            # A model without a price could never be recorded: refuse it
            # before spending anything.
            cost_usd(model, Usage(0, 0, 0), self._settings.openai_prices)
            if not self._ledger.allow(purpose):
                return None
        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(timeout=self._settings.openai_timeout)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
            # Reasoning tokens count against max_completion_tokens,
            # which is as low as 10 for a rating.
            "reasoning_effort": "none",
            "temperature": 0.2,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        try:
            resp = client.post(
                f"{self._settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
                json=payload,
            )
            if self._ledger is not None and is_spend_limit_error(resp):
                self._ledger.hard_limit_hit()
                return None
            resp.raise_for_status()
        finally:
            if owns_client:
                client.close()
        body = resp.json()
        if self._ledger is not None:
            # Recorded before parsing: a reply we can't use was still paid for.
            self._ledger.record(
                model=model, purpose=purpose, topic_id=self._topic_id,
                usage=self._usage(body, system, user, max_tokens),
            )
        return body["choices"][0]["message"]["content"].strip()

    @staticmethod
    def _usage(body: dict, system: str, user: str, max_tokens: int) -> Usage:
        try:
            return usage_from_openai(body)
        except ValueError:
            # OpenAI always reports usage on non-streaming calls; if it ever
            # doesn't, over-estimate rather than record nothing (~3 chars per
            # token in, the whole output allowance out).
            logger.warning("OpenAI response had no usage; recording an estimate")
            return Usage(len(system + user) // 3, 0, max_tokens, estimated=True)
