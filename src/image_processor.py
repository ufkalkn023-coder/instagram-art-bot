import io
import os
import random
import logging
import requests
import time
import uuid
import boto3
from datetime import datetime
from botocore.exceptions import ClientError
from typing import Tuple, Optional, List, Dict, Any
from PIL import Image, ImageOps, ImageDraw, ImageFont

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Available frame styles
FRAME_STYLES = ["palette_border", "gradient_border", "clean"]

PORTRAIT_OR_SQUARE_MAX_RATIO = 1.15
PANORAMIC_MIN_RATIO = 1.80
WIDE_PANORAMA_MAX_RATIO = 2.40
PRESENTATION_PORTRAIT_OR_SQUARE = "portrait_or_square"
PRESENTATION_LANDSCAPE = "landscape"
PRESENTATION_PANORAMIC = "panoramic"
PANORAMA_WIDE = "wide_panorama"
PANORAMA_EXTREME = "extreme_panorama"
WIDE_PANORAMA_TOP_SPACE_SHARE = 0.42
EXTREME_PANORAMA_TOP_SPACE_SHARE = 0.34
MUSEUM_MATTE = (244, 242, 237)  # #F4F2ED
SUBTLE_BORDER = (216, 213, 206)  # #D8D5CE


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
    """
    Takes an already downloaded raw image, normalizes it (EXIF transpose, RGB), and determines orientation.
    Returns (output_path, orientation).
    """
    logger.info(f"Preparing local artwork image: {local_path}")
    
    img = Image.open(local_path)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    img.save(local_path, "JPEG", quality=100)
    
    orientation = "horizontal" if img.width > img.height else "vertical"
    logger.info(f"Prepared raw image: {img.width}x{img.height} ({orientation})")
    
    return local_path, orientation


def classify_presentation_mode(width: int, height: int) -> str:
    """Classify a downloaded artwork by its true aspect ratio."""
    if width <= 0 or height <= 0:
        raise ValueError("Artwork dimensions must be positive.")
    ratio = width / height
    if ratio <= PORTRAIT_OR_SQUARE_MAX_RATIO:
        return PRESENTATION_PORTRAIT_OR_SQUARE
    if ratio <= PANORAMIC_MIN_RATIO:
        return PRESENTATION_LANDSCAPE
    return PRESENTATION_PANORAMIC


def classify_panorama_submode(width: int, height: int) -> str | None:
    """Distinguish wide panoramas from extreme, single-post-limited ones."""
    if classify_presentation_mode(width, height) != PRESENTATION_PANORAMIC:
        return None
    return PANORAMA_WIDE if width / height <= WIDE_PANORAMA_MAX_RATIO else PANORAMA_EXTREME


def _fit_within_canvas(width: int, height: int, canvas_width: int, canvas_height: int) -> tuple[int, int]:
    scale = min(canvas_width / width, canvas_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def calculate_feed_artwork_box(
    width: int,
    height: int,
    canvas_width: int = config.TARGET_WIDTH,
    canvas_height: int = config.TARGET_HEIGHT,
) -> tuple[int, int, int, int]:
    """Return the deterministic full-artwork placement box for a feed canvas."""
    mode = classify_presentation_mode(width, height)
    art_width, art_height = _fit_within_canvas(width, height, canvas_width, canvas_height)
    paste_x = (canvas_width - art_width) // 2
    available_vertical_space = canvas_height - art_height

    if mode != PRESENTATION_PANORAMIC:
        paste_y = available_vertical_space // 2
    elif classify_panorama_submode(width, height) == PANORAMA_WIDE:
        paste_y = round(available_vertical_space * WIDE_PANORAMA_TOP_SPACE_SHARE)
    else:
        paste_y = round(available_vertical_space * EXTREME_PANORAMA_TOP_SPACE_SHARE)

    return paste_x, paste_y, art_width, art_height


def _needs_subtle_border(img: Image.Image) -> bool:
    """Return whether a near-white neutral artwork needs separation from the matte."""
    sample = img.resize((32, 32), Image.Resampling.BOX)
    edge_pixels = []
    for coordinate in range(32):
        edge_pixels.extend(
            (
                sample.getpixel((coordinate, 0)),
                sample.getpixel((coordinate, 31)),
                sample.getpixel((0, coordinate)),
                sample.getpixel((31, coordinate)),
            )
        )
    red = sum(pixel[0] for pixel in edge_pixels) / len(edge_pixels)
    green = sum(pixel[1] for pixel in edge_pixels) / len(edge_pixels)
    blue = sum(pixel[2] for pixel in edge_pixels) / len(edge_pixels)
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return luminance >= 225 and max(red, green, blue) - min(red, green, blue) <= 25


def create_feed_post(raw_image_path: str, artist_name: str = "", artwork_title: str = "", output_path: str = config.OUTPUT_IMAGE_PATH, base_font_size: int = 46) -> str:
    """
    Present a full artwork on a 1080x1350 museum-matte feed canvas.

    The downloaded image dimensions are the source of truth. The artwork is
    always fit inside the canvas without cropping, distortion, or a blurred
    duplicate background.
    """
    img = Image.open(raw_image_path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    canvas_w = config.TARGET_WIDTH   # 1080
    canvas_h = config.TARGET_HEIGHT  # 1350
    mode = classify_presentation_mode(img.width, img.height)
    panorama_submode = classify_panorama_submode(img.width, img.height)
    paste_x, paste_y, art_w, art_h = calculate_feed_artwork_box(
        img.width,
        img.height,
        canvas_w,
        canvas_h,
    )
    art_resized = img.resize((art_w, art_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (canvas_w, canvas_h), MUSEUM_MATTE)
    canvas.paste(art_resized, (paste_x, paste_y))

    if _needs_subtle_border(art_resized):
        ImageDraw.Draw(canvas).rectangle(
            (paste_x, paste_y, paste_x + art_w - 1, paste_y + art_h - 1),
            outline=SUBTLE_BORDER,
            width=1,
        )

    canvas.save(output_path, "JPEG", quality=95)
    logger.info(
        "Feed post image processed mode=%s panorama_submode=%s source=%sx%s rendered=%sx%s matte=%s saved_to=%s",
        mode,
        panorama_submode or "none",
        img.width,
        img.height,
        art_w,
        art_h,
        "#F4F2ED",
        output_path,
    )
    return output_path



def upload_temp_media(file_path: str) -> str:
    """
    Uploads processed image to Cloudflare R2.
    Returns the public HTTP URL of the uploaded image.
    """
    logger.info("Uploading image to Cloudflare R2...")
    
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
    bucket_name = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
    public_url_base = os.environ.get("CLOUDFLARE_R2_PUBLIC_URL", "").strip()
    
    if not all([account_id, access_key, secret_key, bucket_name, public_url_base]):
        raise ValueError("Missing one or more CLOUDFLARE_R2_* environment variables!")
        
    public_url_base = public_url_base.rstrip('/')
    
    # Generate unique object key
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    unique_id = str(uuid.uuid4())[:8]
    
    content_type = "image/jpeg"
    object_key = f"images/{timestamp}_{unique_id}.jpg"
        
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    
    # Initialize boto3 S3 client
    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto"
    )
    
    # Upload with 3 retries
    upload_success = False
    for attempt in range(1, 4):
        try:
            logger.info(f"R2 upload attempt {attempt}/3...")
            s3_client.upload_file(
                file_path, 
                bucket_name, 
                object_key,
                ExtraArgs={"ContentType": content_type}
            )
            upload_success = True
            break
        except ClientError as e:
            logger.warning(f"R2 upload failed on attempt {attempt}: {e}")
            if attempt < 3:
                time.sleep(2)
        except Exception as e:
            logger.warning(f"Unexpected R2 upload error on attempt {attempt}: {e}")
            if attempt < 3:
                time.sleep(2)
                
    if not upload_success:
        raise RuntimeError("Failed to upload media to Cloudflare R2 after 3 attempts.")
        
    # Construct public URL
    final_url = f"{public_url_base}/{object_key}"
    logger.info(f"File uploaded to R2. Validating public URL: {final_url}")
    
    # HEAD check to ensure Instagram can reach it
    for head_attempt in range(1, 4):
        try:
            head_res = requests.head(final_url, allow_redirects=True, timeout=10)
            if head_res.status_code == 200:
                res_content_type = head_res.headers.get("Content-Type", "")
                res_content_length = int(head_res.headers.get("Content-Length", 0))
                
                # Verify length and type
                if res_content_length > 0 and content_type in res_content_type:
                    logger.info("Public URL health check passed!")
                    return final_url
                else:
                    logger.warning(f"Health check warning: type={res_content_type}, length={res_content_length}")
            else:
                logger.warning(f"Health check failed with HTTP {head_res.status_code}")
                
        except Exception as e:
            logger.warning(f"Health check error: {e}")
            
        time.sleep(2)
        
    raise RuntimeError(f"R2 uploaded successfully, but public URL health check failed: {final_url}")
