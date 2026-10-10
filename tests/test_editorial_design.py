import pytest
from PIL import Image

from src.editorial_design import CoverStyle, render_editorial_cover


@pytest.fixture
def artwork(tmp_path):
    path = tmp_path / "source.png"
    image = Image.new("RGB", (200, 100), "#cc3322")
    image.paste("#2244cc", (100, 0, 200, 100))
    image.save(path)
    return path


def render(artwork, tmp_path, style, focus=None):
    output = tmp_path / f"{style.value}.jpg"
    result = render_editorial_cover(
        str(artwork),
        "A Quiet Study of Color",
        "A short deck",
        style,
        str(output),
        focus,
    )
    return result, output


def test_artwork_first_retains_full_source_in_gallery_field(artwork, tmp_path):
    result, output = render(artwork, tmp_path, CoverStyle.ARTWORK_FIRST)
    assert result["crop_basis"] == "full_artwork"
    with Image.open(output) as rendered:
        assert rendered.size == (1080, 1350)
        # The complete source aspect is visible as a 1080x540 rectangle.
        assert (
            max(
                abs(a - b) for a, b in zip(rendered.getpixel((120, 400)), (204, 51, 34))
            )
            <= 3
        )
        assert (
            max(
                abs(a - b) for a, b in zip(rendered.getpixel((960, 400)), (34, 68, 204))
            )
            <= 3
        )
        assert rendered.getpixel((540, 250)) == rendered.getpixel((540, 300))


def test_detail_focus_edges_and_missing_focus_are_explicit(artwork, tmp_path):
    result, _ = render(
        artwork, tmp_path, CoverStyle.DETAIL_STUDY, (0.0, 0.0, 0.85, 1.0)
    )
    assert result["crop_basis"] == "model_focus"
    missing, output = render(artwork, tmp_path, CoverStyle.DETAIL_STUDY)
    assert missing["crop_basis"] == "missing_focus_full_artwork"
    assert missing["requested_style"] == CoverStyle.DETAIL_STUDY.value
    assert missing["actual_style"] == CoverStyle.ARTWORK_FIRST.value
    with Image.open(output) as rendered:
        assert rendered.size == (1080, 1350)
        # The fallback label sits between the image frame and caption block.
        assert rendered.crop((80, 930, 340, 970)).getbbox() is not None


def test_moderately_long_museum_title_fits_caption_area(artwork, tmp_path):
    title = (
        "A Quiet Study of Color, Memory, and the Changing Light Across a Gallery Wall"
    )
    output = tmp_path / "long-title.jpg"
    result = render_editorial_cover(
        str(artwork),
        title,
        "A short factual deck",
        CoverStyle.ARTWORK_FIRST,
        str(output),
    )
    assert result["actual_style"] == CoverStyle.ARTWORK_FIRST.value
    with Image.open(output) as rendered:
        assert rendered.size == (1080, 1350)


@pytest.mark.parametrize(
    "focus",
    [
        (-0.1, 0, 0.5, 0.5),
        (0, 0, float("nan"), 1),
        (0, 0, 0.2, 0.2),
        (0, 0, 0.95, 0.95),
    ],
)
def test_rejects_out_of_bounds_or_invalid_focus(artwork, tmp_path, focus):
    with pytest.raises(ValueError):
        render(artwork, tmp_path, CoverStyle.DETAIL_STUDY, focus)


def test_complete_long_mixed_case_title_fits_and_styles_are_distinct(artwork, tmp_path):
    outputs = [render(artwork, tmp_path, style)[1] for style in CoverStyle]
    assert len({Image.open(path).tobytes() for path in outputs}) == 3
    assert all(Image.open(path).format == "JPEG" for path in outputs)
    with pytest.raises(ValueError, match="capacity"):
        render_editorial_cover(
            str(artwork),
            "word " * 2000,
            "deck",
            CoverStyle.ARTWORK_FIRST,
            str(tmp_path / "bad.jpg"),
        )
