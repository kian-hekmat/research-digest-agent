import json

import httpx
import pytest
import respx

from app.config import get_settings
from app.services import scholar
from app.services.scholar import fetch_max_author_h_index

BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


def _paper(*h_indices):
    return {"paperId": "abc", "authors": [{"authorId": str(i), "hIndex": h} for i, h in enumerate(h_indices)]}


@respx.mock
def test_returns_the_max_author_h_index_per_found_paper():
    route = respx.post(BATCH).mock(
        return_value=httpx.Response(
            200,
            json=[
                _paper(3, 41, 7),
                None,  # not indexed yet
                _paper(None, None),  # indexed, but no author has an h-index
                _paper(None, 12),
            ],
        )
    )

    out = fetch_max_author_h_index(["2610.00001", "2610.00002", "2610.00003", "2610.00004"])

    assert out == {"2610.00001": 41, "2610.00004": 12}
    request = route.calls.last.request
    assert request.url.params["fields"] == "authors.hIndex"
    assert json.loads(request.content) == {
        "ids": ["arXiv:2610.00001", "arXiv:2610.00002", "arXiv:2610.00003", "arXiv:2610.00004"]
    }
    assert "x-api-key" not in request.headers


@respx.mock
def test_none_indexed_yet_is_an_empty_result_not_an_error():
    """Observed live: a batch of only same-day papers gets a 400, not nulls."""
    respx.post(BATCH).mock(
        return_value=httpx.Response(400, json={"error": "No valid paper ids given"})
    )

    assert fetch_max_author_h_index(["2610.08789"]) == {}


@respx.mock
@pytest.mark.parametrize("status", [400, 429, 500])
def test_other_errors_raise_so_the_activity_retries(status):
    respx.post(BATCH).mock(return_value=httpx.Response(status, json={"message": "nope"}))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_max_author_h_index(["2610.00001"])


@respx.mock
def test_large_batches_are_split_at_the_endpoint_limit(monkeypatch):
    monkeypatch.setattr(scholar, "_BATCH_LIMIT", 2)
    route = respx.post(BATCH).mock(
        side_effect=lambda request: httpx.Response(
            200, json=[_paper(len(i)) for i in json.loads(request.content)["ids"]]
        )
    )

    out = fetch_max_author_h_index(["a", "bb", "ccc"])

    assert route.call_count == 2
    assert out == {"a": 7, "bb": 8, "ccc": 9}  # len("arXiv:" + id)


@respx.mock
def test_sends_the_api_key_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "semantic_scholar_api_key", "s2-test-key")
    route = respx.post(BATCH).mock(return_value=httpx.Response(200, json=[None]))

    fetch_max_author_h_index(["2610.00001"])

    assert route.calls.last.request.headers["x-api-key"] == "s2-test-key"
