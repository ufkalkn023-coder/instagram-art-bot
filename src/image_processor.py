import io
import logging
import requests
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def process_artwork_image(image_url: str, output_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Downloads an image from URL and formats it directly as a clean RGB JPEG
    preserving the original painting's natural aspect ratio (no blurred background).
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

    # Optionally resize if image is excessively large (> 2048px on max dimension)
    max_dim = 2048
    orig_w, orig_h = img.size
    if orig_w > max_dim or orig_h > max_dim:
        scale = max_dim / max(orig_w, orig_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        logger.info(f"Resized image from ({orig_w}x{orig_h}) to ({new_w}x{new_h})")

    # Save clean RGB JPEG
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

