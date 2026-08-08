import io
import random
import logging
import requests
from PIL import Image, ImageOps, ImageDraw
from collections import Counter
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    """
    Applies a solid-color border derived from the painting's dominant color palette.
    Creates an elegant museum-card aesthetic.
    """
    colors = _get_dominant_colors(img, num_colors=5)

    # Pick a muted/dark color for the border (prefer darker tones)
    # Sort by luminance and pick a mid-dark shade
    colors_with_lum = [(c, 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) for c in colors]
    colors_with_lum.sort(key=lambda x: x[1])

    # Pick from the darker half of the palette
    border_color = colors_with_lum[random.randint(0, min(2, len(colors_with_lum) - 1))][0]

    # Create new canvas with border
    new_w = img.width + border_size * 2
    new_h = img.height + border_size * 2
    canvas = Image.new("RGB", (new_w, new_h), border_color)
    canvas.paste(img, (border_size, border_size))

    logger.info(f"Applied palette border with color {border_color}")
    return canvas


def _apply_gradient_border(img: Image.Image, border_size: int = 60) -> Image.Image:
    """
    Applies a subtle vertical gradient border using the painting's two dominant colors.
    """
    colors = _get_dominant_colors(img, num_colors=3)
    color_top = colors[0]
    color_bottom = colors[-1]

    new_w = img.width + border_size * 2
    new_h = img.height + border_size * 2
    canvas = Image.new("RGB", (new_w, new_h))
    draw = ImageDraw.Draw(canvas)

    # Draw vertical gradient
    for y in range(new_h):
        ratio = y / new_h
        r = int(color_top[0] * (1 - ratio) + color_bottom[0] * ratio)
        g = int(color_top[1] * (1 - ratio) + color_bottom[1] * ratio)
        b = int(color_top[2] * (1 - ratio) + color_bottom[2] * ratio)
        draw.line([(0, y), (new_w, y)], fill=(r, g, b))

    canvas.paste(img, (border_size, border_size))

    logger.info(f"Applied gradient border: {color_top} -> {color_bottom}")
    return canvas


def process_artwork_image(image_url: str, output_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Downloads an image from URL, applies a random frame style
    (palette border, gradient border, or clean), and saves as JPEG.
    """
    logger.info(f"Downloading artwork image from: {image_url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    res = requests.get(image_url, headers=headers, timeout=30)
    res.raise_for_status()

    img = Image.open(io.BytesIO(res.content))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Resize if image is excessively large (> 2048px on max dimension)
    max_dim = 2048
    orig_w, orig_h = img.size
    if orig_w > max_dim or orig_h > max_dim:
        scale = max_dim / max(orig_w, orig_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        logger.info(f"Resized image from ({orig_w}x{orig_h}) to ({new_w}x{new_h})")

    # Apply random frame style
    style = random.choice(FRAME_STYLES)
    logger.info(f"Applying frame style: '{style}'")

    if style == "palette_border":
        img = _apply_palette_border(img, border_size=random.randint(40, 80))
    elif style == "gradient_border":
        img = _apply_gradient_border(img, border_size=random.randint(40, 80))
    # "clean" = no frame, just the painting

    # Ensure clean RGB format
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Save clean JPEG
    img.save(output_path, "JPEG", quality=95, icc_profile=None)
    logger.info(f"Artwork processed and saved to: {output_path}")

    return output_path


def upload_temp_image(image_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Uploads processed image to a public HTTPS host that returns a DIRECT image URL
    with Content-Type: image/jpeg header (required by Instagram Graph API).
    """
    logger.info("Uploading image to public HTTPS host for Instagram Graph API...")

    # Method 1: catbox.moe (returns direct raw image URL, no redirects)
    try:
        logger.info("Trying catbox.moe...")
        with open(image_path, "rb") as f:
            res = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": ("artwork.jpg", f, "image/jpeg")},
                timeout=30
            )
        if res.status_code == 200 and res.text.strip().startswith("http"):
            url = res.text.strip()
            logger.info(f"catbox.moe upload successful: {url}")
            if _verify_image_url(url):
                return url
            else:
                logger.warning("catbox.moe URL did not pass verification.")
    except Exception as e:
        logger.warning(f"catbox.moe upload failed: {e}")

    # Method 2: freeimage.host
    try:
        logger.info("Trying freeimage.host...")
        import base64
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
        res = requests.post(
            "https://freeimage.host/api/1/upload",
            data={
                "key": "6d207e02198a847aa98d0a2a901485a5",
                "action": "upload",
                "source": image_data,
                "format": "json"
            },
            timeout=30
        )
        if res.status_code == 200:
            data = res.json()
            url = data.get("image", {}).get("url")
            if url:
                logger.info(f"freeimage.host upload successful: {url}")
                return url
    except Exception as e:
        logger.warning(f"freeimage.host upload failed: {e}")

    # Method 3: litterbox.catbox.moe (1 hour retention)
    try:
        logger.info("Trying litterbox.catbox.moe...")
        with open(image_path, "rb") as f:
            res = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": ("artwork.jpg", f, "image/jpeg")},
                timeout=30
            )
        if res.status_code == 200 and res.text.strip().startswith("http"):
            url = res.text.strip()
            logger.info(f"litterbox upload successful: {url}")
            return url
    except Exception as e:
        logger.warning(f"litterbox upload failed: {e}")

    raise RuntimeError("Could not upload image to any public host!")


def _verify_image_url(url: str) -> bool:
    """Verifies that a URL returns actual image data with correct Content-Type."""
    try:
        head_res = requests.head(url, timeout=10, allow_redirects=True)
        content_type = head_res.headers.get("Content-Type", "")
        is_image = "image/" in content_type
        logger.info(f"URL verification - Status: {head_res.status_code}, Content-Type: {content_type}, Is Image: {is_image}")
        return is_image and head_res.status_code == 200
    except Exception as e:
        logger.warning(f"URL verification failed: {e}")
        return False
