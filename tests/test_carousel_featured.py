import inspect
from types import SimpleNamespace

import pytest
from PIL import Image, ImageCms

from src.carousel_featured import (
    CAROUSEL_CANVAS_HEIGHT,
    CAROUSEL_CANVAS_WIDTH,
    CarouselFeaturedRenderMode,
    GalleryFieldFamily,
    calculate_contain_geometry,
    derive_carousel_featured_presentation,
    render_carousel_featured_artwork,
)


def _features(luminance: float):
    return SimpleNamespace(mean_luminance=luminance)


def _artwork(path, luminance: float = 120.0):
    return {
        "id": str(path),
        "local_image_path": str(path),
        "visual_features": _features(luminance),
    }


@pytest.mark.parametrize(
    ("source_size", "expected_size", "expected_position"),
    [
        ((1600, 900), (1080, 608), (0, 371)),
        ((900, 1600), (759, 1350), (160, 0)),
        ((1200, 1200), (1080, 1080), (0, 135)),
        ((801, 1000), (1080, 1348), (0, 1)),
        ((5000, 200), (1080, 43), (0, 653)),
    ],
)
def test_contain_geometry_preserves_complete_artwork_and_maximizes_scale(
    source_size, expected_size, expected_position
):
    geometry = calculate_contain_geometry(*source_size)

    assert (
        geometry.rendered_artwork_width,
        geometry.rendered_artwork_height,
    ) == expected_size
    assert (
        geometry.rendered_artwork_x,
        geometry.rendered_artwork_y,
    ) == expected_position
    assert geometry.rendered_artwork_width <= CAROUSEL_CANVAS_WIDTH
    assert geometry.rendered_artwork_height <= CAROUSEL_CANVAS_HEIGHT
    assert geometry.rendered_artwork_aspect_ratio == pytest.approx(
        geometry.source_aspect_ratio,
        # Integer output pixels impose at most half-pixel rounding per axis;
        # this relative tolerance covers that bound for the 43 px extreme-wide case.
        rel=0.006,
    )
    assert (
        geometry.rendered_artwork_width == CAROUSEL_CANVAS_WIDTH
        or geometry.rendered_artwork_height == CAROUSEL_CANVAS_HEIGHT
    )


def test_gallery_field_policy_uses_grid_warmth_and_set_median_luminance():
    light_warm = derive_carousel_featured_presentation(
        [_artwork("one", 180), _artwork("two", 210), _artwork("three", 190)],
        cover_visual_features=_features(200),
        grid_color_tone="warm",
    )
    mid_neutral = derive_carousel_featured_presentation(
        [_artwork("one", 60), _artwork("two", 120), _artwork("three", 160)],
        cover_visual_features=_features(130),
        grid_color_tone="cool",
    )

    assert light_warm.mode is CarouselFeaturedRenderMode.CAROUSEL_GALLERY_FIELD
    assert light_warm.field_family is GalleryFieldFamily.WARM_DARK
    assert light_warm.evidence_median_luminance == 195.0
    assert mid_neutral.field_family is GalleryFieldFamily.NEUTRAL_LIGHT
    assert mid_neutral.evidence_median_luminance == 125.0


def test_pixel_fidelity_keeps_all_source_edges_and_flat_field(tmp_path):
    source_path = tmp_path / "source.png"
    output_path = tmp_path / "featured.jpg"
    source = Image.new("RGB", (1600, 900), (40, 80, 120))
    # Thick, distinct edge/corner evidence survives deterministic Lanczos/JPEG.
    for x in range(1600):
        for y in range(24):
            source.putpixel((x, y), (220, 20, 20))
            source.putpixel((x, 899 - y), (20, 40, 220))
    for y in range(900):
        for x in range(24):
            source.putpixel((x, y), (20, 200, 40))
            source.putpixel((1599 - x, y), (220, 190, 20))
    source.save(source_path)
    presentation = derive_carousel_featured_presentation(
        [_artwork(source_path)], grid_color_tone="neutral"
    )

    result = render_carousel_featured_artwork(
        source_path,
        presentation=presentation,
        output_path=output_path,
    )

    assert result.geometry.rendered_artwork_y > 0
    with Image.open(output_path) as rendered:
        assert rendered.format == "JPEG"
        assert rendered.size == (1080, 1350)
        field_samples = [
            rendered.getpixel((40, 40)),
            rendered.getpixel((540, 100)),
            rendered.getpixel((1030, 1300)),
        ]
        for pixel in field_samples:
            assert pixel == pytest.approx(presentation.field_value, abs=3)
        y_mid = result.geometry.rendered_artwork_y + result.geometry.rendered_artwork_height // 2
        assert rendered.getpixel((5, y_mid)) == pytest.approx((20, 200, 40), abs=20)
        assert rendered.getpixel((1074, y_mid)) == pytest.approx((220, 190, 20), abs=20)
        assert rendered.getpixel((540, result.geometry.rendered_artwork_y + 5)) == pytest.approx(
            (220, 20, 20), abs=20
        )
        assert rendered.getpixel(
            (540, result.geometry.rendered_artwork_y + result.geometry.rendered_artwork_height - 6)
        ) == pytest.approx((20, 40, 220), abs=20)


def test_renderer_composites_the_artwork_exactly_once(monkeypatch, tmp_path):
    source_path = tmp_path / "source.png"
    Image.new("RGB", (300, 200), "navy").save(source_path)
    presentation = derive_carousel_featured_presentation(
        [_artwork(source_path)], grid_color_tone="neutral"
    )
    calls = []
    original_paste = Image.Image.paste

    def tracked_paste(image, pasted, box=None, mask=None):
        calls.append((pasted.size, box, mask))
        return original_paste(image, pasted, box, mask)

    monkeypatch.setattr(Image.Image, "paste", tracked_paste)
    render_carousel_featured_artwork(
        source_path,
        presentation=presentation,
        output_path=tmp_path / "featured.jpg",
    )

    assert calls == [((1080, 720), (0, 315), None)]


def test_featured_renderer_has_no_metadata_or_typography_inputs():
    assert set(inspect.signature(render_carousel_featured_artwork).parameters) == {
        "source_path",
        "presentation",
        "output_path",
    }


def test_exif_orientation_is_applied_once_before_geometry(tmp_path):
    source_path = tmp_path / "rotated.jpg"
    source = Image.new("RGB", (1600, 900), "teal")
    exif = Image.Exif()
    exif[274] = 6
    source.save(source_path, "JPEG", exif=exif)
    presentation = derive_carousel_featured_presentation(
        [_artwork(source_path)], grid_color_tone="neutral"
    )

    result = render_carousel_featured_artwork(
        source_path,
        presentation=presentation,
        output_path=tmp_path / "featured.jpg",
    )

    assert (result.geometry.source_width, result.geometry.source_height) == (900, 1600)
    assert result.geometry.rendered_artwork_height == 1350
    with Image.open(result.output_path) as output:
        assert output.getexif().get(274, 1) == 1


def test_valid_icc_profile_is_normalized_and_retained(tmp_path):
    source_path = tmp_path / "profiled.jpg"
    output_path = tmp_path / "featured.jpg"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (400, 300), "purple").save(
        source_path, "JPEG", icc_profile=profile
    )
    presentation = derive_carousel_featured_presentation(
        [_artwork(source_path)], grid_color_tone="neutral"
    )

    render_carousel_featured_artwork(
        source_path,
        presentation=presentation,
        output_path=output_path,
    )

    with Image.open(output_path) as output:
        assert output.info.get("icc_profile")


@pytest.mark.parametrize("featured_count", [3, 5, 8])
def test_adaptive_carousels_share_one_presentation_without_fixed_length(
    tmp_path, featured_count
):
    artworks = []
    for index, size in enumerate(((300, 500), (600, 300), (400, 400))):
        path = tmp_path / f"source_{index}.png"
        Image.new("RGB", size, (40 + index * 30, 60, 80)).save(path)
        artworks.append(_artwork(path, 100 + index * 10))
    artworks = [artworks[index % 3] for index in range(featured_count)]
    presentation = derive_carousel_featured_presentation(
        artworks,
        cover_visual_features=_features(110),
        grid_color_tone="warm",
    )

    families = []
    for index, artwork in enumerate(artworks):
        render_carousel_featured_artwork(
            artwork["local_image_path"],
            presentation=presentation,
            output_path=tmp_path / f"featured_{index}.jpg",
        )
        families.append(presentation.field_family)

    assert len(families) == featured_count
    assert set(families) == {GalleryFieldFamily.WARM_LIGHT}
