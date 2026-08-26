import os
import random
import logging
import requests
import time
import uuid
import boto3
from datetime import datetime
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from typing import Tuple

from PIL import Image, ImageDraw

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Available frame styles
FRAME_STYLES = ["palette_border", "gradient_border", "clean"]
R2_CLIENT_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    # upload_temp_media owns the three logged application-level attempts.
    retries={"total_max_attempts": 1, "mode": "standard"},
)
_TRANSIENT_R2_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_R2_ERROR_CODES = {
    "InternalError",
    "RequestTimeout",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
}
_TRANSIENT_R2_EXCEPTIONS = (
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)


def _is_transient_r2_upload_error(error: BaseException) -> bool:
    """Classify retryable R2 failures, including boto3 wrapper chains."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSIENT_R2_EXCEPTIONS):
            return True
        if isinstance(current, ClientError):
            response = current.response
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            return status in _TRANSIENT_R2_HTTP_STATUSES or code in _TRANSIENT_R2_ERROR_CODES
        current = current.__cause__ or current.__context__
    return False


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
    
    content_type, file_suffix = _upload_image_metadata(file_path)
    object_key = f"images/{timestamp}_{unique_id}{file_suffix}"
        
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    
    # Initialize boto3 S3 client
    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=R2_CLIENT_CONFIG,
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
        except Exception as e:
            retryable = _is_transient_r2_upload_error(e)
            logger.warning(
                "R2 upload failed attempt=%s/3 error=%s retryable=%s",
                attempt,
                type(e).__name__,
                retryable,
            )
            if retryable and attempt < 3:
                time.sleep(2)
                continue
            break
                
    if not upload_success:
        raise RuntimeError("Failed to upload media to Cloudflare R2 after 3 attempts.")
        
    # Construct public URL
    final_url = f"{public_url_base}/{object_key}"
    logger.info("File uploaded to R2. Validating public object: %s", object_key)
    
    # HEAD check to ensure Instagram can reach it
    for head_attempt in range(1, 4):
        try:
            head_res = requests.head(final_url, allow_redirects=True, timeout=10)
            if head_res.status_code == 200:
                res_content_type = head_res.headers.get("Content-Type", "")
                res_content_length = int(head_res.headers.get("Content-Length", 0))
                
                # Verify length and type
                response_media_type = res_content_type.split(";", 1)[0].strip().casefold()
                if res_content_length > 0 and response_media_type == content_type:
                    logger.info("Public URL health check passed!")
                    return final_url
                else:
                    logger.warning(f"Health check warning: type={res_content_type}, length={res_content_length}")
            else:
                logger.warning(f"Health check failed with HTTP {head_res.status_code}")
                
        except Exception as e:
            logger.warning(f"Health check error: {e}")
            
        time.sleep(2)
        
    raise RuntimeError(
        f"R2 object {object_key} uploaded successfully, but its public health check failed."
    )
