from pathlib import Path

import pytest

from src import art_fetcher, content_diversity, history_tracker
from src.models import NormalizedArtwork
from src.museums import aic
from src.quality_filter import ImageValidationResult
from src.region import infer_region


def _candidate(index, region):
    return NormalizedArtwork(
        source="aic", source_id=str(index), title=f"Artwork {index}",
        artist_name=f"Artist {index}", creation_date="1900", medium="Oil on canvas",
        classification="Painting", museum_name=f"Museum {index}",
        image_url=f"https://images.example/{index}.jpg", is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN", region=region,
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"culture": "Japanese"}, "east_asia"),
        ({"geography": "China"}, "east_asia"),
        ({"artist_nationality": "French"}, "europe"),
        ({"artist_nationality": "Italian"}, "europe"),
        ({"geography": "Netherlands"}, "europe"),
        ({"style_or_period": "Edo period"}, "east_asia"),
        ({"culture": "Asian Art"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_region_inference_is_conservative_and_deterministic(metadata, expected):
    assert infer_region(**metadata) == expected
    assert infer_region(**metadata) == expected


def test_explicit_culture_has_priority_over_place_and_nationality():
    assert infer_region(culture="Japanese", geography="France", artist_nationality="French") == "east_asia"
    assert infer_region(artist_nationality="Romanian") == "europe"


def test_model_normalizes_invalid_or_missing_region_values_to_unknown():
    artwork = NormalizedArtwork(source="test", source_id="1", museum_name="Test", region="unclassified")
    assert artwork.region == "unknown"


def test_regional_scoring_is_region_agnostic_and_ignores_unknown():
    assert content_diversity.analyze_regional_diversity("latin_america_caribbean", []) == 2.0
    assert content_diversity.analyze_regional_diversity("east_asia", [{"region": "east_asia"}] * 3) == -18.0
    assert content_diversity.analyze_regional_diversity("europe", [{"region": "europe"}] * 3) == -18.0
    assert content_diversity.analyze_regional_diversity("unknown", [{"region": "east_asia"}] * 6) == 0.0


def test_legacy_history_without_region_is_valid_and_new_reservation_stores_region(monkeypatch):
    history = {"posted_artworks": [{"id": "met_old", "status": "PUBLISHED"}]}
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda value, etag: uploads.append((value, etag)))

    assert history_tracker.get_recent_history() == history["posted_artworks"]
    assert content_diversity.analyze_regional_diversity("east_asia", history_tracker.get_recent_history()) == 2.0
    history_tracker.reserve_artwork({"id": "aic_new", "title": "A", "artist": "B", "region": "europe"})
    assert uploads[-1][0]["posted_artworks"][-1]["region"] == "europe"


def _install_carousel(monkeypatch, tmp_path, candidates):
    class StaticAdapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [StaticAdapter()])
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda candidate, weights: 100.0 - int(candidate.source_id))

    def download(url, output_path):
        Path(output_path).write_bytes(b"image")
        return ImageValidationResult(True, width=2000, height=1600, image_format="JPEG", reason="ok")

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)


def test_carousel_strict_region_cap_is_two_when_an_alternative_exists(monkeypatch, tmp_path):
    candidates = [
        *[_candidate(index, "east_asia") for index in range(3)],
        *[_candidate(index, "europe") for index in range(3, 5)],
        *[_candidate(index, "north_america") for index in range(5, 7)],
        _candidate(7, "middle_east_north_africa"),
        _candidate(8, "latin_america_caribbean"),
    ]
    _install_carousel(monkeypatch, tmp_path, candidates)

    regions = [artwork["region"] for artwork in art_fetcher.fetch_themed_artworks(set(), "portrait", 8, "warm")]
    assert regions.count("east_asia") == 2
    assert max(regions.count(region) for region in set(regions)) <= 2


def test_carousel_relaxes_known_region_cap_to_three_only_when_needed(monkeypatch, tmp_path):
    candidates = [
        *[_candidate(index, "east_asia") for index in range(3)],
        *[_candidate(index, "europe") for index in range(3, 6)],
        *[_candidate(index, "north_america") for index in range(6, 8)],
    ]
    _install_carousel(monkeypatch, tmp_path, candidates)

    regions = [artwork["region"] for artwork in art_fetcher.fetch_themed_artworks(set(), "portrait", 8, "warm")]
    assert regions.count("east_asia") == 3
    assert regions.count("europe") == 3
    assert max(regions.count(region) for region in set(regions)) <= 3


def test_aic_adapter_preserves_structured_geography_and_style(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"data": [{
                "id": 1, "title": "Untitled", "artist_title": "Unknown Artist",
                "artist_display": "Japanese, active 1800", "medium_display": "Ink on paper",
                "classification_title": "Painting", "place_of_origin": "Japan",
                "department_title": "Arts of Asia", "style_titles": ["Edo period"],
                "image_id": "image", "is_public_domain": True,
            }]}

    monkeypatch.setattr(aic.requests, "get", lambda *args, **kwargs: Response())
    artwork = aic.AICAdapter().fetch_candidates(limit=1)[0]
    assert artwork.geographic_origin == "Japan"
    assert artwork.artist_nationality == "Japanese, active 1800"
    assert artwork.style_or_period == "Edo period"
    assert artwork.region == "east_asia"
