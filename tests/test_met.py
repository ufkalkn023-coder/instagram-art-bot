import random

import pytest
import requests

import config
from src.museums import met
from src.museums.base import AdapterHTTPError


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class InvalidJSONResponse(FakeResponse):
    def json(self):
        raise ValueError("invalid JSON")


def _detail(object_id):
    return {
        "isPublicDomain": True,
        "title": f"Painting {object_id}",
        "objectName": "Painting",
        "medium": "Oil on canvas",
        "primaryImage": f"https://images.example/{object_id}.jpg",
    }


def test_met_uses_v11_search_and_keeps_v1_object_detail(monkeypatch):
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url == f"{config.MET_SEARCH_API_BASE}/search":
            return FakeResponse({"total": 1, "objectIDs": [437133]})
        return FakeResponse(_detail(437133))

    monkeypatch.setattr(met.requests, "get", request)

    candidates = met.MetAdapter().fetch_candidates(
        limit=1, query="portrait", rng=random.Random(7)
    )

    assert [candidate.source_id for candidate in candidates] == ["437133"]
    search_url, search_kwargs = calls[0]
    assert search_url == (
        "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
    )
    assert search_kwargs["params"] == {
        "hasImages": "true",
        "medium": "Paintings",
        "q": "portrait",
        "offset": 0,
        "limit": 500,
    }
    assert calls[1][0] == (
        "https://collectionapi.metmuseum.org/public/collection/v1/objects/437133"
    )


def test_met_pagination_is_bounded_and_samples_beyond_first_page(monkeypatch):
    search_params = []

    def request(url, **kwargs):
        if url == f"{config.MET_SEARCH_API_BASE}/search":
            params = dict(kwargs["params"])
            search_params.append(params)
            offset = params["offset"]
            page_limit = params["limit"]
            return FakeResponse(
                {
                    "total": 50_000,
                    "objectIDs": list(range(offset + 1, offset + page_limit + 1)),
                }
            )
        return FakeResponse(_detail(int(url.rsplit("/", 1)[1])))

    monkeypatch.setattr(met.requests, "get", request)

    candidates = met.MetAdapter().fetch_candidates(
        limit=3, query="painting", rng=random.Random(1)
    )

    assert len(search_params) == 2
    assert search_params[1]["offset"] > 0
    assert all(params["limit"] <= 500 for params in search_params)
    assert all(
        params["offset"] + params["limit"] <= 10_000
        for params in search_params
    )
    assert all(int(candidate.source_id) > 500 for candidate in candidates)


def test_met_broad_search_rng_is_deterministic(monkeypatch):
    calls = []

    def request(url, **kwargs):
        if url == f"{config.MET_SEARCH_API_BASE}/search":
            params = dict(kwargs["params"])
            calls.append((params["offset"], params["limit"]))
            offset = params["offset"]
            return FakeResponse(
                {
                    "total": 7_500,
                    "objectIDs": list(range(offset + 1, offset + params["limit"] + 1)),
                }
            )
        return FakeResponse(_detail(int(url.rsplit("/", 1)[1])))

    monkeypatch.setattr(met.requests, "get", request)

    first = met.MetAdapter().fetch_candidates(
        limit=4, query="painting", rng=random.Random(19)
    )
    first_calls = list(calls)
    calls.clear()
    second = met.MetAdapter().fetch_candidates(
        limit=4, query="painting", rng=random.Random(19)
    )

    assert [candidate.source_id for candidate in first] == [
        candidate.source_id for candidate in second
    ]
    assert first_calls == calls


@pytest.mark.parametrize("object_ids", [None, []])
def test_met_empty_search_is_healthy(monkeypatch, object_ids):
    monkeypatch.setattr(
        met.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"total": 0, "objectIDs": object_ids}
        ),
    )
    adapter = met.MetAdapter()

    assert adapter.fetch_candidates(query="no matches", rng=random.Random(2)) == []
    assert adapter.source_failure_category is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"total": "1", "objectIDs": [1]},
        {"total": 1, "objectIDs": "1"},
        {"total": 1, "objectIDs": [True]},
        {"total": 1, "objectIDs": []},
    ],
)
def test_met_malformed_search_response_fails_closed(monkeypatch, payload):
    monkeypatch.setattr(
        met.requests, "get", lambda *args, **kwargs: FakeResponse(payload)
    )
    adapter = met.MetAdapter()

    assert adapter.fetch_candidates(query="painting", rng=random.Random(2)) == []
    assert adapter.source_failure_category == "INVALID_RESPONSE"


def test_met_invalid_json_fails_closed(monkeypatch):
    monkeypatch.setattr(
        met.requests,
        "get",
        lambda *args, **kwargs: InvalidJSONResponse(None),
    )
    adapter = met.MetAdapter()

    assert adapter.fetch_candidates(query="painting", rng=random.Random(2)) == []
    assert adapter.source_failure_category == "INVALID_RESPONSE"


def test_met_http_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(
        met.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(None, status_code=503),
    )
    adapter = met.MetAdapter()

    assert adapter.fetch_candidates(query="painting", rng=random.Random(2)) == []
    assert adapter.source_failure_category == "API_ERROR"


@pytest.mark.parametrize(
    "status_code,expected_category",
    [(403, "HTTP_BLOCKED"), (429, "RATE_LIMITED")],
)
def test_met_403_and_429_raise_structured_failures(
    monkeypatch, status_code, expected_category
):
    monkeypatch.setattr(
        met.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(None, status_code=status_code),
    )
    adapter = met.MetAdapter()

    with pytest.raises(AdapterHTTPError) as error:
        adapter.fetch_candidates(query="painting", rng=random.Random(2))

    assert error.value.operation == "search"
    assert error.value.category == expected_category
    assert adapter.source_failure_category == expected_category


def test_met_network_failure_fails_closed(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(met.requests, "get", fail)
    adapter = met.MetAdapter()

    assert adapter.fetch_candidates(query="painting", rng=random.Random(2)) == []
    assert adapter.source_failure_category == "NETWORK_ERROR"
