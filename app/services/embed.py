"""Text embeddings, from OpenAI or from a local model served by Ollama.

`EMBEDDING_BACKEND` picks the backend:
- "openai": an OpenAI embedding model (`text-embedding-3-small`), requested
  at `embedding_dim` dimensions so it fits the existing vector(768) column.
  Priced and recorded like every OpenAI call (app.services.spend), with
  purpose "embedding" - never blocked by the budget, since retrieval,
  previews and similar-topic suggestions depend on it and it costs a
  fraction of a cent.
- "ollama": EmbeddingGemma, served by Ollama on the owner's Mac. Kept as a
  local development backend.

Same shape as app.services.summarize: plain HTTP per batch, `Settings`-
driven, injectable `http_client` for tests. With embeddings off
(`Settings.embeddings_enabled` False: RAG_ENABLED=false or
SUMMARY_BACKEND=none) `embed` returns None immediately - no network call, no
error - and the pipeline runs without retrieval.

EmbeddingGemma is trained with task prompts; `document_text` and `query_text`
apply them, and only for Ollama. OpenAI's models take plain text. Papers are
embedded as documents and compared document to document; full-text chunks
and topic queries are found with queries.
"""
from __future__ import annotations

import httpx

from app.config import Settings, get_settings
from app.services.spend import (
    SpendLedger,
    SpendLimitReached,
    Usage,
    cost_usd,
    is_spend_limit_error,
    usage_from_openai,
)


class Embedder:
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
        # None: nothing recorded (tests, ad-hoc use). Every production caller
        # passes one - tests/test_spend_coverage.py.
        self._ledger = ledger
        self._topic_id = topic_id

    @property
    def enabled(self) -> bool:
        return self._settings.embeddings_enabled

    @property
    def model(self) -> str:
        return self._settings.embedding_model

    @property
    def _openai(self) -> bool:
        return self._settings.embedding_backend == "openai"

    def document_text(self, title: str, abstract: str) -> str:
        if self._openai:
            return f"{title}\n\n{abstract}"
        return f"title: {title} | text: {abstract}"  # EmbeddingGemma's document prompt

    def query_text(self, query: str) -> str:
        if self._openai:
            return query
        return f"task: search result | query: {query}"  # EmbeddingGemma's retrieval-query prompt

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed `texts` in order, in batches of `embedding_batch_size`.
        Left to raise on failure (server down, model not pulled, wrong
        width) - the caller's retry policy governs attempts. Raises
        SpendLimitReached when OpenAI refuses paid calls."""
        if not self.enabled:
            return None
        if self._openai and self._ledger is not None:
            # A model without a price could never be recorded.
            cost_usd(self.model, Usage(0, 0, 0), self._settings.openai_prices)
        size = max(1, self._settings.embedding_batch_size)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            batch = texts[start : start + size]
            vectors.extend(self._embed_openai(batch) if self._openai else self._embed_ollama(batch))
        return vectors

    def _post(self, url: str, timeout: float, **kwargs) -> httpx.Response:
        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(timeout=timeout)
        try:
            return client.post(url, **kwargs)
        finally:
            if owns_client:
                client.close()

    def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        if not self._settings.openai_api_key:
            raise RuntimeError("EMBEDDING_BACKEND=openai needs OPENAI_API_KEY to be set")
        resp = self._post(
            f"{self._settings.openai_base_url.rstrip('/')}/embeddings",
            self._settings.openai_timeout,
            headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
            json={"model": self.model, "input": texts, "dimensions": self._settings.embedding_dim},
        )
        if self._ledger is not None and is_spend_limit_error(resp):
            self._ledger.hard_limit_hit()
            raise SpendLimitReached("OpenAI refused the embedding call: spend limit or credits")
        resp.raise_for_status()
        body = resp.json()
        if self._ledger is not None:
            # Recorded before parsing: a reply we can't use was still paid for.
            self._ledger.record(
                model=self.model, purpose="embedding", topic_id=self._topic_id,
                usage=usage_from_openai(body),
            )
        # The API documents `index` as the input position; don't assume the
        # list itself is in order.
        vectors = [d["embedding"] for d in sorted(body["data"], key=lambda d: d["index"])]
        return self._checked(vectors, texts)

    def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        resp = self._post(
            f"{self._settings.ollama_base_url.rstrip('/')}/api/embed",
            self._settings.ollama_timeout,
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        return self._checked(resp.json()["embeddings"], texts)

    def _checked(self, vectors: list[list[float]], texts: list[str]) -> list[list[float]]:
        if len(vectors) != len(texts):
            raise ValueError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        expected = self._settings.embedding_dim
        if any(len(v) != expected for v in vectors):
            raise ValueError(
                f"{self.model} returned {len(vectors[0])}-dim embeddings; "
                f"paper_embeddings stores {expected} (EMBEDDING_DIM)"
            )
        return vectors
