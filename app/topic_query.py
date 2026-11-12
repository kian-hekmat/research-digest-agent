"""Topic query normalization and public-input validation.

Normalizing makes "RLHF", " rlhf " and '"RLHF"' one shared topic, so public
subscribers who type the same thing differently share one arXiv fetch.
"""
import re

from app.config import Settings, get_settings
from app.services.arxiv import FIELD_PREFIX

_QUOTES = str.maketrans("", "", '"“”')


def normalize_query(query: str) -> str:
    """Canonical form of a topic query.

    Field-prefixed queries (admin-only power syntax like ``cat:cs.LG``) are
    only stripped: lowercasing or dropping quotes could change their meaning.
    Phrases lose double quotes (arxiv.py re-quotes them), collapse whitespace
    and are lowercased. Apostrophes stay ("Bayes' rule").
    """
    stripped = query.strip()
    if FIELD_PREFIX.match(stripped):
        return stripped
    phrase = " ".join(stripped.translate(_QUOTES).split()).lower()
    if not phrase:
        raise ValueError("empty query")
    return phrase


def validate_public_query(query: str, settings: Settings | None = None) -> str:
    """Normalize a public subscriber's topic, or raise ValueError with a
    message that is safe to show them."""
    settings = settings or get_settings()
    if FIELD_PREFIX.match(query.strip()):
        raise ValueError(
            'Enter a phrase, like "physics-informed neural networks". '
            "Category and field searches aren't available."
        )
    phrase = normalize_query(query)
    if len(phrase) > settings.max_query_length:
        raise ValueError(f"Topics can be at most {settings.max_query_length} characters.")
    if not re.match(settings.public_query_pattern, phrase):
        raise ValueError(
            "Topics can use letters, numbers, spaces and basic punctuation only."
        )
    return phrase
