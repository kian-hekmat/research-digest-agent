import json

import httpx
import pytest
import respx

from app.config import get_settings
from app.services import scholar
from app.services.scholar import fetch_max_author_h_index

S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
OA_WORKS = "https://api.openalex.org/works"
OA_AUTHORS = "https://api.openalex.org/authors"


@pytest.fixture
def s2_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "semantic_scholar_api_key", "s2-test-key")


# ---------- source selection ----------
@respx.mock
def test_without_a_key_uses_openalex_and_never_semantic_scholar():
    s2 = respx.post(S2_BATCH).mock(return_value=httpx.Response(200, json=[]))
    works = respx.get(OA_WORKS).mock(return_value=httpx.Response(200, json={"results": []}))

    assert fetch_max_author_h_index(["2610.00001"]) == {}

    assert works.called and not s2.called


@respx.mock
def test_with_a_key_uses_semantic_scholar_and_never_openalex(s2_key):
    s2 = respx.post(S2_BATCH).mock(return_value=httpx.Response(200, json=[None]))
    works = respx.get(OA_WORKS).mock(return_value=httpx.Response(200, json={"results": []}))

    fetch_max_author_h_index(["2610.00001"])

    assert s2.called and not works.called
    assert s2.calls.last.request.headers["x-api-key"] == "s2-test-key"


def test_empty_input_makes_no_request():
    assert fetch_max_author_h_index([]) == {}  # respx not active: a request would fail


# ---------- OpenAlex ----------
def _work(arxiv_id, *author_ids):
    return {
        # OpenAlex returns DOIs lowercased, as URLs.
        "doi": f"https://doi.org/10.48550/arxiv.{arxiv_id}".lower(),
        "authorships": [
            {"author": {"id": f"https://openalex.org/{a}" if a else None}} for a in author_ids
        ],
    }


def _author(author_id, h_index):
    return {"id": f"https://openalex.org/{author_id}", "summary_stats": {"h_index": h_index}}


@respx.mock
def test_openalex_max_author_h_index_per_found_paper():
    works = respx.get(OA_WORKS).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _work("2610.00001", "A1", "A2", None),  # an author without an id
                    _work("2610.00003", "A3"),  # author returned without h-index stats
                    # 2610.00002: not indexed yet - absent from results
                ]
            },
        )
    )
    authors = respx.get(OA_AUTHORS).mock(
        return_value=httpx.Response(
            200,
            json={"results": [_author("A1", 4), _author("A2", 37), {"id": "https://openalex.org/A3"}]},
        )
    )

    out = fetch_max_author_h_index(["2610.00001", "2610.00002", "2610.00003"])

    assert out == {"2610.00001": 37}
    assert works.calls.last.request.url.params["filter"] == (
        "doi:10.48550/arxiv.2610.00001|10.48550/arxiv.2610.00002|10.48550/arxiv.2610.00003"
    )
    assert authors.calls.last.request.url.params["filter"] == "id:A1|A2|A3"


@respx.mock
def test_openalex_splits_or_filters_at_its_limit(monkeypatch):
    monkeypatch.setattr(scholar, "_OPENALEX_OR_LIMIT", 2)

    def works_reply(request):
        dois = request.url.params["filter"].removeprefix("doi:").split("|")
        return httpx.Response(
            200, json={"results": [_work(d.removeprefix("10.48550/arxiv."), f"A-{d[-1]}") for d in dois]}
        )

    def authors_reply(request):
        ids = request.url.params["filter"].removeprefix("id:").split("|")
        return httpx.Response(200, json={"results": [_author(i, int(i[-1])) for i in ids]})

    works = respx.get(OA_WORKS).mock(side_effect=works_reply)
    authors = respx.get(OA_AUTHORS).mock(side_effect=authors_reply)

    out = fetch_max_author_h_index(["1", "2", "3"])

    assert works.call_count == 2 and authors.call_count == 2
    assert out == {"1": 1, "2": 2, "3": 3}


@respx.mock
@pytest.mark.parametrize("status", [429, 500])
def test_openalex_errors_raise_so_the_activity_retries(status):
    respx.get(OA_WORKS).mock(return_value=httpx.Response(status, json={"error": "nope"}))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_max_author_h_index(["2610.00001"])


# ---------- Semantic Scholar ----------
def _s2_paper(*h_indices):
    return {"paperId": "abc", "authors": [{"authorId": str(i), "hIndex": h} for i, h in enumerate(h_indices)]}


@respx.mock
def test_s2_returns_the_max_author_h_index_per_found_paper(s2_key):
    route = respx.post(S2_BATCH).mock(
        return_value=httpx.Response(
            200,
            json=[
                _s2_paper(3, 41, 7),
                None,  # not indexed yet
                _s2_paper(None, None),  # indexed, but no author has an h-index
                _s2_paper(None, 12),
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


@respx.mock
def test_s2_none_indexed_yet_is_an_empty_result_not_an_error(s2_key):
    """Observed live: a batch of only same-day papers gets a 400, not nulls."""
    respx.post(S2_BATCH).mock(
        return_value=httpx.Response(400, json={"error": "No valid paper ids given"})
    )

    assert fetch_max_author_h_index(["2610.08789"]) == {}


@respx.mock
@pytest.mark.parametrize("status", [400, 429, 500])
def test_s2_other_errors_raise_so_the_activity_retries(s2_key, status):
    respx.post(S2_BATCH).mock(return_value=httpx.Response(status, json={"message": "nope"}))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_max_author_h_index(["2610.00001"])


@respx.mock
def test_s2_large_batches_are_split_at_the_endpoint_limit(s2_key, monkeypatch):
    monkeypatch.setattr(scholar, "_S2_BATCH_LIMIT", 2)
    route = respx.post(S2_BATCH).mock(
        side_effect=lambda request: httpx.Response(
            200, json=[_s2_paper(len(i)) for i in json.loads(request.content)["ids"]]
        )
    )

    out = fetch_max_author_h_index(["a", "bb", "ccc"])

    assert route.call_count == 2
    assert out == {"a": 7, "bb": 8, "ccc": 9}  # len("arXiv:" + id)
