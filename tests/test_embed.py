import json

import httpx
import pytest
import respx

from app.config import Settings
from app.services.embed import Embedder
from app.services.spend import SpendLimitReached, UnknownModelPrice, Usage

EMBED_URL = "http://ollama.test/api/embed"


def _settings(**overrides) -> Settings:
    base = dict(
        summary_backend="ollama",
        ollama_base_url="http://ollama.test",
        embedding_model="embeddinggemma",
        embedding_dim=4,
        embedding_batch_size=2,
    )
    return Settings(**{**base, **overrides})


def _reply(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["input"]
    return httpx.Response(200, json={"embeddings": [[float(len(t)), 0.0, 0.0, 1.0] for t in texts]})


def test_document_text_uses_embeddinggemma_document_prompt():
    e = Embedder(Settings(embedding_backend="ollama"))
    assert e.document_text("A Title", "An abstract.") == "title: A Title | text: An abstract."
    assert e.query_text("main results") == "task: search result | query: main results"


@respx.mock
def test_embed_batches_and_preserves_order():
    route = respx.post(EMBED_URL).mock(side_effect=_reply)

    vectors = Embedder(_settings()).embed(["a", "bb", "ccc"])

    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
    assert route.call_count == 2  # batch size 2
    first = json.loads(route.calls[0].request.content)
    assert first == {"model": "embeddinggemma", "input": ["a", "bb"]}


@respx.mock
def test_wrong_width_raises_instead_of_storing_garbage():
    respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json={"embeddings": [[1.0, 2.0]]}))

    with pytest.raises(ValueError, match="2-dim"):
        Embedder(_settings()).embed(["a"])


@respx.mock
def test_count_mismatch_raises():
    respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json={"embeddings": []}))

    with pytest.raises(ValueError, match="asked for 1"):
        Embedder(_settings()).embed(["a"])


@respx.mock
def test_server_error_raises_for_the_retry_policy():
    respx.post(EMBED_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        Embedder(_settings()).embed(["a"])


@pytest.mark.parametrize("overrides", [{"rag_enabled": False}, {"summary_backend": "none"}])
@respx.mock
def test_disabled_makes_no_call(overrides):
    route = respx.post(EMBED_URL).mock(side_effect=_reply)

    embedder = Embedder(_settings(**overrides))

    assert not embedder.enabled
    assert embedder.embed(["a"]) is None
    assert not route.called


# ---------- OpenAI backend (text-embedding-3-small) ----------
OPENAI_EMBED = "http://openai.test/v1/embeddings"


class FakeLedger:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.records = []
        self.hard_limits = 0

    def allow(self, purpose):
        return self._allow

    def record(self, *, model, purpose, topic_id, usage):
        self.records.append({"model": model, "purpose": purpose, "topic_id": topic_id, "usage": usage})

    def hard_limit_hit(self):
        self.hard_limits += 1


def _openai(**overrides) -> Settings:
    base = dict(
        summary_backend="openai",
        openai_api_key="k",
        openai_base_url="http://openai.test/v1",
        embedding_backend="openai",
        embedding_model="text-embedding-3-small",
        embedding_dim=768,
    )
    return Settings(**{**base, **overrides})


def _vectors(n, dim=768, order=None):
    order = list(range(n)) if order is None else order
    return {
        "data": [{"object": "embedding", "index": i, "embedding": [float(i)] * dim} for i in order],
        "usage": {"prompt_tokens": 7 * n, "total_tokens": 7 * n},
    }


@respx.mock
def test_openai_request_shape_and_no_task_prompts():
    route = respx.post(OPENAI_EMBED).mock(return_value=httpx.Response(200, json=_vectors(1)))
    e = Embedder(_openai())

    assert e.document_text("T", "A") == "T\n\nA"
    assert e.query_text("main results") == "main results"
    e.embed([e.document_text("T", "A")])

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer k"
    assert json.loads(request.content) == {
        "model": "text-embedding-3-small", "input": ["T\n\nA"], "dimensions": 768,
    }


@respx.mock
def test_vectors_are_ordered_by_index():
    respx.post(OPENAI_EMBED).mock(return_value=httpx.Response(200, json=_vectors(3, order=[2, 0, 1])))
    vectors = Embedder(_openai()).embed(["a", "b", "c"])
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]


@respx.mock
def test_empty_input_makes_no_call():
    route = respx.post(OPENAI_EMBED).mock(return_value=httpx.Response(200, json=_vectors(0)))
    assert Embedder(_openai(), ledger=FakeLedger()).embed([]) == []
    assert not route.called


@respx.mock
def test_batches_and_records_one_row_per_request():
    def reply(request):
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json=_vectors(n))

    route = respx.post(OPENAI_EMBED).mock(side_effect=reply)
    ledger = FakeLedger()

    vectors = Embedder(_openai(embedding_batch_size=2), ledger=ledger, topic_id="t1").embed(list("abcde"))

    assert len(vectors) == 5
    assert route.call_count == 3
    assert [r["purpose"] for r in ledger.records] == ["embedding"] * 3
    assert [r["usage"] for r in ledger.records] == [Usage(14, 0, 0), Usage(14, 0, 0), Usage(7, 0, 0)]
    assert {r["model"] for r in ledger.records} == {"text-embedding-3-small"}
    assert {r["topic_id"] for r in ledger.records} == {"t1"}


@respx.mock
def test_wrong_width_raises():
    respx.post(OPENAI_EMBED).mock(return_value=httpx.Response(200, json=_vectors(1, dim=1536)))
    with pytest.raises(ValueError, match="768"):
        Embedder(_openai()).embed(["a"])


@respx.mock
def test_spend_limit_raises_spend_limit_reached_and_reports():
    respx.post(OPENAI_EMBED).mock(
        return_value=httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
    )
    ledger = FakeLedger()
    with pytest.raises(SpendLimitReached):
        Embedder(_openai(), ledger=ledger).embed(["a"])
    assert ledger.hard_limits == 1
    assert ledger.records == []


@respx.mock
def test_ordinary_429_still_raises_http_error():
    respx.post(OPENAI_EMBED).mock(
        return_value=httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        Embedder(_openai(), ledger=FakeLedger()).embed(["a"])


@respx.mock
def test_unknown_model_price_fails_before_http():
    route = respx.post(OPENAI_EMBED).mock(return_value=httpx.Response(200, json=_vectors(1)))
    with pytest.raises(UnknownModelPrice):
        Embedder(_openai(embedding_model="text-embedding-9"), ledger=FakeLedger()).embed(["a"])
    assert not route.called


@respx.mock
def test_embeddings_are_never_budget_blocked():
    respx.post(OPENAI_EMBED).mock(return_value=httpx.Response(200, json=_vectors(1)))
    ledger = FakeLedger(allow=False)
    assert len(Embedder(_openai(), ledger=ledger).embed(["a"])) == 1
    assert len(ledger.records) == 1


@respx.mock
def test_usage_recorded_before_a_malformed_reply_fails():
    respx.post(OPENAI_EMBED).mock(
        return_value=httpx.Response(200, json={"data": [], "usage": {"prompt_tokens": 3}})
    )
    ledger = FakeLedger()
    with pytest.raises(ValueError):
        Embedder(_openai(), ledger=ledger).embed(["a"])
    assert len(ledger.records) == 1


def test_openai_backend_without_a_key_raises():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        Embedder(_openai(openai_api_key="")).embed(["a"])


def test_embed_papers_activity_does_not_retry_a_spend_limit(db_session, monkeypatch):
    from temporalio.exceptions import ApplicationError

    from app.temporal import activities
    from app.temporal.types import EmbedPapersInput

    class RefusedEmbedder:
        enabled = True
        model = "text-embedding-3-small"

        def __init__(self, *args, **kwargs):
            pass

        def document_text(self, title, abstract):
            return f"{title}\n\n{abstract}"

        def embed(self, texts):
            raise SpendLimitReached("out of credits")

    from app import models

    paper = models.Paper(arxiv_id="2611.0001", title="T", abstract="A")
    db_session.add(paper)
    db_session.commit()
    monkeypatch.setattr(activities, "Embedder", RefusedEmbedder)

    with pytest.raises(ApplicationError) as excinfo:
        activities.embed_papers(EmbedPapersInput(paper_ids=[paper.id]))
    assert excinfo.value.non_retryable is True
