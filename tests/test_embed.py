import json

import httpx
import pytest
import respx

from app.config import Settings
from app.services.embed import Embedder, document_text

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
    assert document_text("A Title", "An abstract.") == "title: A Title | text: An abstract."


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
