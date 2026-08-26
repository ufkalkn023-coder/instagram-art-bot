"""Deterministic artwork-only presentation for carousel featured slides."""

from __future__ import annotations

import logging
import os
import statistics
import tempfile
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageCms, ImageOps

from src.artwork_visual_features import extract_visual_features

logger = logging.getLogger(__name__)

CAROUSEL_CANVAS_WIDTH = 1080
CAROUSEL_CANVAS_HEIGHT = 1350
CAROUSEL_JPEG_QUALITY = 95
LIGHT_ARTWORK_LUMINANCE_THRESHOLD = 170.0


class CarouselFeaturedRenderMode(str, Enum):
    """Supported featured-slide render modes."""

    CAROUSEL_GALLERY_FIELD = "CAROUSEL_GALLERY_FIELD"


class GalleryFieldFamily(str, Enum):
    """Small stable set of neutral carousel-level field treatments."""

    WARM_LIGHT = "WARM_LIGHT"
    NEUTRAL_LIGHT = "NEUTRAL_LIGHT"
    WARM_DARK = "WARM_DARK"
    NEUTRAL_DARK = "NEUTRAL_DARK"


_FIELD_COLORS: dict[GalleryFieldFamily, tuple[int, int, int]] = {
    GalleryFieldFamily.WARM_LIGHT: (238, 234, 225),
    GalleryFieldFamily.NEUTRAL_LIGHT: (235, 235, 232),
    GalleryFieldFamily.WARM_DARK: (37, 33, 29),
    GalleryFieldFamily.NEUTRAL_DARK: (32, 33, 34),
}
_WARM_GRID_TONES = frozenset({"red", "orange", "yellow", "brown", "warm"})


@dataclass(frozen=True)
class CarouselFeaturedPresentation:
    """One field decision shared by every featured slide in a carousel."""

    mode: CarouselFeaturedRenderMode
    canvas_width: int
    canvas_height: int
    field_family: GalleryFieldFamily
    field_value: tuple[int, int, int]
    evidence_median_luminance: float | None

    @property
    def field_policy(self) -> str:
        return self.field_family.value


@dataclass(frozen=True)
class ArtworkRenderGeometry:
    """Auditable source and contain-fit geometry in display orientation."""

    source_width: int
    source_height: int
    source_aspect_ratio: float
    rendered_artwork_x: int
    rendered_artwork_y: int
    rendered_artwork_width: int
    rendered_artwork_height: int
    rendered_artwork_aspect_ratio: float


@dataclass(frozen=True)
class CarouselFeaturedRenderResult:
    """Rendered artifact plus its fidelity geometry."""

    output_path: str
    geometry: ArtworkRenderGeometry


def _feature_luminance(features: Any) -> float | None:
    if features is None:
        return None
    if isinstance(features, Mapping):
        value = features.get("mean_luminance")
    else:
        value = getattr(features, "mean_luminance", None)
    if isinstance(value, (int, float)) and 0 <= float(value) <= 255:
        return float(value)
    return None


def _artwork_luminance(artwork: Mapping[str, Any]) -> float | None:
    measured = _feature_luminance(artwork.get("visual_features"))
    if measured is not None:
        return measured
    source_path = artwork.get("local_image_path")
    if not source_path:
        return None
    try:
        return extract_visual_features(str(source_path)).mean_luminance
    except (OSError, SyntaxError, ValueError):
        return None


def derive_carousel_featured_presentation(
    featured_artworks: Sequence[Mapping[str, Any]],
    *,
    cover_visual_features: Any = None,
    grid_color_tone: str = "neutral",
) -> CarouselFeaturedPresentation:
    """Choose one deterministic neutral field for the entire carousel.

    Warm persisted grid tones select the warm family; all other/unknown tones
    select neutral. A median luminance of 170 or above selects a dark field so
    a predominantly light set remains legible. Dark and mid-tone sets use a
    light field. The cover contributes one sample when its measurement exists.
    """
    luminances = [
        value
        for value in (
            _feature_luminance(cover_visual_features),
            *(_artwork_luminance(artwork) for artwork in featured_artworks),
        )
        if value is not None
    ]
    median_luminance = (
        round(float(statistics.median(luminances)), 2) if luminances else None
    )
    dark = (
        median_luminance is not None
        and median_luminance >= LIGHT_ARTWORK_LUMINANCE_THRESHOLD
    )
    warm = str(grid_color_tone or "").strip().casefold() in _WARM_GRID_TONES
    if warm and dark:
        field_family = GalleryFieldFamily.WARM_DARK
    elif warm:
        field_family = GalleryFieldFamily.WARM_LIGHT
    elif dark:
        field_family = GalleryFieldFamily.NEUTRAL_DARK
    else:
        field_family = GalleryFieldFamily.NEUTRAL_LIGHT

    return CarouselFeaturedPresentation(
        mode=CarouselFeaturedRenderMode.CAROUSEL_GALLERY_FIELD,
        canvas_width=CAROUSEL_CANVAS_WIDTH,
        canvas_height=CAROUSEL_CANVAS_HEIGHT,
        field_family=field_family,
        field_value=_FIELD_COLORS[field_family],
        evidence_median_luminance=median_luminance,
    )


def calculate_contain_geometry(
    source_width: int,
    source_height: int,
    *,
    canvas_width: int = CAROUSEL_CANVAS_WIDTH,
    canvas_height: int = CAROUSEL_CANVAS_HEIGHT,
) -> ArtworkRenderGeometry:
    """Return a centered maximum-size contain fit without crop or distortion."""
    if min(source_width, source_height, canvas_width, canvas_height) <= 0:
        raise ValueError("Artwork and canvas dimensions must be positive")

    source_aspect = source_width / source_height
    canvas_aspect = canvas_width / canvas_height
    if source_aspect >= canvas_aspect:
        rendered_width = canvas_width
        rendered_height = max(1, round(canvas_width / source_aspect))
    else:
        rendered_height = canvas_height
        rendered_width = max(1, round(canvas_height * source_aspect))

    rendered_width = min(canvas_width, rendered_width)
    rendered_height = min(canvas_height, rendered_height)
    rendered_x = (canvas_width - rendered_width) // 2
    rendered_y = (canvas_height - rendered_height) // 2
    return ArtworkRenderGeometry(
        source_width=source_width,
        source_height=source_height,
        source_aspect_ratio=round(source_aspect, 6),
        rendered_artwork_x=rendered_x,
        rendered_artwork_y=rendered_y,
        rendered_artwork_width=rendered_width,
        rendered_artwork_height=rendered_height,
        rendered_artwork_aspect_ratio=round(rendered_width / rendered_height, 6),
    )


def _normalized_srgb(image: Image.Image, icc_profile: bytes | None) -> tuple[Image.Image, bytes | None]:
    """Normalize pixels to RGB, retaining color appearance when an ICC profile is usable."""
    if icc_profile:
        try:
            source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
            output_profile = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(
                image,
                source_profile,
                output_profile,
                outputMode="RGB",
            )
            output_icc = ImageCms.ImageCmsProfile(output_profile).tobytes()
            return converted, output_icc
        except (OSError, TypeError, ValueError):
            logger.warning("Artwork ICC profile could not be applied; rendering as RGB.")
    return image.convert("RGB"), None


def _save_jpeg_atomically(
    image: Image.Image,
    output_path: str,
    *,
    icc_profile: bytes | None,
) -> None:
    output_directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_directory, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".carousel-featured-",
            suffix=".jpg",
            dir=output_directory,
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
        save_options: dict[str, Any] = {
            "format": "JPEG",
            "quality": CAROUSEL_JPEG_QUALITY,
            "subsampling": 0,
        }
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        image.save(temporary_path, **save_options)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass


def render_carousel_featured_artwork(
    source_path: str | Path,
    *,
    presentation: CarouselFeaturedPresentation,
    output_path: str | Path,
) -> CarouselFeaturedRenderResult:
    """Render one complete artwork exactly once over the shared flat field."""
    if presentation.mode is not CarouselFeaturedRenderMode.CAROUSEL_GALLERY_FIELD:
        raise ValueError(f"Unsupported carousel featured mode: {presentation.mode}")

    with Image.open(source_path) as opened:
        opened.load()
        source_icc = opened.info.get("icc_profile")
        normalized = ImageOps.exif_transpose(opened)
        artwork, output_icc = _normalized_srgb(
            normalized,
            source_icc if isinstance(source_icc, bytes) else None,
        )

    geometry = calculate_contain_geometry(
        artwork.width,
        artwork.height,
        canvas_width=presentation.canvas_width,
        canvas_height=presentation.canvas_height,
    )
    resized = artwork.resize(
        (
            geometry.rendered_artwork_width,
            geometry.rendered_artwork_height,
        ),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new(
        "RGB",
        (presentation.canvas_width, presentation.canvas_height),
        presentation.field_value,
    )
    # The artwork layer is intentionally composited once: no clone, blur, crop,
    # reflection, extension, typography, border, or shadow is part of this path.
    canvas.paste(
        resized,
        (geometry.rendered_artwork_x, geometry.rendered_artwork_y),
    )
    final_path = str(output_path)
    _save_jpeg_atomically(canvas, final_path, icc_profile=output_icc)
    logger.info(
        "carousel_featured_rendered mode=%s field=%s source=%sx%s artwork=%s,%s,%sx%s canvas=%sx%s",
        presentation.mode.value,
        presentation.field_family.value,
        geometry.source_width,
        geometry.source_height,
        geometry.rendered_artwork_x,
        geometry.rendered_artwork_y,
        geometry.rendered_artwork_width,
        geometry.rendered_artwork_height,
        presentation.canvas_width,
        presentation.canvas_height,
    )
    return CarouselFeaturedRenderResult(final_path, geometry)
