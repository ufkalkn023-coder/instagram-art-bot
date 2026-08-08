import io
import logging
import requests
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def process_artwork_image(image_url: str, output_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Downloads an image from URL and formats it into a 1080x1350 (4:5) Instagram post
    with a blurred passe-partout background derived from the artwork itself.
    """
    logger.info(f"Downloading artwork image from: {image_url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://www.artic.edu/"
    }
    res = requests.get(image_url, headers=headers, timeout=30)
    res.raise_for_status()

    img = Image.open(io.BytesIO(res.content))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    target_w = config.TARGET_WIDTH
    target_h = config.TARGET_HEIGHT
    target_ratio = target_w / target_h

    orig_w, orig_h = img.size
    orig_ratio = orig_w / orig_h

    # 1. Create Blurred Background
    # Crop artwork to fill 1080x1350 container
    if orig_ratio > target_ratio:
        # Original is wider than target ratio
        new_h = orig_h
        new_w = int(orig_h * target_ratio)
        left = (orig_w - new_w) // 2
        crop_box = (left, 0, left + new_w, orig_h)
    else:
        # Original is taller than target ratio
        new_w = orig_w
        new_h = int(orig_w / target_ratio)
        top = (orig_h - new_h) // 2
        crop_box = (0, top, orig_w, top + new_h)

    bg = img.crop(crop_box).resize((target_w, target_h), Image.Resampling.LANCZOS)
    
    # Apply Gaussian Blur to background
    bg = bg.filter(ImageFilter.GaussianBlur(radius=config.BLUR_RADIUS))

    # Slightly darken background to make foreground painting stand out
    enhancer = ImageEnhance.Brightness(bg)
    bg = enhancer.enhance(0.70)

    # 2. Resize Foreground Artwork preserving original aspect ratio
    # Provide inner padding margin (e.g. 5% padding)
    padding_pct = 0.06
    max_fg_w = int(target_w * (1 - 2 * padding_pct))
    max_fg_h = int(target_h * (1 - 2 * padding_pct))

    scale_w = max_fg_w / orig_w
    scale_h = max_fg_h / orig_h
    scale = min(scale_w, scale_h)

    fg_w = int(orig_w * scale)
    fg_h = int(orig_h * scale)

    fg = img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

    # Center foreground artwork onto blurred canvas
    pos_x = (target_w - fg_w) // 2
    pos_y = (target_h - fg_h) // 2

    # Paste crisp artwork
    bg.paste(fg, (pos_x, pos_y))

    # Ensure clean RGB format (no RGBA, no CMYK, no ICC profile issues)
    if bg.mode != "RGB":
        bg = bg.convert("RGB")

    # Save output as clean JPEG (Instagram compatible)
    bg.save(output_path, "JPEG", quality=95, icc_profile=None)
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
            # Verify the URL returns actual image data
            if _verify_image_url(url):
                return url
            else:
                logger.warning("catbox.moe URL did not pass verification.")
    except Exception as e:
        logger.warning(f"catbox.moe upload failed: {e}")

    # Method 2: freeimage.host (free, no API key needed for anonymous uploads)
    try:
        logger.info("Trying freeimage.host...")
        import base64
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
        res = requests.post(
            "https://freeimage.host/api/1/upload",
            data={
                "key": "6d207e02198a847aa98d0a2a901485a5",  # Public demo key
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

    # Method 3: litterbox.catbox.moe (1 hour retention, direct URL)
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

