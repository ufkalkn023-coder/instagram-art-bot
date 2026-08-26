import logging
import random
from pathlib import Path
from types import SimpleNamespace

import main
from src import art_fetcher, history_tracker
from src.instagram_image import (
    InstagramImagePublishability,
    InstagramImagePublishabilityReason,
    PreparedSingleImage,
    SingleImageProcessing,
)
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult


def _candidate(identifier, *, artist, region, title, museum="Museum"):
    return NormalizedArtwork(
        source="aic",
        source_id=identifier,
        title=title,
        artist_name=artist,
        creation_date="1900",
        medium="Oil on canvas",
        classification="Painting",
        museum_name=museum,
        image_url=f"https://images.example/{identifier}.jpg",
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
        region=region,
    )


def _install_single_download(monkeypatch, tmp_path, attempted):
    monkeypatch.setattr(art_fetcher.config, "OUTPUT_RAW_IMAGE_PATH", str(tmp_path / "raw.jpg"))

    def download(url, output_path):
        attempted.append(Path(url).stem)
        Path(output_path).write_bytes(b"validated image")
        return ImageValidationResult(True, width=2000, height=1600, image_format="JPEG", reason="ok")

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)


def test_run_single_post_uses_canonical_selector_without_grid_or_carousel_selection(monkeypatch):
    artwork = {
        "id": "aic_1", "title": "Artwork", "artist": "Artist", "date": "1900", "museum": "Museum",
        "local_image_path": "downloaded.jpg", "quality_score": 90.0, "selection_score": 90.0,
    }
    calls = []
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: (_ for _ in ()).throw(AssertionError("grid read")))
    monkeypatch.setattr(main.art_fetcher, "fetch_themed_artworks", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("carousel selector")))
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda posted_ids, **kwargs: calls.append((posted_ids, kwargs)) or iter([artwork]),
    )
    publishability = InstagramImagePublishability(
        publishable=True,
        reason=InstagramImagePublishabilityReason.SUPPORTED_AS_IS,
        width=1000,
        height=1200,
        aspect_ratio=1000 / 1200,
        image_format="JPEG",
        file_size=100,
        exif_orientation=1,
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda *args: PreparedSingleImage(
            path="downloaded.jpg",
            source=publishability,
            publishability=publishability,
            processing=SingleImageProcessing.ZERO_TOUCH,
            source_bytes_preserved=True,
            compatibility_conversion=False,
        ),
    )
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(main.content_diversity, "select_content_type", lambda history: "SINGLE_ARTWORK")
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda *args, **kwargs: None)

    main.run_single_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert calls == [(set(), {"max_candidates": main.SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT})]


def test_single_acquisition_omits_color_query_and_isolates_failed_sources(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=art_fetcher.__name__)
    observed = []
    candidate = _candidate("safe", artist="Artist", region="europe", title="Landscape")

    class FailingAdapter:
        source_id = "failed"

        def fetch_candidates(self, **kwargs):
            raise RuntimeError("unavailable")

    class RecordingAdapter:
        source_id = "recording"

        def fetch_candidates(self, **kwargs):
            observed.append(kwargs)
            return [candidate]

    attempted = []
    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [FailingAdapter(), RecordingAdapter()])
    monkeypatch.setattr(history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda candidate, weights: 80.0)
    monkeypatch.setattr(art_fetcher, "calculate_serendipity_bonus", lambda *args: 0.0)
    _install_single_download(monkeypatch, tmp_path, attempted)

    artwork = art_fetcher.fetch_single_artwork(set())

    assert artwork["id"] == "aic_safe"
    assert attempted == ["safe"]
    assert observed and all("query" not in request for request in observed)
    assert "Museum source failed failed; continuing with remaining sources." in caplog.text


def test_single_selector_penalizes_recent_artist_and_region_while_rewarding_fresh_editorial_fit(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=art_fetcher.__name__)
    repeated = _candidate("repeated", artist="Repeated Artist", region="europe", title="Portrait of a sitter")
    fresh = _candidate("fresh", artist="Fresh Artist", region="latin_america_caribbean", title="Landscape with river")
    fresh.creation_date = "1800"
    fresh.medium = "Watercolor on paper"

    class StaticAdapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return [repeated, fresh]

    recent_history = [
        {
            "artist_name": "Repeated Artist", "region": "europe", "visual_category": "portrait",
            "period": "1900s", "medium": "painting",
        }
    ] * 3
    attempted = []
    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [StaticAdapter()])
    monkeypatch.setattr(history_tracker, "get_recent_history", lambda: recent_history)
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda candidate, weights: 80.0)
    monkeypatch.setattr(art_fetcher, "calculate_serendipity_bonus", lambda *args: 0.0)
    _install_single_download(monkeypatch, tmp_path, attempted)

    artwork = art_fetcher.fetch_single_artwork(set())

    assert artwork["id"] == "aic_fresh"
    assert attempted == ["fresh", "repeated"]
    assert "region=latin_america_caribbean" in caplog.text
    assert "region_adjustment=+2.00" in caplog.text


def test_single_quality_gate_rejects_editorially_boosted_low_quality_candidate(monkeypatch, tmp_path):
    low = _candidate("low", artist="Low", region="europe", title="Landscape")
    safe = _candidate("safe", artist="Safe", region="europe", title="Landscape")

    class StaticAdapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return [low, safe]

    attempted = []
    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [StaticAdapter()])
    monkeypatch.setattr(history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(
        art_fetcher,
        "calculate_quality_score",
        lambda candidate, weights: 45.0 if candidate.canonical_id == "aic_low" else 50.0,
    )
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_museum_diversity", lambda *args: 20.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_visual_diversity", lambda *args: 20.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_discovery_score", lambda *args: 20.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_regional_diversity", lambda *args: 20.0)
    monkeypatch.setattr(art_fetcher, "calculate_serendipity_bonus", lambda *args: 5.0)
    _install_single_download(monkeypatch, tmp_path, attempted)

    artwork = art_fetcher.fetch_single_artwork(set())

    assert artwork["id"] == "aic_safe"
    assert attempted == ["safe"]


def test_seeded_single_selection_is_repeatable_and_serendipity_is_bounded(monkeypatch, tmp_path):
    candidates = [
        _candidate("one", artist="Artist One", region="europe", title="Landscape"),
        _candidate("two", artist="Artist Two", region="north_america", title="Landscape"),
    ]

    class StaticAdapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    attempted = []
    monkeypatch.setenv(art_fetcher.SELECTION_SEED_ENV, "single-selection-fixture")
    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [StaticAdapter()])
    monkeypatch.setattr(history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda candidate, weights: 80.0)
    _install_single_download(monkeypatch, tmp_path, attempted)

    first = art_fetcher.fetch_single_artwork(set())
    second = art_fetcher.fetch_single_artwork(set())

    assert first["id"] == second["id"]
    expected_finalist_pool = [candidate.source_id for candidate in candidates]
    assert attempted == expected_finalist_pool * 2
    assert 0.0 <= art_fetcher.calculate_serendipity_bonus("fixture", "candidate") <= 5.0

    random.seed(20260824)
    expected_next = random.random()
    random.seed(20260824)
    art_fetcher.calculate_serendipity_bonus("fixture", "candidate")
    assert random.random() == expected_next
