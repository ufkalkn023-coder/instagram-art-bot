from pathlib import Path

import pytest
from PIL import Image

import main
from src import image_processor


def _render(tmp_path, size, color=(38, 78, 118)):
    source_path = tmp_path / "source.jpg"
    output_path = tmp_path / "feed.jpg"
    Image.new("RGB", size, color).save(source_path, "JPEG")
    image_processor.create_feed_post(str(source_path), output_path=str(output_path))
    return Image.open(output_path)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((800, 1200), image_processor.PRESENTATION_PORTRAIT_OR_SQUARE),
        ((1000, 1000), image_processor.PRESENTATION_PORTRAIT_OR_SQUARE),
        ((1600, 1000), image_processor.PRESENTATION_LANDSCAPE),
        ((2400, 800), image_processor.PRESENTATION_PANORAMIC),
        ((1150, 1000), image_processor.PRESENTATION_PORTRAIT_OR_SQUARE),
        ((1151, 1000), image_processor.PRESENTATION_LANDSCAPE),
        ((1800, 1000), image_processor.PRESENTATION_LANDSCAPE),
        ((1801, 1000), image_processor.PRESENTATION_PANORAMIC),
    ],
)
def test_presentation_mode_thresholds_are_deterministic(size, expected):
    assert image_processor.classify_presentation_mode(*size) == expected


@pytest.mark.parametrize(
    ("size", "submode"),
    [
        ((1800, 1000), None),
        ((1801, 1000), image_processor.PANORAMA_WIDE),
        ((2400, 1000), image_processor.PANORAMA_WIDE),
        ((2401, 1000), image_processor.PANORAMA_EXTREME),
    ],
)
def test_panorama_submode_boundaries(size, submode):
    assert image_processor.classify_panorama_submode(*size) == submode


def test_panorama_layout_uses_asymmetric_museum_plate_placement():
    assert image_processor.calculate_feed_artwork_box(2400, 1000) == (0, 378, 1080, 450)
    assert image_processor.calculate_feed_artwork_box(3000, 1000) == (0, 337, 1080, 360)


@pytest.mark.parametrize(
    "size",
    [
        (800, 1200),
        (1000, 1000),
        (1600, 1000),
        (1800, 1000),
        (2000, 1000),
        (2400, 1000),
        (3000, 1000),
        (4000, 1000),
        (6000, 1000),
    ],
)
def test_feed_render_keeps_artwork_aspect_ratio_and_canvas_size(tmp_path, size):
    rendered = _render(tmp_path, size)

    assert rendered.size == (1080, 1350)
    source_ratio = size[0] / size[1]
    expected_width = min(1080, round(1350 * source_ratio))
    expected_height = min(1350, round(1080 / source_ratio))
    assert image_processor.calculate_feed_artwork_box(*size)[2:] == (expected_width, expected_height)
    assert (expected_width / expected_height) == pytest.approx(source_ratio, abs=0.002)


@pytest.mark.parametrize(
    "size",
    [(1600, 1000), (1800, 1000), (2000, 1000), (2400, 1000), (3000, 1000), (4000, 1000), (6000, 1000)],
)
def test_landscape_and_panorama_use_solid_matte_without_blur(tmp_path, monkeypatch, size):
    def forbidden_filter(*args, **kwargs):
        pytest.fail("feed presentation must not create a blurred artwork background")

    monkeypatch.setattr(Image.Image, "filter", forbidden_filter)
    rendered = _render(tmp_path, size)

    assert rendered.size == (1080, 1350)
    assert rendered.getpixel((0, 0)) == pytest.approx(image_processor.MUSEUM_MATTE, abs=2)
    assert rendered.getpixel((1079, 1349)) == pytest.approx(image_processor.MUSEUM_MATTE, abs=2)


def test_light_artwork_gets_only_a_thin_neutral_separation_border(tmp_path):
    rendered = _render(tmp_path, (1600, 1000), color=(250, 250, 248))

    assert rendered.getpixel((0, 337)) == pytest.approx(image_processor.SUBTLE_BORDER, abs=3)
    assert rendered.getpixel((500, 0)) == pytest.approx(image_processor.MUSEUM_MATTE, abs=2)


def test_rendering_is_deterministic_for_the_same_artwork(tmp_path):
    source_path = tmp_path / "source.jpg"
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    Image.new("RGB", (2400, 800), (38, 78, 118)).save(source_path, "JPEG")

    image_processor.create_feed_post(str(source_path), output_path=str(first_path))
    image_processor.create_feed_post(str(source_path), output_path=str(second_path))

    assert first_path.read_bytes() == second_path.read_bytes()


def test_workflow_uses_four_daily_istanbul_feed_windows():
    workflow = Path(".github/workflows/instagram_bot.yml").read_text()

    assert 'cron: "0 6,10,14,18 * * *"' in workflow
    assert "workflow_dispatch" in workflow
    assert main.SCHEDULED_CAROUSEL_UTC_HOURS == {18}
