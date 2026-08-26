import pytest

from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    ContrastBucket,
    DominantColorFamily,
    LuminanceBucket,
)
from src.carousel_caption import format_carousel_caption
from src.carousel_sequence import sequence_carousel_artworks
from src.carousel_set_optimizer import (
    FORMAT_POLICIES,
    build_selection_features,
    pairwise_redundancy,
)
from src.carousel_themes import CarouselFormat, CarouselThemeDefinition, ThemeFamily


def _theme(carousel_format=CarouselFormat.THEMATIC_COLLECTION):
    return CarouselThemeDefinition(
        id=f"sequence_{carousel_format.value.casefold()}",
        title="Sequence Theme",
        family=ThemeFamily.SUBJECT,
        format=carousel_format,
        description="A sufficiently detailed sequencing test theme.",
        primary_queries=("shared subject",),
        required_terms=("shared",),
    )


def _artwork(index, *, score, relevance=None, date=None, luminance=None, region=None):
    luminance = luminance or (LuminanceBucket.DARK if index < 4 else LuminanceBucket.LIGHT)
    mean = {LuminanceBucket.DARK: 40.0, LuminanceBucket.MID: 125.0, LuminanceBucket.LIGHT: 215.0}[luminance]
    visual = ArtworkVisualFeatures(
        width=800 if index % 2 else 1200,
        height=1200 if index % 2 else 800,
        aspect_ratio=2 / 3 if index % 2 else 1.5,
        orientation=ArtworkOrientation.PORTRAIT if index % 2 else ArtworkOrientation.LANDSCAPE,
        mean_luminance=mean,
        luminance_bucket=luminance,
        mean_saturation=0.5,
        dominant_color_family=DominantColorFamily.BLUE if index < 4 else DominantColorFamily.ORANGE,
        contrast_bucket=ContrastBucket.MEDIUM,
    )
    return {
        "id": f"art_{index}",
        "title": f"Shared Work {index}",
        "artist": f"Artist {index}",
        "museum": f"Museum {index % 3}",
        "region": region or ("europe" if index < 4 else "east_asia"),
        "date": date or str(1800 + index * 20),
        "medium": "Oil" if index < 4 else "Watercolor",
        "classification": "Painting",
        "theme_relevance_score": relevance if relevance is not None else score,
        "quality_score": score,
        "selection_score": score,
        "visual_features": visual,
    }


def _adjacent_penalty(artworks, theme):
    features = [build_selection_features(artwork, theme) for artwork in artworks]
    return sum(
        pairwise_redundancy(features[index - 1], features[index], FORMAT_POLICIES[theme.format]).total
        for index in range(1, len(features))
    )


def test_sequence_is_deterministic_not_raw_score_order_and_distributes_strength():
    theme = _theme()
    artworks = [_artwork(index, score=100 - index * 4) for index in range(8)]

    first = sequence_carousel_artworks(artworks, theme=theme)
    second = sequence_carousel_artworks(list(reversed(artworks)), theme=theme)
    order = [artwork["id"] for artwork in first.ordered_artworks]

    assert order == [artwork["id"] for artwork in second.ordered_artworks]
    assert order != [artwork["id"] for artwork in artworks]
    assert first.ordered_artworks[0]["theme_relevance_score"] == 100
    assert first.ordered_artworks[-1]["quality_score"] > min(
        artwork["quality_score"] for artwork in artworks
    )
    assert max(artwork["quality_score"] for artwork in first.ordered_artworks[3:]) >= 96


def test_sequence_reduces_adjacent_visual_redundancy_and_caption_uses_final_order():
    theme = _theme()
    artworks = [_artwork(index, score=90 - index) for index in range(8)]
    result = sequence_carousel_artworks(artworks, theme=theme)

    assert _adjacent_penalty(result.ordered_artworks, theme) < _adjacent_penalty(artworks, theme)
    caption = format_carousel_caption(
        theme_title=theme.title,
        editorial_intro="A grounded introduction.",
        hashtags="#Art",
        featured_artworks=result.ordered_artworks,
    )
    for position, artwork in enumerate(result.ordered_artworks, start=1):
        assert f"{position}. {artwork['title']}" in caption


def test_chronological_sequence_orders_known_dates_then_unknowns_stably():
    theme = _theme(CarouselFormat.CHRONOLOGICAL)
    dates = ["c. 1900", "Unknown Date", "1750", "1880-1885", "Unknown", "1600", "1950", "1800"]
    artworks = [_artwork(index, score=90 - index, date=date) for index, date in enumerate(dates)]

    result = sequence_carousel_artworks(artworks, theme=theme)

    assert [artwork["date"] for artwork in result.ordered_artworks] == [
        "1600",
        "1750",
        "1800",
        "1880-1885",
        "c. 1900",
        "1950",
        "Unknown Date",
        "Unknown",
    ]


def test_comparative_sequence_increases_adjacent_metadata_contrast():
    theme = _theme(CarouselFormat.COMPARATIVE)
    artworks = [
        _artwork(index, score=90 - index, date=str(1800 if index < 4 else 1950))
        for index in range(8)
    ]
    result = sequence_carousel_artworks(artworks, theme=theme)

    raw_period_changes = sum(
        artworks[index - 1]["date"] != artworks[index]["date"] for index in range(1, 8)
    )
    final_period_changes = sum(
        result.ordered_artworks[index - 1]["date"] != result.ordered_artworks[index]["date"]
        for index in range(1, 8)
    )
    assert final_period_changes > raw_period_changes


@pytest.mark.parametrize("featured_count", [3, 5, 8])
def test_sequence_preserves_exact_adaptive_cardinality(featured_count):
    theme = _theme(CarouselFormat.THEMATIC_COLLECTION)
    artworks = [
        _artwork(index, score=100 - index) for index in range(featured_count)
    ]

    result = sequence_carousel_artworks(artworks, theme=theme)

    assert len(result.ordered_artworks) == featured_count
    assert {artwork["id"] for artwork in result.ordered_artworks} == {
        artwork["id"] for artwork in artworks
    }
