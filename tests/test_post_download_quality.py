import logging
from pathlib import Path

from src import art_fetcher
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult


def _candidate(index):
    return NormalizedArtwork(
        source="aic",
        source_id=index,
        title=f"Artwork {index}",
        artist_name=f"Artist {index}",
        creation_date="1900",
        museum_name=f"Museum {index}",
        image_url=f"https://images.example/{index}.jpg",
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )


def _install_adapters(monkeypatch, candidates):
    class StaticAdapter:
        source_id = "test"

        def __init__(self, values):
            self.values = values

        def fetch_candidates(self, **kwargs):
            return self.values

    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [StaticAdapter(candidates)])


def _install_download_results(monkeypatch, dimensions):
    attempted = []

    def download(url, output_path):
        identifier = Path(url).stem
        attempted.append(identifier)
        Path(output_path).write_bytes(b"validated image")
        width, height = dimensions[identifier]
        return ImageValidationResult(True, width=width, height=height, image_format="JPEG", reason="ok")

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)
    return attempted


def test_single_post_rejects_measured_low_quality_and_preserves_serendipity(monkeypatch, tmp_path):
    low = _candidate("low")
    high = _candidate("high")
    _install_adapters(monkeypatch, [low, high])
    monkeypatch.setattr(art_fetcher.config, "OUTPUT_RAW_IMAGE_PATH", str(tmp_path / "raw.jpg"))
    monkeypatch.setattr(art_fetcher.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(art_fetcher.content_diversity, "get_candidate_metadata_features", lambda candidate: {})
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_museum_diversity", lambda *args: 0.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_visual_diversity", lambda *args: 0.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_discovery_score", lambda *args: 0.0)
    random_calls = []
    monkeypatch.setattr(
        art_fetcher,
        "calculate_serendipity_bonus",
        lambda run_seed, candidate_id: random_calls.append((run_seed, candidate_id)) or 1.5,
    )
    monkeypatch.setattr(
        art_fetcher,
        "calculate_quality_score",
        lambda candidate, weights: (
            {"aic_low": 90.0, "aic_high": 70.0}[candidate.canonical_id]
            if candidate.image_width is None
            else {"aic_low": 45.0, "aic_high": 70.0}[candidate.canonical_id]
        ),
    )
    attempted = _install_download_results(monkeypatch, {"low": (300, 700), "high": (1686, 1200)})

    artwork = art_fetcher.fetch_random_artwork(set())

    assert artwork["id"] == "aic_high"
    assert attempted == ["low", "high"]
    assert len(random_calls) == 2
    assert artwork["quality_score"] == 70.0
    assert artwork["selection_score"] == 71.5
    assert artwork["measurement_coverage"] == 1.0
    assert (artwork["image_width"], artwork["image_height"]) == (1686, 1200)


def test_selection_breakdown_is_mathematically_consistent():
    breakdown = art_fetcher.SelectionScoreBreakdown(
        quality_score=82.1,
        museum_adjustment=-3.0,
        visual_adjustment=4.0,
        discovery_adjustment=2.0,
        serendipity_adjustment=1.7,
    )

    assert breakdown.selection_score == 86.8


def test_single_selection_logs_breakdown_and_aggregate_rejections_without_query_secrets(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=art_fetcher.__name__)
    restricted = _candidate("restricted")
    restricted.rights_status = None
    duplicate = _candidate("duplicate")
    pre_reject = _candidate("pre-reject")
    image_failure = _candidate("image-failure")
    post_reject = _candidate("post-reject")
    selected = _candidate("selected")
    selected.image_url = "https://images.example/selected.jpg?api_key=selection-secret"
    _install_adapters(monkeypatch, [restricted, duplicate, pre_reject, image_failure, post_reject, selected])
    monkeypatch.setattr(art_fetcher.config, "OUTPUT_RAW_IMAGE_PATH", str(tmp_path / "raw.jpg"))
    monkeypatch.setattr(art_fetcher.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(art_fetcher.content_diversity, "get_candidate_metadata_features", lambda candidate: {})
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_museum_diversity", lambda *args: -3.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_visual_diversity", lambda *args: 4.0)
    monkeypatch.setattr(art_fetcher.content_diversity, "analyze_discovery_score", lambda *args: 2.0)
    monkeypatch.setenv(art_fetcher.SELECTION_SEED_ENV, "super-secret-looking-seed")
    monkeypatch.setattr(art_fetcher, "calculate_serendipity_bonus", lambda *args: 1.5)

    pre_scores = {
        "aic_pre-reject": 40.0,
        "aic_image-failure": 100.0,
        "aic_post-reject": 90.0,
        "aic_selected": 80.0,
    }
    post_scores = {"aic_post-reject": 45.0, "aic_selected": 70.0}
    monkeypatch.setattr(
        art_fetcher,
        "calculate_quality_score",
        lambda candidate, weights: (
            pre_scores[candidate.canonical_id]
            if candidate.image_width is None
            else post_scores[candidate.canonical_id]
        ),
    )

    def download(url, output_path):
        identifier = Path(url.split("?", 1)[0]).stem
        if identifier == "image-failure":
            return ImageValidationResult(False, reason="invalid_image")
        Path(output_path).write_bytes(b"validated image")
        dimensions = (300, 700) if identifier == "post-reject" else (2000, 1600)
        return ImageValidationResult(True, *dimensions, image_format="JPEG", reason="ok")

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)

    artwork = art_fetcher.fetch_random_artwork({"aic_duplicate"})

    assert artwork["id"] == "aic_selected"
    assert artwork["quality_score"] == 70.0
    assert artwork["selection_score"] == 74.5
    assert "selection_selected candidate=aic_selected" in caplog.text
    assert "museum=-3.00 visual=+4.00 discovery=+2.00 serendipity=+1.50 selection=74.50" in caplog.text
    assert "raw=6 rights_safe=5 history_new=4 quality_pass=3 downloads=3 selected=aic_selected" in caplog.text
    for reason in ("rights_unconfirmed:1", "history_duplicate:1", "pre_quality_below_threshold:1", "image_validation_failed:1", "post_quality_below_threshold:1"):
        assert reason in caplog.text
    assert "selection-secret" not in caplog.text
    assert "super-secret-looking-seed" not in caplog.text
    assert "selection_seed source=explicit" in caplog.text


def test_carousel_replaces_measured_low_quality_candidates_without_returning_partial(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=art_fetcher.__name__)
    low = _candidate("low")
    high_one = _candidate("high-one")
    high_two = _candidate("high-two")
    _install_adapters(monkeypatch, [low, high_one, high_two])
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        art_fetcher,
        "calculate_quality_score",
        lambda candidate, weights: (
            {"aic_low": 100.0, "aic_high-one": 90.0, "aic_high-two": 80.0}[candidate.canonical_id]
            if candidate.image_width is None
            else {"aic_low": 45.0, "aic_high-one": 80.0, "aic_high-two": 70.0}[candidate.canonical_id]
        ),
    )
    attempted = _install_download_results(
        monkeypatch,
        {"low": (300, 700), "high-one": (2000, 1600), "high-two": (843, 600)},
    )

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=2, color_tone="warm")

    assert [artwork["id"] for artwork in artworks] == ["aic_high-one", "aic_high-two"]
    assert attempted == ["low", "high-one", "high-two"]
    assert len(artworks) == 2
    assert all(artwork["measurement_coverage"] == 1.0 for artwork in artworks)
    assert [(artwork["image_width"], artwork["image_height"]) for artwork in artworks] == [
        (2000, 1600),
        (843, 600),
    ]
    assert "carousel_selection_summary requested=2 selected=2" in caplog.text
    assert "source_distribution=aic:2" in caplog.text
    assert "post_quality_below_threshold:1" in caplog.text
