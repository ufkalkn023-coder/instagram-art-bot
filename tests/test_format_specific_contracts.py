from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src import carousel_cover
from src.art_fetcher import SelectionRunSeed
from src.artwork_metadata import (
    ArtworkDateCertainty,
    normalize_artist_identity,
    normalize_museum_identity,
    parse_artwork_date,
)
from src.carousel_set_optimizer import (
    build_selection_features,
    optimize_carousel_set,
    pairwise_redundancy,
    score_carousel_set,
)
from src.carousel_themes import (
    FORMAT_POLICIES,
    CarouselFormat,
    CarouselThemeDefinition,
    ThemeFamily,
    ThemeRegistryError,
    parse_theme_registry,
)
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
from src.theme_acquisition import ThemeAcquisitionPolicy, acquire_theme_candidates


def _theme(carousel_format: CarouselFormat, **target) -> CarouselThemeDefinition:
    return CarouselThemeDefinition(
        id=f"contract_{carousel_format.value.casefold()}",
        title="Contract Theme",
        family=ThemeFamily.SUBJECT,
        format=carousel_format,
        description="A focused format contract used for deterministic tests.",
        primary_queries=["shared subject"],
        required_terms=[],
        minimum_candidate_target=9,
        format_target=target or None,
    )


def _normalized(
    index: int,
    *,
    artist: str = "Vincent van Gogh",
    museum: str = "The Metropolitan Museum of Art",
    source: str = "met",
    description: str = "A documented shared subject.",
) -> NormalizedArtwork:
    return NormalizedArtwork(
        source=source,
        source_id=str(index),
        title=f"Shared Work {index}",
        artist_name=artist,
        creation_date=str(1600 + index * 35),
        medium="Watercolor" if index % 2 else "Oil on canvas",
        classification="Painting",
        description=description,
        museum_name=museum,
        image_url=f"https://images.example/{index}.jpg",
        image_width=1600,
        image_height=1200,
        region="europe",
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )


def _mapping(
    index: int,
    *,
    artist: str = "Vincent van Gogh",
    museum: str = "The Metropolitan Museum of Art",
    date: str | None = None,
    relevance: float = 90.0,
    quality: float = 90.0,
    medium: str = "Oil on canvas",
    region: str = "europe",
) -> dict[str, object]:
    return {
        "id": f"art_{index}",
        "title": f"Shared Work {index}",
        "artist": artist,
        "museum": museum,
        "date": date or str(1500 + index * 50),
        "medium": medium,
        "classification": "Painting",
        "description": "A documented shared subject.",
        "region": region,
        "theme_relevance_score": relevance,
        "quality_score": quality,
        "selection_score": relevance * 0.68 + quality * 0.30,
        "image_width": 1200 if index % 2 else 800,
        "image_height": 800 if index % 2 else 1200,
    }


class _Adapter:
    def __init__(self, source_id: str, candidates: list[NormalizedArtwork]):
        self.source_id = source_id
        self.candidates = candidates
        self.calls: list[str] = []

    def fetch_candidates(self, *, query, limit, rng):
        self.calls.append(query)
        return self.candidates[:limit]


def _acquire(monkeypatch, theme, adapters):
    monkeypatch.setattr("src.theme_acquisition.calculate_quality_score", lambda *args: 90.0)
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage", lambda *args: 1.0
    )
    return acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=adapters,
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(
            max_primary_queries=1,
            max_secondary_queries=0,
            minimum_safe_pool=9,
        ),
    )


def test_every_format_has_one_policy_and_constrained_targets_are_required():
    assert set(FORMAT_POLICIES) == set(CarouselFormat)

    with pytest.raises(ValidationError, match="artist_name"):
        _theme(CarouselFormat.MONOGRAPHIC)
    with pytest.raises(ValidationError, match="museum_name"):
        _theme(CarouselFormat.MUSEUM_SPOTLIGHT)

    malformed = _theme(CarouselFormat.THEMATIC_COLLECTION).model_dump(mode="json")
    malformed.update(format="MONOGRAPHIC", format_target={"museum_name": "The Met"})
    with pytest.raises(ThemeRegistryError, match="artist_name"):
        parse_theme_registry({"themes": [malformed]})


def test_identity_normalization_is_exact_deterministic_and_alias_controlled():
    assert normalize_artist_identity("Van Gogh, Vincent") == normalize_artist_identity(
        "Vincent van Gogh"
    )
    assert normalize_artist_identity("Vincent W. van Gogh") != normalize_artist_identity(
        "Vincent van Gogh"
    )
    assert normalize_museum_identity("The Metropolitan Museum of Art") != (
        normalize_museum_identity("The Met")
    )


def test_monographic_acquisition_rejects_unrelated_and_description_only_artists(monkeypatch):
    theme = _theme(
        CarouselFormat.MONOGRAPHIC,
        artist_name="Vincent van Gogh",
        aliases=["Van Gogh, Vincent", "Van Gogh"],
    )
    matching = [_normalized(index) for index in range(9)]
    impostor = _normalized(
        99,
        artist="Other Artist",
        description="An essay mentioning Vincent van Gogh and this shared subject.",
    )
    result = _acquire(monkeypatch, theme, [_Adapter("met", [*matching, impostor])])

    assert result.availability.sufficient
    assert result.availability.format_target_matches == 9
    assert {candidate.artwork.artist_name for candidate in result.candidates} == {
        "Vincent van Gogh"
    }
    rejected = next(
        candidate for candidate in result.all_candidates if candidate.artwork.source_id == "99"
    )
    assert rejected.evidence.format_rejection_reason == "target_artist_mismatch"


def test_monographic_optimizer_exempts_artist_and_impossible_museum_caps():
    theme = _theme(
        CarouselFormat.MONOGRAPHIC,
        artist_name="Vincent van Gogh",
        aliases=["Van Gogh, Vincent"],
    )
    works = [_mapping(index, museum="Single Museum") for index in range(8)]
    works.append(_mapping(9, artist="Unrelated Artist", quality=100.0, relevance=100.0))

    result = optimize_carousel_set(works, theme=theme)
    first, second = (build_selection_features(work, theme) for work in works[:2])

    assert len(result.artworks) == 8
    assert {work["artist"] for work in result.artworks} == {"Vincent van Gogh"}
    assert {work["museum"] for work in result.artworks} == {"Single Museum"}
    assert pairwise_redundancy(first, second, FORMAT_POLICIES[theme.format]).same_artist == 0


def test_monographic_cover_is_a_ninth_distinct_target_work(monkeypatch, tmp_path):
    theme = _theme(CarouselFormat.MONOGRAPHIC, artist_name="Vincent van Gogh")
    acquisition = _acquire(
        monkeypatch,
        theme,
        [_Adapter("met", [_normalized(index) for index in range(9)])],
    )
    featured = [
        {"id": candidate.artwork.canonical_id}
        for candidate in acquisition.candidates[:8]
    ]
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(carousel_cover, "calculate_quality_score", lambda *args: 90.0)
    monkeypatch.setattr(
        carousel_cover,
        "calculate_measurement_coverage",
        lambda *args: 1.0,
    )

    def download(_url, output_path):
        Path(output_path).write_bytes(b"validated")
        return ImageValidationResult(
            True, width=1600, height=1200, image_format="JPEG", reason="ok"
        )

    monkeypatch.setattr(carousel_cover, "validate_and_download_image_with_metadata", download)
    cover = carousel_cover.select_editorial_cover(
        posted_ids=set(),
        featured_artworks=featured,
        theme=theme.title,
        color_tone="warm",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
        theme_definition=theme,
        acquisition=acquisition,
    )

    assert cover.canonical_id not in {work["id"] for work in featured}
    cover_candidate = next(
        candidate
        for candidate in acquisition.candidates
        if candidate.artwork.canonical_id == cover.canonical_id
    )
    assert cover_candidate.artwork.artist_name == "Vincent van Gogh"


def test_constrained_format_accepts_minimum_product_but_retains_headroom_target(monkeypatch):
    theme = _theme(CarouselFormat.MONOGRAPHIC, artist_name="Vincent van Gogh")
    acquisition = _acquire(
        monkeypatch,
        theme,
        [_Adapter("met", [_normalized(index) for index in range(8)])],
    )

    assert acquisition.availability.sufficient
    assert acquisition.availability.format_target_matches == 8
    assert acquisition.availability.absolute_minimum == 6
    assert acquisition.availability.narrow_pool


def test_museum_spotlight_narrows_adapter_and_rejects_description_only_match(monkeypatch):
    theme = _theme(
        CarouselFormat.MUSEUM_SPOTLIGHT,
        museum_name="The Metropolitan Museum of Art",
        aliases=["The Met"],
        source_ids=["met"],
    )
    matching = [_normalized(index) for index in range(9)]
    wrong = _normalized(
        99,
        museum="Other Museum",
        description="Transferred after display at The Metropolitan Museum of Art.",
    )
    met = _Adapter("met", [*matching, wrong])
    other = _Adapter("aic", [_normalized(200, source="aic")])
    result = _acquire(monkeypatch, theme, [met, other])

    assert result.availability.sufficient
    assert met.calls == ["The Metropolitan Museum of Art"]
    assert other.calls == []
    assert all(
        candidate.artwork.museum_name == "The Metropolitan Museum of Art"
        for candidate in result.candidates
    )
    rejected = next(
        candidate for candidate in result.all_candidates if candidate.artwork.source_id == "99"
    )
    assert rejected.evidence.format_rejection_reason == "target_museum_mismatch"


def test_museum_spotlight_exempts_only_target_museum_redundancy():
    theme = _theme(
        CarouselFormat.MUSEUM_SPOTLIGHT,
        museum_name="The Metropolitan Museum of Art",
        aliases=["The Met"],
    )
    works = [_mapping(index, artist=f"Artist {index}") for index in range(8)]
    works.append(_mapping(9, museum="Other Museum", quality=100.0, relevance=100.0))
    result = optimize_carousel_set(works, theme=theme)
    first, second = (build_selection_features(work, theme) for work in works[:2])

    assert len(result.artworks) == 8
    assert {work["museum"] for work in result.artworks} == {
        "The Metropolitan Museum of Art"
    }
    assert pairwise_redundancy(first, second, FORMAT_POLICIES[theme.format]).same_museum == 0


def test_museum_spotlight_cover_is_a_ninth_distinct_target_work(monkeypatch, tmp_path):
    theme = _theme(
        CarouselFormat.MUSEUM_SPOTLIGHT,
        museum_name="The Metropolitan Museum of Art",
        source_ids=["met"],
    )
    acquisition = _acquire(
        monkeypatch,
        theme,
        [_Adapter("met", [_normalized(index) for index in range(9)])],
    )
    featured = [
        {"id": candidate.artwork.canonical_id}
        for candidate in acquisition.candidates[:8]
    ]
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(carousel_cover, "calculate_quality_score", lambda *args: 90.0)
    monkeypatch.setattr(
        carousel_cover,
        "calculate_measurement_coverage",
        lambda *args: 1.0,
    )

    def download(_url, output_path):
        Path(output_path).write_bytes(b"validated")
        return ImageValidationResult(
            True, width=1600, height=1200, image_format="JPEG", reason="ok"
        )

    monkeypatch.setattr(carousel_cover, "validate_and_download_image_with_metadata", download)
    cover = carousel_cover.select_editorial_cover(
        posted_ids=set(),
        featured_artworks=featured,
        theme=theme.title,
        color_tone="warm",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
        theme_definition=theme,
        acquisition=acquisition,
    )

    assert cover.canonical_id not in {work["id"] for work in featured}
    cover_candidate = next(
        candidate
        for candidate in acquisition.candidates
        if candidate.artwork.canonical_id == cover.canonical_id
    )
    assert cover_candidate.artwork.museum_name == "The Metropolitan Museum of Art"


@pytest.mark.parametrize(
    "raw,representative,certainty",
    [
        ("1888", 1888, ArtworkDateCertainty.EXACT),
        ("c. 1888", 1888, ArtworkDateCertainty.APPROXIMATE),
        ("1888–1890", 1889, ArtworkDateCertainty.RANGE),
        ("ca. 1750", 1750, ArtworkDateCertainty.APPROXIMATE),
        ("17th century", 1649, ArtworkDateCertainty.CENTURY),
    ],
)
def test_coarse_date_parser_preserves_uncertainty(raw, representative, certainty):
    parsed = parse_artwork_date(raw)
    assert parsed is not None
    assert parsed.representative_year == representative
    assert parsed.certainty is certainty
    assert parse_artwork_date("date unknown") is None


def test_chronological_optimizer_prefers_broad_equivalent_coverage():
    theme = _theme(
        CarouselFormat.CHRONOLOGICAL,
        chronological_dimension="creation_date",
    )
    clustered = [
        _mapping(
            index,
            artist=f"Artist {index}",
            date=str(1880 + index * 2),
            museum=f"Museum {index % 4}",
            region=("europe", "east_asia", "north_america", "oceania")[index % 4],
        )
        for index in range(8)
    ]
    broad = [
        _mapping(
            index + 20,
            artist=f"Artist {index + 20}",
            date=str(1300 + index * 100),
            museum=f"Museum {index % 4}",
            region=("europe", "east_asia", "north_america", "oceania")[index % 4],
            quality=89.5,
            relevance=89.5,
        )
        for index in range(8)
    ]
    result = optimize_carousel_set([*clustered, *broad], theme=theme)
    years = sorted(int(str(work["date"])) for work in result.artworks)

    assert years[-1] - years[0] >= 500


def test_comparison_dimension_adds_bounded_preference_without_bypassing_floors():
    balanced = _theme(CarouselFormat.COMPARATIVE)
    period_first = _theme(
        CarouselFormat.COMPARATIVE,
        comparison_dimension="period",
    )
    works = [
        _mapping(
            index,
            artist=f"Artist {index}",
            date=str(1400 + index * 100),
            museum=f"Museum {index % 4}",
            region=("europe", "east_asia", "north_america", "oceania")[index % 4],
        )
        for index in range(8)
    ]
    balanced_features = [build_selection_features(work, balanced) for work in works]
    preferred_features = [build_selection_features(work, period_first) for work in works]

    assert score_carousel_set(preferred_features, period_first).format_adjustments > (
        score_carousel_set(balanced_features, balanced).format_adjustments
    )
    works.append(
        _mapping(
            99,
            artist="Artist 99",
            relevance=59.9,
            quality=100.0,
            date="1100",
            region="latin_america_caribbean",
        )
    )
    selected = optimize_carousel_set(works, theme=period_first)
    assert "art_99" not in {work["id"] for work in selected.artworks}

    contrast_pool = [
        _mapping(
            index + 200,
            artist=f"Contrast Artist {index}",
            date=date,
            museum=f"Museum {index % 4}",
            region=("europe", "east_asia", "north_america", "oceania")[index % 4],
        )
        for index, date in enumerate(
            ("1500", "1600", "1700", "1800", "1850", "1900", "1950")
        )
    ]
    contrast_pool.extend(
        (
            _mapping(
                300,
                artist="High Duplicate Period",
                date="1920",
                museum="Museum 3",
                region="oceania",
                quality=90.0,
                relevance=90.0,
            ),
            _mapping(
                301,
                artist="Lower New Period",
                date="1400",
                museum="Museum 3",
                region="oceania",
                quality=89.5,
                relevance=89.5,
            ),
        )
    )
    contrasted = optimize_carousel_set(contrast_pool, theme=period_first)
    contrasted_ids = {work["id"] for work in contrasted.artworks}
    assert "art_301" in contrasted_ids
    assert "art_300" not in contrasted_ids


def test_remaining_intentional_similarity_exemptions_are_format_scoped():
    shared = _mapping(1)
    other = dict(_mapping(2))
    other.update(
        title=shared["title"],
        description=shared["description"],
        date=shared["date"],
        region=shared["region"],
        medium=shared["medium"],
        image_width=shared["image_width"],
        image_height=shared["image_height"],
    )

    light = _theme(CarouselFormat.LIGHT_STUDY)
    iconographic = _theme(CarouselFormat.ICONOGRAPHIC)
    pattern = _theme(CarouselFormat.VISUAL_PATTERN)
    light_pair = [build_selection_features(work, light) for work in (shared, other)]
    icon_pair = [build_selection_features(work, iconographic) for work in (shared, other)]
    pattern_pair = [build_selection_features(work, pattern) for work in (shared, other)]

    assert pairwise_redundancy(*light_pair, FORMAT_POLICIES[light.format]).similar_luminance == 0
    assert pairwise_redundancy(*icon_pair, FORMAT_POLICIES[iconographic.format]).semantic_overlap == 0
    pattern_penalty = pairwise_redundancy(*pattern_pair, FORMAT_POLICIES[pattern.format])
    assert pattern_penalty.same_orientation == 0
    assert pattern_penalty.similar_luminance == 0
    assert pattern_penalty.same_color == 0


def test_typed_region_and_medium_targets_are_hard_only_when_declared():
    regional = _theme(CarouselFormat.REGIONAL, region="east_asia")
    medium = _theme(CarouselFormat.MEDIUM_FOCUS, medium_family="watercolor")
    wrong_region = [_mapping(index, region="europe") for index in range(9)]
    wrong_medium = [_mapping(index, medium="Oil on canvas") for index in range(9)]

    with pytest.raises(ValueError, match="got 0"):
        optimize_carousel_set(wrong_region, theme=regional)
    with pytest.raises(ValueError, match="got 0"):
        optimize_carousel_set(wrong_medium, theme=medium)

    legacy_regional = CarouselThemeDefinition(
        id="legacy_regional",
        title="Legacy Regional",
        family=ThemeFamily.REGIONAL,
        format=CarouselFormat.REGIONAL,
        description="A backward-compatible general regional definition.",
        primary_queries=["regional art"],
    )
    assert legacy_regional.format_target is None
