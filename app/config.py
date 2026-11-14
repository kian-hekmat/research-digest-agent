from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# USD per 1M tokens, by model. An OPENAI_PRICES override is merged over these,
# so overriding one model never drops another's price.
DEFAULT_OPENAI_PRICES: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "text-embedding-3-small": {"input": 0.02, "cached_input": 0.02, "output": 0.0},
}
_PRICE_KEYS = {"input", "cached_input", "output"}


class Calibration(BaseModel):
    """Similarity thresholds for one embedding model. Cosine scales differ a
    lot between models, so every threshold belongs to the model it was
    measured on (evals.embeddings; docs/rag-plan.md §5.2-5.3, §5.9)."""

    # The closest earlier, topic-linked paper at or above this is shown as
    # "Similar earlier paper".
    similar_paper: float
    # At or above this, an earlier paper counts as the same work.
    near_duplicate: float
    # Floor for related-work candidates offered to the summarizer.
    related_min: float
    # prior_similarity -> novelty anchors: novelty 0 at novelty_repeat,
    # novelty 1 at novelty_unrelated (app.services.ranking).
    novelty_repeat: float
    novelty_unrelated: float
    # Floor for "join this existing topic?" suggestions at signup; None
    # turns suggestions off for the model.
    topic_suggestion: float | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Calibration":
        values = [self.similar_paper, self.near_duplicate, self.related_min,
                  self.novelty_repeat, self.novelty_unrelated]
        if self.topic_suggestion is not None:
            values.append(self.topic_suggestion)
        if any(not -1.0 <= v <= 1.0 for v in values):
            raise ValueError("cosine thresholds must be within [-1, 1]")
        if self.novelty_unrelated >= self.novelty_repeat:
            raise ValueError("novelty_unrelated must be below novelty_repeat")
        if self.near_duplicate < self.similar_paper:
            raise ValueError("near_duplicate must be at least similar_paper")
        return self


# embeddinggemma's numbers (docs/rag-plan.md §5.2-5.3): its scale is
# compressed - random pairs ~0.41 (p99 0.67), nearest neighbours ~0.66. Every
# neighbour reviewed at >= 0.75 was genuinely related (~7% of papers have
# one); at 0.65 half the related-work notes were wrong, all from neighbours
# <= 0.70, hence 0.72. Near duplicates are untested: no pair reached 0.89.
DEFAULT_RETRIEVAL_CALIBRATION: dict[str, Calibration] = {
    "embeddinggemma": Calibration(
        similar_paper=0.75,
        near_duplicate=0.9,
        related_min=0.72,
        novelty_repeat=0.85,
        novelty_unrelated=0.45,
    ),
    # Measured on the live corpus 2026-11-12 (evals.embeddings; docs/rag-plan.md
    # §5.9). At 768 dimensions its scale nearly matches embeddinggemma's, so
    # the thresholds are carried over by quantile and barely move; 20/20
    # neighbours reviewed at >= 0.749 were related. near_duplicate folds the
    # corpus's one companion pair (GDSW Schwarz preconditioners in 2D and 3D,
    # 0.924), as embeddinggemma does today. No topic_suggestion: acronyms sit
    # far from their expansions ("RLHF" vs its expansion: 0.196).
    "text-embedding-3-small": Calibration(
        similar_paper=0.749,
        near_duplicate=0.92,
        related_min=0.721,
        novelty_repeat=0.838,
        novelty_unrelated=0.459,
    ),
}


class MissingCalibration(RuntimeError):
    """The configured embedding model has no retrieval calibration."""

    def __init__(self, model: str) -> None:
        super().__init__(
            f"No retrieval calibration for embedding model {model!r}: measure it with "
            "`python -m evals.embeddings` and add it to RETRIEVAL_CALIBRATION"
        )
        self.model = model


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

    # Shared secret for the admin JSON API (X-Admin-Token header). Empty means
    # every admin route answers 401 - the API stays locked until one is set.
    admin_token: str = ""

    # --- summarization ---
    # "openai": summaries, relevance ratings and overviews come from the OpenAI
    # API (needs OPENAI_API_KEY; billed per token).
    # "ollama": they come from a local open-weights model served by Ollama, as
    # they did until November 2026 - no API key, no per-call cost.
    # "none": summarization off; digests and emails still work, without
    # AI-written text.
    summary_backend: Literal["openai", "ollama", "none"] = "openai"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4-mini"
    openai_timeout: float = 60.0

    # Ollama also serves embeddings, whichever backend writes the summaries.
    # It listens on 127.0.0.1 only; docker-compose points containers at the
    # Mac via host.docker.internal instead (colima forwards it to loopback).
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:12b"  # used only with SUMMARY_BACKEND=ollama
    ollama_timeout: float = 120.0  # first call of a session also loads the model

    # --- spend (app.services.spend, app.spend_ledger) ---
    # Hard monthly cap on OpenAI spend, enforced in the app: at
    # spend_alert_fraction of it the owner is alerted (and, from Phase 3, new
    # topics wait); at 100% AI text stops until the 1st (UTC). Raising the cap
    # is this one setting. docs/public-launch-plan.md §3.6.
    openai_monthly_budget_usd: float = 20.0
    spend_alert_fraction: float = 0.8
    # USD per 1M tokens, by model. Settings rather than code so a price change
    # needs no deploy; overridable as JSON in OPENAI_PRICES (merged over
    # DEFAULT_OPENAI_PRICES). A model missing here can't be called at all -
    # spend must never go unrecorded.
    openai_prices: dict[str, dict[str, float]] = DEFAULT_OPENAI_PRICES

    @field_validator("openai_prices")
    @classmethod
    def _merge_and_check_prices(cls, prices: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for model, price in prices.items():
            if set(price) != _PRICE_KEYS:
                raise ValueError(f"price for {model!r} needs exactly {sorted(_PRICE_KEYS)}")
            if any(v < 0 for v in price.values()):
                raise ValueError(f"price for {model!r} can't be negative")
        return {**DEFAULT_OPENAI_PRICES, **prices}
    # Where spend and other operational alerts are emailed. Empty: alerts are
    # only logged.
    alert_email: str = ""

    @property
    def summaries_enabled(self) -> bool:
        """When False, Summarizer skips the call cleanly instead of
        failing/retrying. When True and the backend is actually down or
        misconfigured, calls fail and are retried like any other outage."""
        return self.summary_backend != "none"

    @property
    def summary_model(self) -> str:
        return self.openai_model if self.summary_backend == "openai" else self.ollama_model

    # --- retrieval (embeddings in pgvector, see docs/rag-plan.md) ---
    # Embeddings (EMBEDDING_BACKEND, below) are off whenever summaries are
    # (SUMMARY_BACKEND=none) as well as with RAG_ENABLED=false.
    rag_enabled: bool = True
    # "openai" (default since 2026-11-12): text-embedding-3-small, requested
    # at embedding_dim dimensions, recorded and priced like every OpenAI call.
    # "ollama": EmbeddingGemma served by a local Ollama, for development or a
    # rollback (EMBEDDING_BACKEND=ollama EMBEDDING_MODEL=embeddinggemma, then
    # `python -m app.backfill_embeddings`).
    embedding_backend: Literal["openai", "ollama"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    # Must match the vector(N) column in paper_embeddings - changing models
    # to one with another width needs a migration.
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    # Similarity thresholds, one row per embedding model (see Calibration).
    # An override (RETRIEVAL_CALIBRATION, JSON) is merged per model.
    retrieval_calibration: dict[str, Calibration] = DEFAULT_RETRIEVAL_CALIBRATION

    @field_validator("retrieval_calibration")
    @classmethod
    def _merge_calibration(cls, value: dict[str, Calibration]) -> dict[str, Calibration]:
        return {**DEFAULT_RETRIEVAL_CALIBRATION, **value}

    @property
    def calibration(self) -> Calibration:
        """The thresholds for the configured embedding model. Raises
        MissingCalibration rather than borrow another model's numbers."""
        try:
            return self.retrieval_calibration[self.embedding_model]
        except KeyError:
            raise MissingCalibration(self.embedding_model) from None

    # Related-work context for summaries: up to this many earlier papers,
    # each at least calibration.related_min similar.
    rag_top_k: int = 4
    # LLM-written related-work notes are off by default: even at 0.72, 5 of 19
    # notes misdescribed the cited paper (§5.3) - over the 10% bar. Emails
    # show the closest earlier paper as a plain link instead
    # (calibration.similar_paper), which
    # makes no claim about how the two relate.
    rag_related_notes: bool = False
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
    # Full-text excerpts for summaries (phase E): fetch each new paper's arXiv
    # HTML, chunk and embed it, and show the summarizer the best-matching
    # chunks. Readers preferred the result 17 to 3; 2 of 20 misread a number's
    # context (docs/rag-plan.md §5.7). Costs ~3s of fetch + ~4s of LLM per paper.
    # Off by default: arXiv's terms allow e-print content for personal use or
    # research, and a public service is a grey area (docs/public-launch-plan.md
    # §0.1). When on, it runs in send prep, for each topic's top K only.
    rag_fulltext: bool = False
    rag_fulltext_excerpts: int = 3
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
    # Full text (app.services.fulltext): arXiv's HTML rendering, from the
    # export host arXiv asks scripts to use, through the same rate gate.
    arxiv_html_url: str = "https://export.arxiv.org/html"

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
    # LLM activities (ratings, summaries) one workflow keeps in flight at once.
    llm_max_concurrency: int = 8
    # Child workflows a fan-out (one per topic) starts at once; it waits for
    # each batch before starting the next.
    fanout_batch_size: int = 10
    # Threads the worker runs its activities on, and so how many it takes on
    # at once (arXiv requests have their own one-slot worker).
    worker_activity_threads: int = 20

    # --- email delivery (SMTP) ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025  # Mailpit's default SMTP port locally
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    smtp_from_address: str = "digest@example.com"

    # --- public limits (app.topic_query, public signup) ---
    # Caps that keep a small public service within its budget: arXiv and
    # OpenAI cost scale with active topics, so both people and topics are bounded.
    max_subscribers: int = 200
    max_topics_per_subscriber: int = 5
    max_active_topics: int = 100  # distinct topics across all subscribers
    max_query_length: int = 200
    # Papers per topic a public subscriber may ask for, and the default.
    public_papers_per_topic_min: int = 5
    public_papers_per_topic_max: int = 10
    public_papers_per_topic_default: int = 5
    # Public topics are plain phrases: letters, digits and light punctuation
    # only, so nothing can smuggle arXiv query syntax or odd input through.
    public_query_pattern: str = r"^[\w\s\-.,'+/()&]+$"


@lru_cache
def get_settings() -> Settings:
    return Settings()
