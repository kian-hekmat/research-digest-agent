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
