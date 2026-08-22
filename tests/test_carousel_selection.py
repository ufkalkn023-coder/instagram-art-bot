import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from src import art_fetcher
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult


def _candidate(index, *, artist=None, museum=None, source="aic", source_id=None, rights=True):
    return NormalizedArtwork(
        source=source,
        source_id=source_id or str(index),
        title=f"Artwork {index}",
        artist_name=artist if artist is not None else f"Artist {index}",
        creation_date="1888",
        medium="Oil on canvas",
        classification="Painting",
        museum_name=museum or f"Museum {int(index) % 4 if str(index).isdigit() else 0}",
        image_url=f"https://images.example/{index}.jpg",
        is_public_domain=rights,
        rights_status="CONFIRMED_PUBLIC_DOMAIN" if rights else None,
    )


def _install_candidates(monkeypatch, candidates):
    class StaticAdapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(art_fetcher, "AICAdapter", lambda: StaticAdapter())
    monkeypatch.setattr(art_fetcher, "ClevelandAdapter", lambda: StaticAdapter())
    monkeypatch.setattr(art_fetcher, "MetAdapter", lambda: StaticAdapter())
    monkeypatch.setattr(art_fetcher, "RijksmuseumAdapter", lambda: StaticAdapter())


def _install_downloads(monkeypatch, tmp_path, invalid_ids=()):
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    invalid_ids = set(invalid_ids)
    attempted = []

    def download(url, output_path):
        artwork_id = Path(url).stem
        attempted.append(artwork_id)
        if artwork_id in invalid_ids:
            return ImageValidationResult(False, reason="invalid_image")
        Path(output_path).write_bytes(b"validated image")
        return ImageValidationResult(True, width=2000, height=1600, image_format="JPEG", reason="ok")

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)
    return attempted


def _set_scores(monkeypatch, candidates, scores=None):
    scores = scores or {candidate.canonical_id: 100 - index for index, candidate in enumerate(candidates)}
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda candidate, weights: scores[candidate.canonical_id])


@pytest.mark.parametrize("count", [1, 2, 10])
def test_supported_counts_return_exactly_requested_artworks(monkeypatch, tmp_path, count):
    candidates = [_candidate(index) for index in range(10)]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=count, color_tone="warm")

    assert len(artworks) == count


@pytest.mark.parametrize("count", [0, 11])
def test_invalid_selection_counts_fail_before_fetching(monkeypatch, count):
    monkeypatch.setattr(art_fetcher, "AICAdapter", lambda: pytest.fail("fetch must not run"))

    with pytest.raises(ValueError, match="Artwork selection count"):
        art_fetcher.fetch_themed_artworks(set(), "portrait", count=count, color_tone="warm")


def test_carousel_wrapper_rejects_a_single_item_before_fetching(monkeypatch):
    monkeypatch.setattr(art_fetcher, "fetch_themed_artworks", lambda *args, **kwargs: pytest.fail("fetch must not run"))

    with pytest.raises(ValueError, match="Carousel count"):
        art_fetcher.fetch_carousel_artworks(set(), "portrait", count=1, color_tone="warm")


def test_single_selection_replaces_an_invalid_candidate(monkeypatch, tmp_path):
    candidates = [_candidate(index) for index in range(2)]
    _install_candidates(monkeypatch, candidates)
    attempted = _install_downloads(monkeypatch, tmp_path, invalid_ids={"0"})
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=1, color_tone="warm")

    assert [artwork["id"] for artwork in artworks] == ["aic_1"]
    assert attempted[:2] == ["0", "1"]


def test_single_selection_with_one_valid_candidate_returns_exactly_one(monkeypatch, tmp_path):
    candidates = [_candidate(1)]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=1, color_tone="warm")

    assert [artwork["id"] for artwork in artworks] == ["aic_1"]


def test_single_selection_without_valid_candidates_fails_explicitly(monkeypatch, tmp_path):
    candidates = [_candidate(0)]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path, invalid_ids={"0"})
    _set_scores(monkeypatch, candidates)

    with pytest.raises(art_fetcher.CarouselSelectionError, match=r"requested=1.*selected=0"):
        art_fetcher.fetch_themed_artworks(set(), "portrait", count=1, color_tone="warm")


@pytest.mark.parametrize("count", [2, 8, 10])
def test_carousel_wrapper_accepts_supported_counts(monkeypatch, count):
    calls = []
    monkeypatch.setattr(
        art_fetcher,
        "fetch_themed_artworks",
        lambda *args: calls.append(args) or [object()] * count,
    )

    assert len(art_fetcher.fetch_carousel_artworks(set(), "portrait", count=count, color_tone="warm")) == count
    assert calls[0][2] == count


def test_exact_count_uses_only_eight_when_more_candidates_are_available(monkeypatch, tmp_path):
    candidates = [_candidate(index) for index in range(10)]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert [artwork["id"] for artwork in artworks] == [candidate.canonical_id for candidate in candidates[:8]]


def test_seven_valid_candidates_raise_explicit_selection_error(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=art_fetcher.__name__)
    candidates = [_candidate(index) for index in range(7)]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    with pytest.raises(art_fetcher.CarouselSelectionError, match=r"requested=8.*safe_candidates=7.*selected=7"):
        art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert "carousel_selection_summary requested=8 selected=7" in caplog.text
    assert "result=carousel_insufficient_candidates" in caplog.text


def test_invalid_image_is_replaced_by_next_safe_candidate(monkeypatch, tmp_path):
    candidates = [_candidate(index) for index in range(9)]
    _install_candidates(monkeypatch, candidates)
    attempted = _install_downloads(monkeypatch, tmp_path, invalid_ids={"0"})
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert "aic_0" not in [artwork["id"] for artwork in artworks]
    assert "0" in attempted and "8" in attempted
    assert len(artworks) == 8


def test_canonical_duplicate_is_selected_only_once(monkeypatch, tmp_path):
    duplicate_aic = _candidate("first", source="aic", source_id="1")
    duplicate_legacy = _candidate("second", source="artic", source_id="1")
    candidates = [duplicate_aic, duplicate_legacy, *[_candidate(index) for index in range(2, 10)]]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert [artwork["id"] for artwork in artworks].count("aic_1") == 1
    assert len({artwork["id"] for artwork in artworks}) == 8


def test_artist_diversity_prefers_one_artwork_per_known_artist(monkeypatch, tmp_path):
    candidates = [_candidate(0, artist="Artist A"), _candidate(1, artist="Artist A")]
    candidates.extend(_candidate(index, artist=f"Artist {index}") for index in range(2, 9))
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert sum(artwork["artist"] == "Artist A" for artwork in artworks) == 1


def test_artist_relaxation_is_limited_to_two(monkeypatch, tmp_path):
    candidates = [_candidate(0, artist="Artist A"), _candidate(1, artist="Artist A")]
    candidates.extend(_candidate(index, artist=f"Artist {index}") for index in range(2, 8))
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert sum(artwork["artist"] == "Artist A" for artwork in artworks) == 2


def test_unknown_artist_does_not_block_a_complete_carousel(monkeypatch, tmp_path):
    candidates = [_candidate(index, artist="Unknown Artist") for index in range(8)]
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates)

    assert len(art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")) == 8


def test_museum_cap_prevents_dominance_and_single_museum_pool_fails(monkeypatch, tmp_path):
    diverse_candidates = [
        *[_candidate(index, museum="Museum A") for index in range(4)],
        *[_candidate(index, museum="Museum B") for index in range(4, 7)],
        *[_candidate(index, museum="Museum C") for index in range(7, 10)],
    ]
    _install_candidates(monkeypatch, diverse_candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, diverse_candidates)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")
    assert max(sum(artwork["museum"] == museum for artwork in artworks) for museum in {a["museum"] for a in artworks}) <= 3

    single_museum_candidates = [_candidate(index, museum="Museum A") for index in range(8)]
    _install_candidates(monkeypatch, single_museum_candidates)
    _set_scores(monkeypatch, single_museum_candidates)
    with pytest.raises(art_fetcher.CarouselSelectionError, match="selected=4"):
        art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")


def test_quality_and_rights_remain_prerequisites_for_diversity(monkeypatch, tmp_path):
    candidates = [_candidate(index) for index in range(8)]
    low_quality = _candidate(8)
    unconfirmed = _candidate(9, rights=False)
    candidates.extend([low_quality, unconfirmed])
    scores = {candidate.canonical_id: 100 - index for index, candidate in enumerate(candidates)}
    scores[low_quality.canonical_id] = 49
    _install_candidates(monkeypatch, candidates)
    _install_downloads(monkeypatch, tmp_path)
    _set_scores(monkeypatch, candidates, scores)

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=8, color_tone="warm")

    assert low_quality.canonical_id not in [artwork["id"] for artwork in artworks]
    assert unconfirmed.canonical_id not in [artwork["id"] for artwork in artworks]


def test_selection_failure_stops_before_reservation_gemini_and_instagram(monkeypatch):
    calls = []
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda: "warm")
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_carousel_artworks",
        lambda *args, **kwargs: (_ for _ in ()).throw(art_fetcher.CarouselSelectionError("insufficient")),
    )
    monkeypatch.setattr(main.history_tracker, "reserve_artwork", lambda artwork: calls.append("reserve"))
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args: calls.append("gemini"))
    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        lambda **kwargs: calls.append("instagram"),
    )

    with pytest.raises(art_fetcher.CarouselSelectionError):
        main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert calls == []
