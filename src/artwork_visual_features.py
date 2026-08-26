"""Cheap deterministic visual signals extracted from already-validated artwork files."""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, ImageOps, ImageStat


MAX_FEATURE_DIMENSION = 128


class ArtworkOrientation(str, Enum):
    PORTRAIT = "PORTRAIT"
    SQUAREISH = "SQUAREISH"
    LANDSCAPE = "LANDSCAPE"
    UNKNOWN = "UNKNOWN"


class LuminanceBucket(str, Enum):
    DARK = "DARK"
    MID = "MID"
    LIGHT = "LIGHT"
    UNKNOWN = "UNKNOWN"


class DominantColorFamily(str, Enum):
    RED = "RED"
    ORANGE = "ORANGE"
    YELLOW = "YELLOW"
    GREEN = "GREEN"
    CYAN = "CYAN"
    BLUE = "BLUE"
    PURPLE = "PURPLE"
    NEUTRAL = "NEUTRAL"
    DARK = "DARK"
    LIGHT = "LIGHT"
    UNKNOWN = "UNKNOWN"


class ContrastBucket(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ArtworkVisualFeatures:
    width: int | None
    height: int | None
    aspect_ratio: float | None
    orientation: ArtworkOrientation
    mean_luminance: float | None
    luminance_bucket: LuminanceBucket
    mean_saturation: float | None
    dominant_color_family: DominantColorFamily
    contrast_bucket: ContrastBucket
    sample_width: int = 0
    sample_height: int = 0


def classify_orientation(width: int | None, height: int | None) -> ArtworkOrientation:
    """Classify aspect ratio without imposing an editorial quota."""
    if not width or not height or width <= 0 or height <= 0:
        return ArtworkOrientation.UNKNOWN
    ratio = width / height
    if ratio < 0.9:
        return ArtworkOrientation.PORTRAIT
    if ratio <= 1.1:
        return ArtworkOrientation.SQUAREISH
    return ArtworkOrientation.LANDSCAPE


def _luminance_bucket(value: float) -> LuminanceBucket:
    if value < 85.0:
        return LuminanceBucket.DARK
    if value >= 170.0:
        return LuminanceBucket.LIGHT
    return LuminanceBucket.MID


def _contrast_bucket(value: float) -> ContrastBucket:
    if value < 35.0:
        return ContrastBucket.LOW
    if value >= 70.0:
        return ContrastBucket.HIGH
    return ContrastBucket.MEDIUM


def _dominant_color(
    pixels: list[tuple[int, int, int]], mean_luminance: float, mean_saturation: float
) -> DominantColorFamily:
    if mean_luminance < 38.0 and mean_saturation < 0.22:
        return DominantColorFamily.DARK
    if mean_luminance > 218.0 and mean_saturation < 0.18:
        return DominantColorFamily.LIGHT
    if mean_saturation < 0.12:
        return DominantColorFamily.NEUTRAL

    sin_sum = 0.0
    cos_sum = 0.0
    total_weight = 0.0
    for red, green, blue in pixels:
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        weight = saturation * max(0.15, value)
        angle = hue * math.tau
        sin_sum += math.sin(angle) * weight
        cos_sum += math.cos(angle) * weight
        total_weight += weight
    if total_weight == 0:
        return DominantColorFamily.NEUTRAL

    hue_degrees = (math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0)
    if hue_degrees < 15 or hue_degrees >= 345:
        return DominantColorFamily.RED
    if hue_degrees < 45:
        return DominantColorFamily.ORANGE
    if hue_degrees < 70:
        return DominantColorFamily.YELLOW
    if hue_degrees < 165:
        return DominantColorFamily.GREEN
    if hue_degrees < 195:
        return DominantColorFamily.CYAN
    if hue_degrees < 255:
        return DominantColorFamily.BLUE
    if hue_degrees < 345:
        return DominantColorFamily.PURPLE
    return DominantColorFamily.RED


def features_from_dimensions(
    width: int | None, height: int | None
) -> ArtworkVisualFeatures:
    """Provide honest dimension-only features when pixel decoding is unavailable."""
    aspect_ratio = round(width / height, 4) if width and height else None
    return ArtworkVisualFeatures(
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        orientation=classify_orientation(width, height),
        mean_luminance=None,
        luminance_bucket=LuminanceBucket.UNKNOWN,
        mean_saturation=None,
        dominant_color_family=DominantColorFamily.UNKNOWN,
        contrast_bucket=ContrastBucket.UNKNOWN,
    )


def extract_visual_features(image_path: str | Path) -> ArtworkVisualFeatures:
    """Extract bounded pixel statistics without changing the source image."""
    with Image.open(image_path) as opened:
        display_image = ImageOps.exif_transpose(opened)
        width, height = display_image.size
        sample = display_image.convert("RGB")
        sample.thumbnail(
            (MAX_FEATURE_DIMENSION, MAX_FEATURE_DIMENSION),
            Image.Resampling.LANCZOS,
        )
        pixels = list(sample.getdata())

    luminances = [0.2126 * red + 0.7152 * green + 0.0722 * blue for red, green, blue in pixels]
    mean_luminance = sum(luminances) / len(luminances)
    saturations = [
        colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)[1]
        for red, green, blue in pixels
    ]
    mean_saturation = sum(saturations) / len(saturations)
    contrast = ImageStat.Stat(sample.convert("L")).stddev[0]

    return ArtworkVisualFeatures(
        width=width,
        height=height,
        aspect_ratio=round(width / height, 4),
        orientation=classify_orientation(width, height),
        mean_luminance=round(mean_luminance, 2),
        luminance_bucket=_luminance_bucket(mean_luminance),
        mean_saturation=round(mean_saturation, 4),
        dominant_color_family=_dominant_color(pixels, mean_luminance, mean_saturation),
        contrast_bucket=_contrast_bucket(contrast),
        sample_width=sample.width,
        sample_height=sample.height,
    )
