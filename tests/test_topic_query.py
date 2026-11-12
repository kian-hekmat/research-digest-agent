import pytest
from app.config import Settings
from app.topic_query import normalize_query, validate_public_query

@pytest.mark.parametrize("raw", ["RLHF", " rlhf ", '"RLHF"', "“RLHF”", "  \"Rlhf\"\t"])
def test_variants_normalize_to_one_topic(raw):
    assert normalize_query(raw) == "rlhf"

def test_whitespace_collapses():
    assert normalize_query("physics-informed   neural\nnetworks") == "physics-informed neural networks"

def test_apostrophes_are_kept():
    assert normalize_query("Bayes' rule") == "bayes' rule"

@pytest.mark.parametrize("raw", ["cat:cs.LG", "  cat:math.NA ", "ti:Transformer AND au:Vaswani"])
def test_field_prefixed_queries_pass_through(raw):
    assert normalize_query(raw) == raw.strip()

@pytest.mark.parametrize("raw", ["", "   ", '""', "“ ”"])
def test_empty_queries_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_query(raw)

def test_public_rejects_field_prefix():
    with pytest.raises(ValueError, match="phrase"):
        validate_public_query("cat:cs.LG")

def test_public_rejects_too_long():
    with pytest.raises(ValueError):
        validate_public_query("a" * 201, Settings(max_query_length=200))

def test_public_rejects_disallowed_characters():
    with pytest.raises(ValueError):
        validate_public_query("rlhf; DROP TABLE")

def test_public_accepts_and_normalizes():
    assert validate_public_query(' "Physics-Informed Neural Networks" ') == "physics-informed neural networks"

def test_limit_defaults():
    s = Settings()
    assert (s.max_subscribers, s.max_topics_per_subscriber, s.max_active_topics, s.max_query_length) == (200, 5, 100, 200)
    assert (s.public_papers_per_topic_min, s.public_papers_per_topic_max, s.public_papers_per_topic_default) == (5, 10, 5)
