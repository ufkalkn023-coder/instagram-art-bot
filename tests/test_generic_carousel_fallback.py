from pathlib import Path

import pytest

import main
from src import art_fetcher, carousel_cover
from src.carousel_editorial import (
    derive_carousel_editorial_facts,
    fallback_carousel_intro,
    fallback_editorial_subtitle,
)
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
from src.theme_acquisition import ThemeAcquisitionPolicy


def _candidate(identifier: str, *, rights: bool = True) -> NormalizedArtwork:
    return NormalizedArtwork(
        source="test",
        source_id=identifier,
        title=f"Artwork {identifier}",
        artist_name="Shared Artist",
        creation_date="1880",
        medium="Oil on canvas",
        classification="Painting",
        description="Museum catalog record.",
        museum_name="Shared Museum",
        image_url=f"https://images.example/{identifier}.jpg",
        image_width=1600,
        image_height=1200,
        is_public_domain=rights,
        rights_status="CONFIRMED_PUBLIC_DOMAIN" if rights else None,
    )


class _Adapter:
    source_id = "test"

    def __init__(self, candidates):
        self.candidates = list(candidates)

    def fetch_candidates(self, **kwargs):
        return self.candidates


def _install_pipeline(monkeypatch, tmp_path, candidates, quality, invalid_reasons):
    monkeypatch.setenv("ARTFOLIO_RIGHTS_POLICY", "strict_public_domain")
    monkeypatch.setattr(art_fetcher, "_museum_adapters", lambda: [_Adapter(candidates)])
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))

    def quality_score(artwork, _weights):
        return quality.get(artwork.source_id, 90.0)

    monkeypatch.setattr("src.theme_acquisition.calculate_quality_score", quality_score)
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage", lambda artwork: 1.0
    )
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", quality_score)
    monkeypatch.setattr(art_fetcher, "calculate_measurement_coverage", lambda artwork: 1.0)
    monkeypatch.setattr(carousel_cover, "calculate_quality_score", quality_score)
    monkeypatch.setattr(
        carousel_cover, "calculate_measurement_coverage", lambda artwork: 1.0
    )

    validation_calls = []

    def validate(url, output_path, **kwargs):
        identifier = url.rsplit("/", 1)[-1].removesuffix(".jpg")
        validation_calls.append(identifier)
        if reason := invalid_reasons.get(identifier):
            return ImageValidationResult(False, reason=reason)
        Path(output_path).write_bytes(b"validated")
        return ImageValidationResult(
            True, width=1600, height=1200, image_format="JPEG", reason="ok"
        )

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", validate)
    monkeypatch.setattr(
        carousel_cover, "validate_and_download_image_with_metadata", validate
    )
    return validation_calls


def _select(posted_ids):
    return art_fetcher.fetch_themed_artworks(
        posted_ids,
        "museum art",
        count=8,
        color_tone="cool",
        selection_run_seed=art_fetcher.SelectionRunSeed("fixed", "test"),
        theme_definition=main.ARTFOLIO_SELECTION_THEME,
        return_acquisition=True,
        acquisition_policy=ThemeAcquisitionPolicy(require_theme_relevance=False),
    )


def test_generic_fallback_reuses_hard_gates_and_five_valid_works_are_sufficient(
    monkeypatch, tmp_path
):
    valid = [_candidate(f"valid-{index}") for index in range(6)]
    low_quality = _candidate("low-quality")
    rights_failure = _candidate("rights-failure", rights=False)
    posted = _candidate("posted")
    duplicate = valid[0].model_copy(update={"title": "Duplicate variant"})
    unsafe = _candidate("unsafe")
    too_many_pixels = _candidate("too-many-pixels")
    validation_calls = _install_pipeline(
        monkeypatch,
        tmp_path,
        [
            *valid,
            low_quality,
            rights_failure,
            posted,
            duplicate,
            unsafe,
            too_many_pixels,
        ],
        {"low-quality": 49.9},
        {"unsafe": "unsafe_url", "too-many-pixels": "too_many_pixels"},
    )

    selection = _select({posted.canonical_id})

    assert isinstance(selection, art_fetcher.ThemedArtworkSelection)
    assert len(selection.artworks) == 5
    assert len({artwork["id"] for artwork in selection.artworks}) == 5
    assert all("theme_relevance_score" not in artwork for artwork in selection.artworks)
    assert all(
        candidate.evidence.theme_relevance_score < 50
        for candidate in selection.acquisition.candidates
    )
    assert {artwork["museum"] for artwork in selection.artworks} == {"Shared Museum"}
    assert "low-quality" not in validation_calls
    assert "rights-failure" not in validation_calls
    assert "posted" not in validation_calls
    assert validation_calls.count("valid-0") == 1
    assert "unsafe" in validation_calls
    assert "too-many-pixels" in validation_calls

    cover = carousel_cover.select_editorial_cover(
        posted_ids={posted.canonical_id},
        featured_artworks=selection.artworks,
        theme="museum art",
        color_tone="cool",
        selection_run_seed=art_fetcher.SelectionRunSeed("fixed", "test"),
        theme_definition=main.ARTFOLIO_SELECTION_THEME,
        acquisition=selection.acquisition,
    )

    assert cover.canonical_id not in {artwork["id"] for artwork in selection.artworks}


def test_generic_fallback_with_fewer_than_five_valid_images_fails(monkeypatch, tmp_path):
    candidates = [_candidate(f"valid-{index}") for index in range(4)]
    candidates.extend((_candidate("unsafe"), _candidate("too-many-pixels")))
    _install_pipeline(
        monkeypatch,
        tmp_path,
        candidates,
        {},
        {"unsafe": "unsafe_url", "too-many-pixels": "too_many_pixels"},
    )

    with pytest.raises(art_fetcher.CarouselSelectionError) as error:
        _select(set())

    assert error.value.reason == "image_validation_exhausted"


def test_artfolio_selection_copy_is_neutral_and_claims_no_shared_subject():
    artworks = [
        {
            "id": f"art-{index}",
            "artist": f"Artist {index}",
            "museum": "Shared Museum",
            "date": str(1800 + index),
            "medium": "Painting",
        }
        for index in range(5)
    ]
    facts = derive_carousel_editorial_facts(
        artworks,
        theme_id=main.ARTFOLIO_SELECTION_THEME.id,
        theme_title=main.ARTFOLIO_SELECTION_THEME.title,
        carousel_format=main.ARTFOLIO_SELECTION_THEME.format,
    )

    subtitle = fallback_editorial_subtitle(facts)
    intro = fallback_carousel_intro(facts)

    assert subtitle == "5 works, selected by Artfolio."
    assert intro.startswith("5 works selected by Artfolio.")
    assert "shared theme" not in intro.casefold()
    assert "around artfolio selection" not in (subtitle + intro).casefold()
