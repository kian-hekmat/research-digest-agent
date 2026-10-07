"""Author h-index lookup: the highest author h-index for a batch of arXiv papers.

One public function, `fetch_max_author_h_index`, backed by one of two sources:

- Semantic Scholar, when SEMANTIC_SCHOLAR_API_KEY is set. Its paper-batch
  endpoint returns each author's h-index inline, so a batch costs one request
  per 500 papers. Fresher (indexes arXiv within about a day), but without a
  key requests share a public pool that is rate-limited nearly all the time.
- OpenAlex otherwise - no key needed, 1000 requests/day per caller. Two
  requests per 100 papers: the works (found by arXiv's DOI, 10.48550/arXiv.<id>)
  for their author ids, then those authors for their h-index. Lags arXiv by
  a few days.

Exactly one source per call, never a mix: the two compute h-index over
different corpora (OpenAlex's run noticeably higher), so mixing them within
one batch would rank papers by which source answered rather than by authors.

Papers a source hasn't indexed yet are simply absent from the result; the
ranking treats them as unknown. No DB or framework imports, so a Temporal
Activity calls this directly.
"""
from __future__ import annotations

import httpx

from app.config import get_settings

_S2_BATCH_LIMIT = 500  # Semantic Scholar's max ids per batch request
_OPENALEX_OR_LIMIT = 100  # OpenAlex's max values in one OR'd filter
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def fetch_max_author_h_index(
    arxiv_ids: list[str], *, client: httpx.Client | None = None
) -> dict[str, int]:
    """Map arxiv_id -> max h-index across the paper's authors, for the papers
    the source knows and has at least one author h-index for.

    Left to raise on failure (429, 5xx, timeout) - the activity's retry
    policy governs attempts.
    """
    if not arxiv_ids:
        return {}
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        if get_settings().semantic_scholar_api_key:
            return _from_semantic_scholar(arxiv_ids, client)
        return _from_openalex(arxiv_ids, client)
    finally:
        if owns_client:
            client.close()


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _from_semantic_scholar(arxiv_ids: list[str], client: httpx.Client) -> dict[str, int]:
    settings = get_settings()
    url = f"{settings.semantic_scholar_api_url.rstrip('/')}/paper/batch"
    found: dict[str, int] = {}
    for chunk in _chunks(arxiv_ids, _S2_BATCH_LIMIT):
        resp = client.post(
            url,
            params={"fields": "authors.hIndex"},
            json={"ids": [f"arXiv:{i}" for i in chunk]},
            headers={"x-api-key": settings.semantic_scholar_api_key},
        )
        # When *none* of the ids are indexed yet the endpoint answers 400
        # rather than a list of nulls - common for a batch of fresh papers.
        if resp.status_code == 400 and "No valid paper ids" in resp.text:
            continue
        resp.raise_for_status()
        # Results are positional: one entry (or null) per requested id.
        for arxiv_id, paper in zip(chunk, resp.json()):
            if not paper:
                continue
            h_indices = [
                a["hIndex"] for a in paper.get("authors") or [] if a.get("hIndex") is not None
            ]
            if h_indices:
                found[arxiv_id] = max(h_indices)
    return found


def _openalex_id(url: str | None) -> str | None:
    # "https://openalex.org/A5133901964" -> "A5133901964"
    return url.rsplit("/", 1)[-1] if url else None


def _from_openalex(arxiv_ids: list[str], client: httpx.Client) -> dict[str, int]:
    base = get_settings().openalex_api_url.rstrip("/")
    by_doi = {f"{_ARXIV_DOI_PREFIX}{i}".lower(): i for i in arxiv_ids}

    # 1. works -> their author ids
    authors_of: dict[str, list[str]] = {}
    for chunk in _chunks(list(by_doi), _OPENALEX_OR_LIMIT):
        resp = client.get(
            f"{base}/works",
            params={
                "filter": f"doi:{'|'.join(chunk)}",
                "select": "doi,authorships",
                "per_page": _OPENALEX_OR_LIMIT,
            },
        )
        resp.raise_for_status()
        for work in resp.json()["results"]:
            doi = (work.get("doi") or "").lower().removeprefix("https://doi.org/")
            if doi not in by_doi:
                continue
            ids = [_openalex_id(a["author"].get("id")) for a in work.get("authorships") or []]
            authors_of[by_doi[doi]] = [i for i in ids if i]

    # 2. authors -> h-index
    author_ids = sorted({a for ids in authors_of.values() for a in ids})
    h_index: dict[str, int] = {}
    for chunk in _chunks(author_ids, _OPENALEX_OR_LIMIT):
        resp = client.get(
            f"{base}/authors",
            params={
                "filter": f"id:{'|'.join(chunk)}",
                "select": "id,summary_stats",
                "per_page": _OPENALEX_OR_LIMIT,
            },
        )
        resp.raise_for_status()
        for author in resp.json()["results"]:
            h = (author.get("summary_stats") or {}).get("h_index")
            if h is not None:
                h_index[_openalex_id(author["id"])] = h

    found: dict[str, int] = {}
    for arxiv_id, ids in authors_of.items():
        known = [h_index[a] for a in ids if a in h_index]
        if known:
            found[arxiv_id] = max(known)
    return found
