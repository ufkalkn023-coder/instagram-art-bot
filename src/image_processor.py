import random
import logging
from typing import Tuple

from PIL import Image, ImageDraw

import config
from src import r2_media

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Backward-compatible observability aliases; R2 media operations live in r2_media.
R2_CLIENT_CONFIG = r2_media.R2_CLIENT_CONFIG
_is_transient_r2_upload_error = r2_media._is_transient_r2_error

# Available frame styles
FRAME_STYLES = ["palette_border", "gradient_border", "clean"]
def _get_dominant_colors(img: Image.Image, num_colors: int = 5) -> list:
    """Extracts dominant colors from an image using color quantization."""
    small = img.copy()
    small.thumbnail((100, 100))
    small = small.convert("RGB")

    # Quantize to a small number of colors
    quantized = small.quantize(colors=num_colors, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()

    # Extract RGB tuples from palette
    colors = []
    for i in range(num_colors):
        r = palette[i * 3]
        g = palette[i * 3 + 1]
        b = palette[i * 3 + 2]
        colors.append((r, g, b))

    return colors


def _apply_palette_border(img: Image.Image, border_size: int = 60) -> Image.Image:
    """Applies a solid-color border derived from the painting's dominant color palette."""
    colors = _get_dominant_colors(img, num_colors=5)
    colors_with_lum = [(c, 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) for c in colors]
    colors_with_lum.sort(key=lambda x: x[1])
    border_color = colors_with_lum[random.randint(0, min(2, len(colors_with_lum) - 1))][0]

    new_w = img.width + border_size * 2
    new_h = img.height + border_size * 2
    canvas = Image.new("RGB", (new_w, new_h), border_color)
    canvas.paste(img, (border_size, border_size))

    logger.info(f"Applied palette border with color {border_color}")
    return canvas


def _apply_gradient_border(img: Image.Image, border_size: int = 60) -> Image.Image:
    """Applies a subtle vertical gradient border using the painting's two dominant colors."""
    colors = _get_dominant_colors(img, num_colors=3)
    color_top = colors[0]
    color_bottom = colors[-1]

    new_w = img.width + border_size * 2
    new_h = img.height + border_size * 2
    canvas = Image.new("RGB", (new_w, new_h))
    draw = ImageDraw.Draw(canvas)

    for y in range(new_h):
        ratio = y / new_h
        r = int(color_top[0] * (1 - ratio) + color_bottom[0] * ratio)
        g = int(color_top[1] * (1 - ratio) + color_bottom[1] * ratio)
        b = int(color_top[2] * (1 - ratio) + color_bottom[2] * ratio)
        draw.line([(0, y), (new_w, y)], fill=(r, g, b))

    canvas.paste(img, (border_size, border_size))

    logger.info(f"Applied gradient border: {color_top} -> {color_bottom}")
    return canvas


def prepare_local_image(local_path: str) -> Tuple[str, str]:
    """Inspect a downloaded image without changing its bytes.

    The single-post pipeline owns compatibility processing in ``instagram_image``.
    This legacy helper remains for callers that only need an orientation label.
    """
    logger.info(f"Preparing local artwork image: {local_path}")

    from src.instagram_image import inspect_instagram_image_publishability

    inspected = inspect_instagram_image_publishability(local_path)
    if inspected.width is None or inspected.height is None:
        raise ValueError("Local artwork image could not be decoded.")
    orientation = "horizontal" if inspected.width > inspected.height else "vertical"
    logger.info(
        "Inspected raw image without modification: %sx%s (%s)",
        inspected.width,
        inspected.height,
        orientation,
    )
    return local_path, orientation


def create_feed_post(raw_image_path: str, artist_name: str = "", artwork_title: str = "", output_path: str = config.OUTPUT_IMAGE_PATH, base_font_size: int = 46) -> str:
    """Prepare a single feed asset with only required compatibility changes.

    Artist/title/font arguments are retained for API compatibility; single artwork
    images never receive overlays, framing, blur, crop, or forced-canvas treatment.
    """
    from src.instagram_image import (
        InstagramImageNotPublishableError,
        prepare_single_instagram_image,
    )

    prepared = prepare_single_instagram_image(raw_image_path, output_path)
    if prepared.path is None:
        raise InstagramImageNotPublishableError(prepared.publishability)
    return prepared.path


_UPLOAD_IMAGE_TYPES = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


def _upload_image_metadata(file_path: str) -> tuple[str, str]:
    """Derive R2 metadata from decoded bytes instead of the local suffix."""
    try:
        with Image.open(file_path) as image:
            image_format = (image.format or "").upper()
    except (OSError, SyntaxError, ValueError) as error:
        raise ValueError("Media upload requires a valid decoded image.") from error

    try:
        return _UPLOAD_IMAGE_TYPES[image_format]
    except KeyError as error:
        raise ValueError(f"Unsupported image upload format: {image_format or 'unknown'}") from error



def upload_temp_media(
    file_path: str, publication_id: str
) -> r2_media.TempMediaUpload:
    """Upload a decoded image into one publication-owned R2 namespace."""
    content_type, file_suffix = _upload_image_metadata(file_path)
    return r2_media.stage_temp_media(
        file_path,
        publication_id=publication_id,
        content_type=content_type,
        file_suffix=file_suffix,
    )
