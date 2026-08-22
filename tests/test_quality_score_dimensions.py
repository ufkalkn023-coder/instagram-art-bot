import pytest

from src.models import NormalizedArtwork, normalize_image_dimensions
from src.quality_filter import calculate_measurement_coverage, calculate_quality_score


def _artwork(*, width=None, height=None, **overrides):
    values = {
        "source": "aic",
        "source_id": "1",
        "title": "Artwork",
        "artist_name": "Artist",
        "creation_date": "1900",
        "medium": "Oil on canvas",
        "museum_name": "Museum",
        "image_width": width,
        "image_height": height,
    }
    values.update(overrides)
    return NormalizedArtwork(**values)


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3400, 2235, (3400, 2235)),
        ("956", "893", (956, 893)),
        (None, 893, (None, None)),
        (956, None, (None, None)),
        (0, 893, (None, None)),
        (956, -1, (None, None)),
        ("unknown", "893", (None, None)),
        (956.0, 893, (None, None)),
    ],
)
def test_image_dimension_normalization(width, height, expected):
    assert normalize_image_dimensions(width, height) == expected


@pytest.mark.parametrize(
    ("width", "height", "expected_score"),
    [
        (2000, 1600, 100.0),  # high-resolution, ordinary landscape
        (900, 900, 85 / 95 * 100),  # moderate resolution, square
        (600, 600, 70 / 95 * 100),  # low resolution, square
        (2000, 500, 85 / 95 * 100),  # high-resolution extreme panoramic image
        (300, 700, 60 / 95 * 100),  # low-resolution extreme tall image
    ],
)
def test_quality_score_uses_real_resolution_and_aspect_ratio(width, height, expected_score):
    assert calculate_quality_score(_artwork(width=width, height=height), {"aic": 15}) == pytest.approx(expected_score)


def test_unknown_dimensions_are_neutralized_without_inventing_image_points():
    known_low_resolution = _artwork(width=600, height=600)
    unknown_dimensions = _artwork()
    poor_metadata_unknown_dimensions = _artwork(
        title="Untitled",
        artist_name="Unknown Artist",
        creation_date="Unknown Date",
        medium=None,
    )

    assert calculate_quality_score(unknown_dimensions, {"aic": 15}) == 100.0
    assert calculate_quality_score(known_low_resolution, {"aic": 15}) == pytest.approx(70 / 95 * 100)
    assert calculate_quality_score(poor_metadata_unknown_dimensions, {"aic": 15}) == pytest.approx(15 / 35 * 100)
    assert calculate_quality_score(poor_metadata_unknown_dimensions, {"aic": 15}) < 50.0


def test_invalid_model_dimensions_are_treated_as_unavailable_by_scoring():
    invalid_dimensions = _artwork(width=0, height=900)
    unknown_dimensions = _artwork()

    assert calculate_quality_score(invalid_dimensions, {"aic": 15}) == calculate_quality_score(
        unknown_dimensions, {"aic": 15}
    )


@pytest.mark.parametrize(
    ("width", "height", "expected_score"),
    [
        (843, 600, 85 / 95 * 100),
        (1686, 1200, 100.0),
    ],
)
def test_downloaded_dimensions_replace_unknown_technical_evidence(width, height, expected_score):
    artwork = _artwork()

    assert calculate_measurement_coverage(artwork) == 0.4
    assert calculate_quality_score(artwork, {"aic": 15}) == 100.0

    artwork.image_width = width
    artwork.image_height = height

    assert calculate_measurement_coverage(artwork) == 1.0
    assert calculate_quality_score(artwork, {"aic": 15}) == pytest.approx(expected_score)


def test_measured_extreme_ratio_uses_aspect_penalty_after_unknown_pre_score():
    artwork = _artwork()

    assert calculate_quality_score(artwork, {"aic": 15}) == 100.0
    artwork.image_width = 2000
    artwork.image_height = 400

    assert calculate_quality_score(artwork, {"aic": 15}) == pytest.approx(85 / 95 * 100)


def test_ordinary_landscape_is_not_penalized_against_portrait_or_square():
    portrait = _artwork(width=1000, height=1500)
    square = _artwork(width=1200, height=1200)
    landscape = _artwork(width=1500, height=1000)

    assert calculate_quality_score(portrait, {"aic": 15}) == 100.0
    assert calculate_quality_score(square, {"aic": 15}) == 100.0
    assert calculate_quality_score(landscape, {"aic": 15}) == 100.0


def test_medium_does_not_change_quality_score():
    with_medium = _artwork(width=900, height=900, medium="Oil on canvas")
    without_medium = _artwork(width=900, height=900, medium=None)
    synthetic_medium = _artwork(width=900, height=900, medium="painting")

    expected_score = calculate_quality_score(with_medium, {"aic": 15})
    assert calculate_quality_score(without_medium, {"aic": 15}) == expected_score
    assert calculate_quality_score(synthetic_medium, {"aic": 15}) == expected_score


def test_metadata_placeholders_are_exact_and_whitespace_normalized():
    baseline = _artwork(width=2000, height=1600)
    placeholders = _artwork(
        width=2000,
        height=1600,
        title="  unknown  ",
        artist_name=" Unknown Artist ",
        creation_date="  Unknown Date ",
    )
    legitimate_attribution = _artwork(
        width=2000,
        height=1600,
        artist_name="Master of the Unknown Woman",
    )

    assert calculate_quality_score(placeholders, {"aic": 15}) == pytest.approx(75 / 95 * 100)
    assert calculate_quality_score(legitimate_attribution, {"aic": 15}) == calculate_quality_score(
        baseline, {"aic": 15}
    )


@pytest.mark.parametrize("source", ["aic", "met", "cleveland", "rijksmuseum"])
def test_default_museum_source_weights_are_equal(source):
    artwork = _artwork(width=2000, height=1600, source=source)
    default_weights = {"aic": 15, "met": 15, "cleveland": 15, "rijksmuseum": 15}

    assert calculate_quality_score(artwork, default_weights) == 100.0


@pytest.mark.parametrize(
    ("configured_weight", "effective_weight"),
    [(0, 0), (5, 5), (15, 15), (20, 15), (30, 15)],
)
def test_source_weight_uses_a_fifteen_point_upper_bound(configured_weight, effective_weight):
    artwork = _artwork(width=2000, height=1600)

    assert calculate_quality_score(artwork, {"aic": configured_weight}) == pytest.approx(
        (80 + effective_weight) / 95 * 100
    )


def test_missing_source_weight_uses_the_default_fifteen_point_contribution():
    artwork = _artwork(width=2000, height=1600, source="met")

    assert calculate_quality_score(artwork, {"aic": 0}) == 100.0


def test_quality_score_remains_float_for_final_selection_scores():
    artwork = _artwork(width=900, height=900)
    artwork.quality_score = calculate_quality_score(artwork, {"aic": 15}) + 1.5

    assert artwork.quality_score == calculate_quality_score(artwork, {"aic": 15}) + 1.5
    assert isinstance(artwork.quality_score, float)
