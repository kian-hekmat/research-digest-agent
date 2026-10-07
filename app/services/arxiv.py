"""arXiv Atom API client.

One function, `search_arxiv`, returning the most recently submitted papers for a
query. No DB or framework imports, so Phase 3 can call it straight from a
Temporal Activity.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx

from app.config import get_settings

_VERSION_SUFFIX = re.compile(r"v\d+$")  # 2408.12345v2 -> 2408.12345 (stable dedupe key)
_FIELD_PREFIX = re.compile(r"^(ti|au|abs|co|jr|cat|rn|id|all):")  # arXiv search fields
_last_request_at = 0.0


@dataclass(frozen=True)
class ArxivResult:
    arxiv_id: str
    title: str
    abstract: str
    published_at: datetime | None
    # Free-text author comment and journal reference; where "Accepted at
    # ..." notes live (see app.services.ranking.venue_score).
    comment: str | None = None
    journal_ref: str | None = None


def _clean(text: str | None) -> str | None:
    """Collapse arXiv's line-wrapping whitespace; empty -> None."""
    return (" ".join(text.split()) or None) if text else None


def _throttle(delay: float) -> None:
    """Space consecutive calls out by `delay` seconds. arXiv asks API clients
    not to hammer the endpoint. Best-effort and deliberately not thread-safe -
    the worst case is one slightly-too-early request."""
    global _last_request_at
    if delay > 0:
        wait = delay - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
    _last_request_at = time.monotonic()


def _parse_entry(entry) -> ArxivResult:
    raw_id = entry.get("id", "")
    arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id
    arxiv_id = _VERSION_SUFFIX.sub("", arxiv_id)

    published_at = None
    if entry.get("published_parsed"):
        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    return ArxivResult(
        arxiv_id=arxiv_id,
        # arXiv wraps title/summary text across lines; collapse whitespace.
        title=" ".join(entry.get("title", "").split()),
        abstract=" ".join(entry.get("summary", "").split()),
        published_at=published_at,
        comment=_clean(entry.get("arxiv_comment")),
        journal_ref=_clean(entry.get("arxiv_journal_ref")),
    )


def _build_search_query(query: str) -> str:
    """Wrap a free-text topic query as an exact-phrase search against arXiv's
    `all:` field.

    Sent unquoted, a multi-word query is tokenized and matched per-word -
    effectively OR'd - so e.g. "reinforcement learning from human feedback"
    pulls back any paper containing just "human" or "learning", with no floor
    on relevance, sorted purely by date. Quoting the phrase requires it to
    appear verbatim, which is what a topic name like that actually means.

    A query that already starts with an arXiv field prefix (e.g.
    `cat:math.NA`) is sent verbatim - that's the way to track a whole
    category, whose papers rarely contain the category's name as a phrase.
    """
    query = query.strip()
    if _FIELD_PREFIX.match(query):
        return query
    phrase = query.strip('"')
    return f'all:"{phrase}"'


def search_arxiv(
    query: str,
    max_results: int | None = None,
    since: datetime | None = None,
    *,
    delay: float | None = None,
    client: httpx.Client | None = None,
) -> list[ArxivResult]:
    """Return papers matching `query`, newest submission first.

    `since` drops entries published at or before that instant (the query API has
    no server-side date filter). `delay` and `client` are test injection points.
    """
    settings = get_settings()
    max_results = max_results or settings.arxiv_max_results
    delay = settings.arxiv_page_delay if delay is None else delay

    params = {
        "search_query": _build_search_query(query),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": max_results,
    }

    _throttle(delay)

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        resp = client.get(settings.arxiv_api_url, params=params)
        resp.raise_for_status()
    finally:
        if owns_client:
            client.close()

    feed = feedparser.parse(resp.text)
    results = [_parse_entry(e) for e in feed.entries]

    if since is not None:
        results = [
            r for r in results
            if r.published_at is None or r.published_at > since
        ]
    return results
