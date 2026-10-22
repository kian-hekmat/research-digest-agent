from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (or a local .env).

    Every field has a default so the app and the test suite import cleanly with
    nothing set.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = (
        "postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_db"
    )

    # --- summarization (local Gemma via Ollama) ---
    # "ollama": summaries, relevance ratings and overviews come from a local
    # open-weights model served by Ollama - no API key, no per-call cost.
    # "none": summarization off; digests and emails still work, without
    # AI-written text.
    summary_backend: Literal["ollama", "none"] = "ollama"

    # Ollama listens on 127.0.0.1 only; docker-compose points containers at the
    # Mac via host.docker.internal instead (colima forwards it to loopback).
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:12b"  # summaries, ratings and overviews
    ollama_timeout: float = 120.0  # first call of a session also loads the model

    @property
    def summaries_enabled(self) -> bool:
        """When False, Summarizer skips the call cleanly instead of
        failing/retrying. When True and the Ollama server is actually down,
        calls fail and are retried like any other outage."""
        return self.summary_backend == "ollama"

    # --- retrieval (embeddings in pgvector, see docs/rag-plan.md) ---
    # Paper embeddings come from the same Ollama server as summaries, so
    # they're off whenever summaries are (SUMMARY_BACKEND=none) as well as
    # with RAG_ENABLED=false.
    rag_enabled: bool = True
    embedding_model: str = "embeddinggemma"
    # Must match the vector(N) column in paper_embeddings - changing models
    # to one with another width needs a migration.
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    # Related-work context for summaries: up to this many earlier papers,
    # each at least this similar (cosine). embeddinggemma's scale is
    # compressed - random pairs sit around 0.41 (p99 0.67), a paper's nearest
    # neighbour around 0.66. At 0.65 half of Gemma's related-work notes were
    # wrong, all from neighbours at <= 0.70; see docs/rag-plan.md §5.3.
    rag_top_k: int = 4
    rag_min_similarity: float = 0.72
    # LLM-written related-work notes are off by default: even at 0.72, 5 of 19
    # notes misdescribed the cited paper (§5.3) - over the 10% bar. Emails
    # show the closest earlier paper as a plain link instead (below), which
    # makes no claim about how the two relate.
    rag_related_notes: bool = False
    # The closest earlier, topic-linked paper at or above this similarity is
    # shown as "Similar earlier paper". Every neighbour reviewed at >= 0.75
    # was genuinely related; ~7% of papers have one.
    rag_similar_paper_similarity: float = 0.75
    # At or above this, an earlier paper counts as the same work. Untested on
    # real duplicates: the corpus had none above 0.89 when this was set.
    rag_near_duplicate_similarity: float = 0.9
    # Few-shot calibration for relevance ratings: up to this many
    # reference-rated papers per rating band (8-10, 4-7, 1-3), most similar
    # first. Only reference labels (topic_paper.reference_relevance) are
    # used, never the LLM's own ratings, which would just echo its biases.
    # Off: with ~20 labels per topic the nearest anchors are only loosely
    # related, and they made gemma3:12b's ratings worse (overall rho 0.71 ->
    # 0.52, on-topic 0.27 -> 0.02; docs/rag-plan.md §5.4).
    rag_rating_anchors: bool = False
    rag_anchors_per_band: int = 2
    # History-aware overviews: show the writer this many of the topic's most
    # recent previous overviews, plus each new paper's similar earlier paper.
    rag_overview_history: bool = True
    rag_overview_history_count: int = 3
    # "avoid_repeats": the writer sees earlier overviews only so as not to
    # repeat them and never mentions them (1/25 overviews with a factual
    # error in review). "compare": it may relate the batch to them and to
    # similar earlier papers - more cross-references, but 7/25 had an error.
    # docs/rag-plan.md §5.6.
    rag_overview_history_style: Literal["avoid_repeats", "compare"] = "avoid_repeats"
    # Novelty compares a paper with earlier ones published within this window.
    rag_novelty_window_days: int = 90

    @property
    def embeddings_enabled(self) -> bool:
        return self.rag_enabled and self.summaries_enabled

    # --- arXiv ingestion ---
    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    arxiv_max_results: int = 25
    arxiv_page_delay: float = 3.0  # arXiv asks clients to space requests out
    # A paper's `published` timestamp is its submission time, but it only
    # appears in the API once announced - typically 1-3 days later (longer
    # over weekends/holidays). Each fetch looks back this far past the topic's
    # watermark so late-announced papers aren't dropped; already-ingested ones
    # are skipped by arxiv_id.
    arxiv_lookback_days: int = 7

    # --- ranking (app.services.ranking) ---
    # Author h-indices come from Semantic Scholar when a key is set (fresher
    # data), else from OpenAlex, which needs no key. Semantic Scholar's keyless
    # pool is rate-limited nearly all the time, so it's never used without one.
    # https://www.semanticscholar.org/product/api#api-key-form
    semantic_scholar_api_url: str = "https://api.semanticscholar.org/graph/v1"
    semantic_scholar_api_key: str = ""
    openalex_api_url: str = "https://api.openalex.org"

    # --- Temporal ---
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "digest-task-queue"

    # --- email delivery (SMTP) ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025  # Mailpit's default SMTP port locally
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    smtp_from_address: str = "digest@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
