import random
from urllib.parse import parse_qs, urlparse

import pytest

from src import art_fetcher
from src.museums import aic, cleveland, met, rijksmuseum


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _rng(seed, source_id, stage_name="single_post", query=None):
    return art_fetcher._museum_adapter_rng(
        art_fetcher.SelectionRunSeed(seed, "explicit"), source_id, stage_name, query
    )


def test_seeded_adapter_requests_keep_existing_page_offset_and_sample_bounds(monkeypatch):
    aic_urls = []
    monkeypatch.setattr(
        aic.requests,
        "get",
        lambda url, **kwargs: (aic_urls.append(url) or FakeResponse({"data": []})),
    )
    aic.AICAdapter().fetch_candidates(rng=_rng("seed-alpha", "aic"))

    cleveland_urls = []
    monkeypatch.setattr(
        cleveland.requests,
        "get",
        lambda url, **kwargs: (cleveland_urls.append(url) or FakeResponse({"data": []})),
    )
    cleveland.ClevelandAdapter().fetch_candidates(rng=_rng("seed-alpha", "cleveland"))

    rijks_urls = []
    monkeypatch.setenv("RIJKSMUSEUM_API_KEY", "test-key")
    monkeypatch.setattr(
        rijksmuseum.requests,
        "get",
        lambda url, **kwargs: (rijks_urls.append(url) or FakeResponse({"artObjects": []})),
    )
    rijksmuseum.RijksmuseumAdapter().fetch_candidates(rng=_rng("seed-alpha", "rijksmuseum"))

    assert parse_qs(urlparse(aic_urls[0]).query)["page"] == ["4"]
    assert parse_qs(urlparse(cleveland_urls[0]).query)["skip"] == ["379"]
    assert parse_qs(urlparse(rijks_urls[0]).query)["p"] == ["50"]


def test_direct_adapter_use_without_an_rng_keeps_existing_random_exploration(monkeypatch):
    requested_urls = []
    monkeypatch.setattr(aic.random, "randint", lambda lower, upper: 17)
    monkeypatch.setattr(
        aic.requests,
        "get",
        lambda url, **kwargs: (requested_urls.append(url) or FakeResponse({"data": []})),
    )

    aic.AICAdapter().fetch_candidates()

    assert parse_qs(urlparse(requested_urls[0]).query)["page"] == ["17"]


def test_met_seeded_search_term_and_object_sample_are_reproducible(monkeypatch):
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        if url.endswith("/search?hasImages=true&isPublicDomain=true&medium=Paintings&q=baroque painting"):
            return FakeResponse({"objectIDs": list(range(1, 11))})
        object_id = int(url.rsplit("/", 1)[1])
        return FakeResponse(
            {
                "isPublicDomain": True,
                "title": f"Painting {object_id}",
                "objectName": "Painting",
                "primaryImage": f"https://images.example/{object_id}.jpg",
            }
        )

    monkeypatch.setattr(met.requests, "get", request)

    first = met.MetAdapter().fetch_candidates(limit=3, rng=_rng("seed-alpha", "met"))
    first_calls = list(calls)
    calls.clear()
    second = met.MetAdapter().fetch_candidates(limit=3, rng=_rng("seed-alpha", "met"))

    assert [candidate.source_id for candidate in first] == ["8", "9", "5"]
    assert [candidate.source_id for candidate in second] == ["8", "9", "5"]
    assert first_calls == calls


def test_different_seeds_have_known_distinct_adapter_choices():
    assert _rng("seed-alpha", "aic").randint(1, 20) == 4
    assert _rng("seed-beta", "aic").randint(1, 20) == 10
    assert _rng("seed-alpha", "cleveland").randint(0, 500) == 379
    assert _rng("seed-beta", "cleveland").randint(0, 500) == 347


def test_adapter_rngs_are_isolated_from_call_order():
    ranges = {"aic": (1, 20), "cleveland": (0, 500), "met": (0, 7), "rijksmuseum": (0, 100)}

    def choices(order):
        return {
            source_id: _rng("seed-alpha", source_id).randint(*ranges[source_id])
            for source_id in order
        }

    assert choices(["aic", "cleveland", "met", "rijksmuseum"]) == choices(
        ["met", "aic", "rijksmuseum", "cleveland"]
    )


def test_seeded_adapter_rng_does_not_change_global_random_state():
    original_state = random.getstate()
    try:
        random.seed(20260822)
        expected_next = random.random()
        random.seed(20260822)

        _rng("seed-alpha", "aic").randint(1, 20)
        _rng("seed-alpha", "met").sample(list(range(10)), 3)

        assert random.random() == expected_next
    finally:
        random.setstate(original_state)


def test_github_run_seed_ignores_retry_attempt_for_adapter_rng():
    first = art_fetcher.resolve_selection_run_seed({"GITHUB_RUN_ID": "run-42", "GITHUB_RUN_ATTEMPT": "1"})
    retried = art_fetcher.resolve_selection_run_seed({"GITHUB_RUN_ID": "run-42", "GITHUB_RUN_ATTEMPT": "7"})

    assert first == retried
    assert _rng(first.value, "aic").randint(1, 20) == _rng(retried.value, "aic").randint(1, 20)


def test_single_selection_resolves_one_root_seed_and_passes_namespaced_rngs(monkeypatch):
    observed = {}
    seed_calls = []

    class EmptyAdapter:
        def __init__(self, source_id):
            self.source_id = source_id

        def fetch_candidates(self, *, rng, **kwargs):
            observed[self.source_id] = rng.randint(0, 1_000_000)
            return []

    monkeypatch.setattr(
        art_fetcher,
        "resolve_selection_run_seed",
        lambda: seed_calls.append(True) or art_fetcher.SelectionRunSeed("local-fixture", "local"),
    )
    monkeypatch.setattr(
        art_fetcher,
        "_museum_adapters",
        lambda: [
            EmptyAdapter("aic"),
            EmptyAdapter("cleveland"),
            EmptyAdapter("met"),
            EmptyAdapter("rijksmuseum"),
        ],
    )

    with pytest.raises(RuntimeError, match="No new public domain"):
        art_fetcher.fetch_random_artwork(set())

    assert seed_calls == [True]
    assert observed == {
        source_id: art_fetcher._museum_adapter_rng(
            art_fetcher.SelectionRunSeed("local-fixture", "local"), source_id, "single_post", None
        ).randint(0, 1_000_000)
        for source_id in observed
    }


def test_carousel_passes_stage_specific_namespaced_rngs(monkeypatch):
    observed = []

    class EmptyAdapter:
        source_id = "aic"

        def fetch_candidates(self, *, query, rng, **kwargs):
            observed.append((query, rng.randint(1, 20)))
            return []

    monkeypatch.setattr(
        art_fetcher,
        "resolve_selection_run_seed",
        lambda: art_fetcher.SelectionRunSeed("carousel-fixture", "explicit"),
    )
    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [EmptyAdapter() for _ in range(4)])

    with pytest.raises(art_fetcher.CarouselSelectionError):
        art_fetcher.fetch_themed_artworks(set(), "portrait", count=2, color_tone="warm")

    expected = []
    for stage_name, query in (
        ("tone_and_theme", "warm portrait"),
        ("theme_only", "portrait"),
        ("expanded_theme", "portrait"),
    ):
        expected.extend(
            [
                (
                    query,
                    art_fetcher._museum_adapter_rng(
                        art_fetcher.SelectionRunSeed("carousel-fixture", "explicit"),
                        "aic",
                        stage_name,
                        query,
                    ).randint(1, 20),
                )
            ]
            * 4
        )
    assert observed == expected
