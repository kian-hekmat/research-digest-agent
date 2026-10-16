"""Text embeddings from a local model (`embeddinggemma`) served by Ollama.

Same shape as app.services.summarize: one plain-HTTP call per batch to
Ollama's `/api/embed`, `Settings`-driven, injectable `http_client` for tests.
With embeddings off (`Settings.embeddings_enabled` False: RAG_ENABLED=false
or SUMMARY_BACKEND=none) every method returns None immediately - no network
call, no error - and the pipeline runs without retrieval.

EmbeddingGemma is trained with task prompts; `document_text` is the one place
that applies them. Papers are embedded as documents and compared document to
document, so no query-side prompt is needed yet.
"""
from __future__ import annotations

import httpx

from app.config import Settings, get_settings


def document_text(title: str, abstract: str) -> str:
    """EmbeddingGemma's document prompt."""
    return f"title: {title} | text: {abstract}"


class Embedder:
    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client

    @property
    def enabled(self) -> bool:
        return self._settings.embeddings_enabled

    @property
    def model(self) -> str:
        return self._settings.embedding_model

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed `texts` in order, in batches of `embedding_batch_size`.
        Left to raise on failure (server down, model not pulled, wrong
        width) - the caller's retry policy governs attempts."""
        if not self.enabled:
            return None
        size = max(1, self._settings.embedding_batch_size)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            vectors.extend(self._embed_batch(texts[start : start + size]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(timeout=self._settings.ollama_timeout)
        try:
            resp = client.post(
                f"{self._settings.ollama_base_url.rstrip('/')}/api/embed",
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
        finally:
            if owns_client:
                client.close()
        vectors = resp.json()["embeddings"]
        if len(vectors) != len(texts):
            raise ValueError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        expected = self._settings.embedding_dim
        if any(len(v) != expected for v in vectors):
            raise ValueError(
                f"{self.model} returned {len(vectors[0])}-dim embeddings; "
                f"paper_embeddings stores {expected} (EMBEDDING_DIM)"
            )
        return vectors
