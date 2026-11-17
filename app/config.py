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
    # prior_similarity -> novelty anchors: novelty 0 at novelty_repeat,
    # novelty 1 at novelty_unrelated (app.services.ranking).
    novelty_repeat: float
    novelty_unrelated: float

    @model_validator(mode="after")
    def _consistent(self) -> "Calibration":
        values = [self.similar_paper, self.near_duplicate,
                  self.novelty_repeat, self.novelty_unrelated]
        if any(not -1.0 <= v <= 1.0 for v in values):
            raise ValueError("cosine thresholds must be within [-1, 1]")
        if self.novelty_unrelated >= self.novelty_repeat:
            raise ValueError("novelty_unrelated must be below novelty_repeat")
        if self.near_duplicate < self.similar_paper:
            raise ValueError("near_duplicate must be at least similar_paper")
        return self


DEFAULT_RETRIEVAL_CALIBRATION: dict[str, Calibration] = {
    # Measured on the live corpus 2026-11-12 (evals.embeddings; docs/rag-plan.md
    # §5.9). At 768 dimensions its scale nearly matches embeddinggemma's (the
    # model used before; §5.2-5.3), so the thresholds were carried over by
    # quantile and barely moved; 20/20
    # neighbours reviewed at >= 0.749 were related. near_duplicate folds the
    # corpus's one companion pair (GDSW Schwarz preconditioners in 2D and 3D,
    # 0.924), as embeddinggemma did.
    "text-embedding-3-small": Calibration(
        similar_paper=0.749,
        near_duplicate=0.92,
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
    # "none": summarization off; digests and emails still work, without
    # AI-written text.
    summary_backend: Literal["openai", "none"] = "openai"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4-mini"
    openai_timeout: float = 60.0

    # --- spend (app.services.spend, app.spend_ledger) ---
    # Hard monthly cap on OpenAI spend, enforced in the app: at
    # spend_alert_fraction of it the owner is alerted (and new
    # topics wait); at 100% AI text stops until the 1st (UTC). Raising the cap
    # is this one setting. docs/public-launch-plan.md §3.6.
    openai_monthly_budget_usd: float = 50.0
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

    # --- retrieval (embeddings in pgvector, see docs/rag-plan.md) ---
    # Embeddings (EMBEDDING_BACKEND, below) are off whenever summaries are
    # (SUMMARY_BACKEND=none) as well as with RAG_ENABLED=false.
    rag_enabled: bool = True
    # An OpenAI embedding model, requested at embedding_dim dimensions,
    # recorded and priced like every OpenAI call.
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

    # History-aware overviews: show the writer this many of the topic's most
    # recent previous overviews, only so it doesn't repeat them; it never
    # mentions them (1/25 overviews with a factual error in review, against
    # 7/25 when it was allowed to compare; docs/rag-plan.md §5.6).
    rag_overview_history: bool = True
    rag_overview_history_count: int = 3
    # Full text (arXiv HTML) was tried and removed on 2026-11-16: arXiv's
    # terms allow e-print content for personal use or research only
    # (docs/public-launch-plan.md; the code is in git history before that date).
    # Novelty compares a paper with earlier ones published within this window.
    rag_novelty_window_days: int = 90

    @property
    def embeddings_enabled(self) -> bool:
        return self.rag_enabled and self.summaries_enabled

    # --- arXiv ingestion ---
    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    # Newest results per topic per fetch. 100 gives a broad phrase a real
    # sample instead of a signup-time refusal (launch plan v3 cut 3b).
    arxiv_max_results: int = 100
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
    # Display name in the From header ("Papers Weekly <digest@...>"); empty
    # sends the bare address.
    smtp_from_name: str = ""

    # Interactive API docs (/docs, /redoc, /openapi.json). Off in production:
    # they show the admin API's shape.
    api_docs: bool = True

    # --- public signup and self-service ---
    # Signs confirm/manage/unsubscribe/csrf links (app.tokens). Public routes
    # return 503 while it's empty: fail closed.
    secret_key: str = ""
    # Base of every link put in an email.
    public_base_url: str = "http://localhost:8000"
    # Cloudflare Turnstile on signup. An empty secret fails every check.
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""
    signup_per_ip_per_hour: int = 5
    account_emails_per_address_per_day: int = 3

    # --- public limits (app.topic_query, public signup) ---
    # Caps that keep a small public service within its budget: arXiv and
    # OpenAI cost scale with active topics, so both people and topics are bounded.
    max_subscribers: int = 200
    max_active_topics: int = 100  # distinct topics across all subscribers
    max_query_length: int = 200
    # Papers per topic a public subscriber may ask for, and the default.
    public_papers_per_topic_min: int = 1
    public_papers_per_topic_max: int = 10
    public_papers_per_topic_default: int = 5
    # Public topics are plain phrases: letters, digits and light punctuation
    # only, so nothing can smuggle arXiv query syntax or odd input through.
    public_query_pattern: str = r"^[\w\s\-.,'+/()&]+$"


@lru_cache
def get_settings() -> Settings:
    return Settings()
