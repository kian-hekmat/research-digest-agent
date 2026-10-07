"""Semantic Scholar client: the highest author h-index for a batch of arXiv papers.

One function, `fetch_max_author_h_index`. The Graph API's paper-batch endpoint
returns each author's h-index inline (`fields=authors.hIndex`), so a whole
batch costs one request per 500 papers - no per-author lookups. No DB or
framework imports, so a Temporal Activity calls it directly.

Brand-new papers often aren't indexed yet (it lags arXiv by a day or so);
they're simply absent from the result, and the ranking treats them as unknown.
"""
from __future__ import annotations

import httpx

from app.config import get_settings

_BATCH_LIMIT = 500  # the endpoint's documented max ids per request


def fetch_max_author_h_index(
    arxiv_ids: list[str], *, client: httpx.Client | None = None
) -> dict[str, int]:
    """Map arxiv_id -> max h-index across the paper's authors, for the papers
    Semantic Scholar knows and has at least one author h-index for.

    Left to raise on failure (429, 5xx, timeout) - the activity's retry
    policy governs attempts. Without an API key requests share a public
    pool that 429s often; set SEMANTIC_SCHOLAR_API_KEY for any real use.
    """
    settings = get_settings()
    headers = {"x-api-key": settings.semantic_scholar_api_key} if settings.semantic_scholar_api_key else {}
    url = f"{settings.semantic_scholar_api_url.rstrip('/')}/paper/batch"

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    found: dict[str, int] = {}
    try:
        for start in range(0, len(arxiv_ids), _BATCH_LIMIT):
            chunk = arxiv_ids[start : start + _BATCH_LIMIT]
            resp = client.post(
                url,
                params={"fields": "authors.hIndex"},
                json={"ids": [f"arXiv:{i}" for i in chunk]},
                headers=headers,
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
    finally:
        if owns_client:
            client.close()
    return found
