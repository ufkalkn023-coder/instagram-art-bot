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
from PIL import Image, ImageOps, ImageDraw, ImageFilter, ImageFont

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


def create_feed_post(raw_image_path: str, artist_name: str, artwork_title: str, output_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Processes a raw image into a 1080x1350 Feed post with a Museum Gallery layout.
    """
    logger.info("Processing feed artwork image with Museum Layout...")
    img = Image.open(raw_image_path)
    
    canvas_w = config.TARGET_WIDTH   # 1080
    canvas_h = config.TARGET_HEIGHT  # 1350
    
    # 1. Background
    bg_color = (244, 241, 234) # #F4F1EA Museum Cream
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    draw = ImageDraw.Draw(canvas)
    
    # 2. Fonts
    font_bold_path = os.path.join(config.BASE_DIR, "assets", "fonts", "Inter-Bold.ttf")
    font_regular_path = os.path.join(config.BASE_DIR, "assets", "fonts", "Inter-Regular.ttf")
    
    text_color = (44, 44, 44) # #2C2C2C Antrasit
    
    try:
        font_artist = ImageFont.truetype(font_bold_path, 42)
        font_title = ImageFont.truetype(font_regular_path, 36)
        font_logo = ImageFont.truetype(font_bold_path, 32)
    except IOError:
        logger.warning("Custom fonts not found. Falling back to default.")
        font_artist = ImageFont.load_default()
        font_title = ImageFont.load_default()
        font_logo = ImageFont.load_default()
        
    # 3. Draw Text
    margin_x = 80
    margin_top = 100
    
    # Convert artist to uppercase
    artist_upper = artist_name.upper() if artist_name else "UNKNOWN ARTIST"
    title_text = artwork_title if artwork_title else "Untitled"
    
    # Handle long titles (truncate with ellipsis)
    if len(title_text) > 45:
        title_text = title_text[:42] + "..."
        
    draw.text((margin_x, margin_top), artist_upper, font=font_artist, fill=text_color)
    draw.text((margin_x, margin_top + 55), title_text, font=font_title, fill=text_color)
    
    # Bottom logo
    logo_text = "artfolio"
    logo_bbox = draw.textbbox((0, 0), logo_text, font=font_logo)
    logo_w = logo_bbox[2] - logo_bbox[0]
    draw.text(((canvas_w - logo_w) // 2, canvas_h - 100), logo_text, font=font_logo, fill=text_color)
    
    # 4. Artwork Area
    img_area_y_start = 220
    img_area_y_end = 1180
    max_w = canvas_w - (margin_x * 2) # 920
    max_h = img_area_y_end - img_area_y_start # 960
    
    img_aspect = img.width / img.height
    
    if img_aspect > max_w / max_h:
        paint_w = max_w
        paint_h = int(paint_w / img_aspect)
    else:
        paint_h = max_h
        paint_w = int(paint_h * img_aspect)
        
    painting = img.resize((paint_w, paint_h), Image.Resampling.LANCZOS)
    
    # Subtle elegant border to separate from background
    painting = ImageOps.expand(painting, border=2, fill=(210, 205, 195))
    
    # Center the painting in the available area
    paste_x = (canvas_w - painting.width) // 2
    paste_y = img_area_y_start + (max_h - painting.height) // 2
    canvas.paste(painting, (paste_x, paste_y))

    canvas.save(output_path, "JPEG", quality=95)
    logger.info(f"Feed post image processed (Museum Layout) and saved to: {output_path}")
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
