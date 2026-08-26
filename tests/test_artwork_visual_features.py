from pathlib import Path

import pytest
from PIL import Image

from src.artwork_visual_features import (
    MAX_FEATURE_DIMENSION,
    ArtworkOrientation,
    DominantColorFamily,
    LuminanceBucket,
    classify_orientation,
    extract_visual_features,
)


@pytest.mark.parametrize(
    ("dimensions", "expected"),
    [
        ((800, 1200), ArtworkOrientation.PORTRAIT),
        ((1000, 980), ArtworkOrientation.SQUAREISH),
        ((1400, 800), ArtworkOrientation.LANDSCAPE),
    ],
)
def test_orientation_classification(dimensions, expected):
    assert classify_orientation(*dimensions) is expected


def _image(path: Path, color, size=(80, 60)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def test_luminance_saturation_and_coarse_dominant_color(tmp_path):
    black = extract_visual_features(_image(tmp_path / "black.png", (0, 0, 0)))
    gray = extract_visual_features(_image(tmp_path / "gray.png", (128, 128, 128)))
    red = extract_visual_features(_image(tmp_path / "red.png", (255, 0, 0)))

    assert black.mean_luminance == 0.0
    assert black.luminance_bucket is LuminanceBucket.DARK
    assert gray.mean_saturation == 0.0
    assert gray.dominant_color_family is DominantColorFamily.NEUTRAL
    assert red.mean_saturation == 1.0
    assert red.dominant_color_family is DominantColorFamily.RED


def test_extraction_is_deterministic_bounded_and_does_not_mutate_source(tmp_path):
    path = _image(tmp_path / "large.png", (10, 160, 220), size=(4000, 2500))
    before = path.read_bytes()

    first = extract_visual_features(path)
    second = extract_visual_features(path)

    assert first == second
    assert max(first.sample_width, first.sample_height) <= MAX_FEATURE_DIMENSION
    assert (first.width, first.height) == (4000, 2500)
    assert first.aspect_ratio == 1.6
    assert path.read_bytes() == before
    with Image.open(path) as unchanged:
        assert unchanged.size == (4000, 2500)
