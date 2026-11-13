"""OpenAlex as a second paper source: a phrase topic's recent journal
articles, preprints from any server and conference papers.

One public search, `search_openalex`, returning `OpenAlexResult`s that carry
a source-neutral `key`: an arXiv work (DOI 10.48550/arXiv.<id>) is keyed by
its arXiv id, so it merges with the copy arXiv itself returns; anything else
is keyed `openalex:<W id>`.

- Only plain phrase topics are searched: an arXiv field query (`cat:math.NA`)
  means nothing to OpenAlex, so it's never sent.
- Kept: types article, preprint and conference-paper, with an abstract (the
  rating needs one). Datasets, software and the rest are dropped.
- Every request goes through `throttle`, one process-wide gate shared with
  the h-index lookup, at most one request per `openalex_min_interval`.
- `api_key` is sent when configured; no `mailto`, ever.

No DB or framework imports, so a Temporal Activity calls this directly.
Left to raise on HTTP errors: the activity's retry policy governs attempts.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from app.config import Settings, get_settings
from app.services.arxiv import FIELD_PREFIX

KEPT_TYPES = ("article", "preprint", "conference-paper")
_ARXIV_DOI_PREFIX = "10.48550/arxiv."
_VENUE_SOURCE_TYPES = ("journal", "conference")
_PER_PAGE = 100
_SELECT = (
    "id,doi,title,abstract_inverted_index,publication_date,type,"
    "primary_location,best_oa_location,open_access"
)

_last_request_at = 0.0
_throttle_lock = threading.Lock()


@dataclass
class OpenAlexResult:
    key: str
    arxiv_id: str | None
    doi: str | None
    openalex_id: str
    title: str
    abstract: str
    published_at: datetime | None
    url: str
    open_access: bool | None
    journal_ref: str | None
    work_type: str


def throttle(delay: float) -> None:
    """Space consecutive OpenAlex requests at least `delay` seconds apart,
    across every thread in this process (the same lock-held wait as
    `app.services.arxiv.throttle`)."""
    global _last_request_at
    with _throttle_lock:
        if delay > 0:
            wait = delay - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
        _last_request_at = time.monotonic()


def is_phrase_query(query: str) -> bool:
    return not FIELD_PREFIX.match(query.strip())


def rebuild_abstract(inverted: dict[str, list[int]] | None) -> str:
    """OpenAlex ships abstracts as word -> positions; put the words back in order."""
    if not inverted:
        return ""
    positioned = sorted((i, word) for word, positions in inverted.items() for i in positions)
    return " ".join(word for _, word in positioned)


def normalize_title(title: str) -> str:
    """Lowercase alphanumerics separated by single spaces, for duplicate matching."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", title.lower()).split())


def search_openalex(
    query: str,
    *,
    since: date,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> list[OpenAlexResult]:
    """The newest kept works whose title or abstract contains the phrase,
    published on or after `since`, up to `openalex_max_results`."""
    settings = settings or get_settings()
    if not settings.openalex_enabled or not is_phrase_query(query):
        return []
    phrase = query.strip().strip('"')
    params = {
        "filter": (
            f'title_and_abstract.search:"{phrase}",from_publication_date:{since:%Y-%m-%d},'
            f"type:{'|'.join(KEPT_TYPES)},has_abstract:true"
        ),
        "sort": "publication_date:desc",
        "per-page": _PER_PAGE,
        "select": _SELECT,
    }
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    url = f"{settings.openalex_api_url.rstrip('/')}/works"

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    results: list[OpenAlexResult] = []
    cursor: str | None = "*"
    try:
        while cursor and len(results) < settings.openalex_max_results:
            throttle(settings.openalex_min_interval)
            response = client.get(url, params={**params, "cursor": cursor})
            response.raise_for_status()
            body = response.json()
            for work in body.get("results", []):
                parsed = _parse(work)
                if parsed is not None:
                    results.append(parsed)
            cursor = (body.get("meta") or {}).get("next_cursor") if body.get("results") else None
    finally:
        if owns_client:
            client.close()
    return results[: settings.openalex_max_results]


def _parse(work: dict) -> OpenAlexResult | None:
    abstract = rebuild_abstract(work.get("abstract_inverted_index"))
    work_type = work.get("type") or ""
    if not abstract or work_type not in KEPT_TYPES:
        return None
    openalex_id = (work.get("id") or "").rsplit("/", 1)[-1]
    doi = (work.get("doi") or "").lower().removeprefix("https://doi.org/") or None
    arxiv_id = doi.removeprefix(_ARXIV_DOI_PREFIX) if doi and doi.startswith(_ARXIV_DOI_PREFIX) else None
    primary = work.get("primary_location") or {}
    best_oa = work.get("best_oa_location") or {}
    source = primary.get("source") or {}
    if arxiv_id:
        url, open_access = f"https://arxiv.org/abs/{arxiv_id}", None
    else:
        url = (
            best_oa.get("landing_page_url")
            or primary.get("landing_page_url")
            or (f"https://doi.org/{doi}" if doi else work.get("id"))
        )
        open_access = (work.get("open_access") or {}).get("is_oa")
    published = work.get("publication_date")
    return OpenAlexResult(
        key=arxiv_id or f"openalex:{openalex_id}",
        arxiv_id=arxiv_id,
        doi=doi,
        openalex_id=openalex_id,
        title=" ".join((work.get("title") or "").split()),
        abstract=abstract,
        published_at=(
            datetime.fromisoformat(published).replace(tzinfo=timezone.utc) if published else None
        ),
        url=url,
        open_access=open_access,
        journal_ref=(
            source.get("display_name")
            if not arxiv_id and source.get("type") in _VENUE_SOURCE_TYPES
            else None
        ),
        work_type=work_type,
    )
