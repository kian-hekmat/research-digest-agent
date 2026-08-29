from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (or a local .env).

    Every field has a default so the app and the test suite import cleanly with
    nothing set; only the Anthropic calls actually need a real key.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = (
        "postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_db"
    )

    # --- summarization (Anthropic) ---
    anthropic_api_key: str = ""
    summary_model: str = "claude-haiku-4-5"  # per-paper summaries: short, high volume
    overview_model: str = "claude-sonnet-5"  # one synthesis call per digest

    # --- arXiv ingestion ---
    arxiv_api_url: str = "http://export.arxiv.org/api/query"
    arxiv_max_results: int = 25
    arxiv_page_delay: float = 3.0  # arXiv asks clients to space requests out


@lru_cache
def get_settings() -> Settings:
    return Settings()
