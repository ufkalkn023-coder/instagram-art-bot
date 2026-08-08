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

    # Save output JPEG
    bg.save(output_path, "JPEG", quality=95)
    logger.info(f"Artwork processed and saved to: {output_path}")

    return output_path

def upload_temp_image(image_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Uploads processed image to a temporary public HTTPS host (catbox/tmpfiles)
    so Instagram Graph API can download it directly.
    """
    logger.info("Uploading image to temporary public HTTPS host for Instagram Graph API...")
    try:
        # Try tmpfiles.org
        with open(image_path, "rb") as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": f}, timeout=20)
        if res.status_code == 200:
            data = res.json()
            url = data.get("data", {}).get("url")
            if url:
                # Convert https://tmpfiles.org/1234/img.jpg -> https://tmpfiles.org/dl/1234/img.jpg for direct access
                direct_url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                logger.info(f"Image uploaded successfully: {direct_url}")
                return direct_url
    except Exception as e:
        logger.warning(f"tmpfiles.org upload failed: {e}")

    try:
        # Fallback to litterbox.catbox.moe (1 hour retention)
        with open(image_path, "rb") as f:
            res = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": f},
                timeout=20
            )
        if res.status_code == 200 and res.text.startswith("http"):
            url = res.text.strip()
            logger.info(f"Image uploaded to litterbox: {url}")
            return url
    except Exception as e:
        logger.warning(f"litterbox upload failed: {e}")

    raise RuntimeError("Could not upload image to any temporary public host!")

