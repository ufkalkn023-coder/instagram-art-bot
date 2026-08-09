import io
import os
import random
import logging
import requests
from typing import Tuple, Optional
from PIL import Image, ImageOps, ImageDraw, ImageFilter
import moviepy as mpy
import moviepy.video.fx as vfx
import moviepy.audio.fx as afx

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


def download_raw_image(image_url: str, output_path: str = config.OUTPUT_RAW_IMAGE_PATH) -> Tuple[str, str]:
    """
    Downloads raw image, saves it, and determines orientation (horizontal or vertical/square).
    Returns (output_path, orientation).
    """
    logger.info(f"Downloading raw artwork image from: {image_url}")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/*",
    }
    res = requests.get(image_url, headers=headers, timeout=30)
    res.raise_for_status()

    img = Image.open(io.BytesIO(res.content))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    img.save(output_path, "JPEG", quality=100)
    
    orientation = "horizontal" if img.width > img.height else "vertical"
    logger.info(f"Downloaded raw image: {img.width}x{img.height} ({orientation})")
    
    return output_path, orientation


def create_feed_post(raw_image_path: str, output_path: str = config.OUTPUT_IMAGE_PATH) -> str:
    """
    Processes a vertical/square image into a 1080x1350 Feed post.
    """
    logger.info("Processing feed artwork image...")
    img = Image.open(raw_image_path)
    
    canvas_w = config.TARGET_WIDTH   # 1080
    canvas_h = config.TARGET_HEIGHT  # 1350

    bg = img.copy()
    bg_aspect = bg.width / bg.height
    target_aspect = canvas_w / canvas_h

    if bg_aspect > target_aspect:
        new_h = canvas_h
        new_w = int(canvas_h * bg_aspect)
    else:
        new_w = canvas_w
        new_h = int(canvas_w / bg_aspect)

    bg = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)
    crop_left = (new_w - canvas_w) // 2
    crop_top = (new_h - canvas_h) // 2
    bg = bg.crop((crop_left, crop_top, crop_left + canvas_w, crop_top + canvas_h))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=config.BLUR_RADIUS))

    dark_overlay = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    canvas = Image.blend(bg, dark_overlay, alpha=0.25)

    style = random.choice(FRAME_STYLES)
    logger.info(f"Applying frame style: '{style}'")

    border_size = random.randint(10, 20) if style in ("palette_border", "gradient_border") else 0

    padding = 50
    max_w = canvas_w - (padding + border_size) * 2
    max_h = canvas_h - (padding + border_size) * 2

    paint_ratio = img.width / img.height
    if paint_ratio > max_w / max_h:
        paint_w = max_w
        paint_h = int(paint_w / paint_ratio)
    else:
        paint_h = max_h
        paint_w = int(paint_h * paint_ratio)

    painting = img.resize((paint_w, paint_h), Image.Resampling.LANCZOS)

    if style == "palette_border":
        painting = _apply_palette_border(painting, border_size=border_size)
    elif style == "gradient_border":
        painting = _apply_gradient_border(painting, border_size=border_size)
    elif style == "clean":
        painting = ImageOps.expand(painting, border=6, fill=(255, 255, 255))

    paste_x = (canvas_w - painting.width) // 2
    paste_y = (canvas_h - painting.height) // 2
    canvas.paste(painting, (paste_x, paste_y))

    canvas.save(output_path, "JPEG", quality=95)
    logger.info(f"Feed post image processed and saved to: {output_path}")
    return output_path


def download_random_audio(output_path: str = config.TEMP_AUDIO_PATH) -> Optional[str]:
    """Downloads a random classical music track from PUBLIC_AUDIO_URLS."""
    urls = list(config.PUBLIC_AUDIO_URLS)
    random.shuffle(urls)
    
    for url in urls:
        logger.info(f"Attempting to download audio from: {url}")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            }
            res = requests.get(url, headers=headers, timeout=30)
            res.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(res.content)
            logger.info("Audio download successful.")
            return output_path
        except Exception as e:
            logger.warning(f"Failed to download audio from {url}: {e}")
            continue
            
    logger.error("Could not download any audio files.")
    return None


def create_reels_video(raw_image_path: str, output_path: str = config.OUTPUT_VIDEO_PATH) -> str:
    """
    Creates a 6-second vertical Reels video with zoom-in effect and dynamic audio.
    """
    logger.info("Creating Reels video (1080x1920) for horizontal image...")
    
    # 1. Prepare audio
    audio_path = download_random_audio()
    audio_clip = None
    if audio_path:
        try:
            audio_clip = mpy.AudioFileClip(audio_path)
            # Pick a random start point if the audio is long
            max_start = max(0, audio_clip.duration - config.REELS_DURATION)
            start_t = random.uniform(0, max_start) if max_start > 0 else 0
            audio_clip = audio_clip.subclipped(start_t, start_t + config.REELS_DURATION)
            audio_clip = audio_clip.with_effects([afx.FadeOut(1.5)])
        except Exception as e:
            logger.warning(f"Error processing audio: {e}")
            audio_clip = None
            
    # 2. Prepare video
    # Load image as clip
    clip = mpy.ImageClip(raw_image_path)
    
    # Resize to width 1080 (if ratio allows)
    clip = clip.resized(width=config.REELS_WIDTH)
    
    # Apply zoom fx (resize based on time)
    # 1.0 at t=0 to 1.15 at t=REELS_DURATION
    clip = clip.resized(lambda t: 1.0 + (0.15 * (t / config.REELS_DURATION)))

    # Create a black background ColorClip
    bg_clip = mpy.ColorClip(size=(config.REELS_WIDTH, config.REELS_HEIGHT), color=(0, 0, 0), duration=config.REELS_DURATION)
    
    # Overlay the zoomed image on the background
    clip = mpy.CompositeVideoClip([bg_clip, clip.with_position("center")])
    
    clip = clip.with_duration(config.REELS_DURATION)
    
    if audio_clip:
        clip = clip.with_audio(audio_clip)

    clip.write_videofile(
        output_path, 
        fps=config.REELS_FPS, 
        codec="libx264", 
        audio_codec="aac",
        logger=None # Suppress moviepy progress bar in logs
    )
    
    # Close clips
    clip.close()
    if audio_clip:
        audio_clip.close()
        
    logger.info(f"Reels video successfully created: {output_path}")
    return output_path


def upload_temp_media(file_path: str, media_type: str = "image") -> str:
    """
    Uploads processed image or video to a public HTTPS host that returns a DIRECT URL.
    media_type: 'image' or 'video'
    """
    logger.info(f"Uploading {media_type} to public HTTPS host...")
    mime_type = "video/mp4" if media_type == "video" else "image/jpeg"
    file_ext = "mp4" if media_type == "video" else "jpg"
    filename = f"artwork.{file_ext}"

    # Method 1: catbox.moe
    try:
        logger.info("Trying catbox.moe...")
        with open(file_path, "rb") as f:
            res = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (filename, f, mime_type)},
                timeout=120
            )
        if res.status_code == 200 and res.text.strip().startswith("http"):
            url = res.text.strip()
            logger.info(f"catbox.moe upload successful: {url}")
            return url
    except Exception as e:
        logger.warning(f"catbox.moe upload failed: {e}")

    # Method 2: freeimage.host (Images only)
    if media_type == "image":
        try:
            logger.info("Trying freeimage.host...")
            import base64
            with open(file_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
            res = requests.post(
                "https://freeimage.host/api/1/upload",
                data={
                    "key": "6d207e02198a847aa98d0a2a901485a5",
                    "action": "upload",
                    "source": image_data,
                    "format": "json"
                },
                timeout=60
            )
            if res.status_code == 200:
                data = res.json()
                url = data.get("image", {}).get("url")
                if url:
                    logger.info(f"freeimage.host upload successful: {url}")
                    return url
        except Exception as e:
            logger.warning(f"freeimage.host upload failed: {e}")

    # Method 3: litterbox.catbox.moe (1 hour retention, accepts up to 1GB)
    try:
        logger.info("Trying litterbox.catbox.moe...")
        with open(file_path, "rb") as f:
            res = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": (filename, f, mime_type)},
                timeout=120
            )
        if res.status_code == 200 and res.text.strip().startswith("http"):
            url = res.text.strip()
            logger.info(f"litterbox upload successful: {url}")
            return url
    except Exception as e:
        logger.warning(f"litterbox upload failed: {e}")

    raise RuntimeError("Could not upload media to any public host!")
