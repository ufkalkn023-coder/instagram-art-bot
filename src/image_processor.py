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


def download_audio(output_path: str = config.TEMP_AUDIO_PATH, track_index: Optional[int] = None) -> Tuple[Optional[str], Optional[dict]]:
    """
    Retrieves audio for Reels. First checks config.AUDIO_DIR for local audio files.
    If none exist or loading fails, falls back to downloading online classical tracks.
    """
    import shutil
    
    # 1. Check local audio files in assets/audio/
    audio_dir = getattr(config, "AUDIO_DIR", os.path.join(config.BASE_DIR, "assets", "audio"))
    if os.path.isdir(audio_dir):
        valid_exts = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac")
        local_files = [
            os.path.join(audio_dir, f) for f in os.listdir(audio_dir)
            if f.lower().endswith(valid_exts)
        ]
        local_files.sort()  # Sort alphabetically for deterministic index selection
        
        if local_files:
            logger.info(f"Found {len(local_files)} local audio file(s) in {audio_dir}.")
            # Shuffle or pick by index
            if track_index is not None and 0 <= track_index < len(local_files):
                chosen_file = local_files[track_index]
            else:
                chosen_file = random.choice(local_files)
                
            filename = os.path.basename(chosen_file)
            logger.info(f"Using local audio file: {filename}")
            
            try:
                # Copy to output path if necessary, or check clip
                clip = mpy.AudioFileClip(chosen_file)
                if clip.duration > 0:
                    clip.close()
                    shutil.copyfile(chosen_file, output_path)
                    track_info = {
                        "title": os.path.splitext(filename)[0],
                        "artist": "Local Music Library",
                        "url": chosen_file,
                        "drop_start": 0.0
                    }
                    logger.info("Local audio validation successful.")
                    return output_path, track_info
                clip.close()
            except Exception as e:
                logger.warning(f"Failed to use local audio {chosen_file}: {e}. Falling back to online sources.")

    # 2. Online download fallback
    tracks = list(config.PUBLIC_AUDIO_TRACKS)
    
    # Put the selected track first if valid
    if track_index is not None and 0 <= track_index < len(tracks):
        selected_track = tracks.pop(track_index)
        random.shuffle(tracks)
        tracks.insert(0, selected_track)
    else:
        random.shuffle(tracks)
    
    import time
    for track in tracks:
        url = track["url"]
        logger.info(f"Attempting to download audio from: {url} ({track.get('title', 'Unknown')})")
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                res = requests.get(url, headers=headers, timeout=15)
                
                if res.status_code == 429:
                    retry_after = res.headers.get("Retry-After")
                    sleep_time = int(retry_after) if retry_after else (2 ** attempt)
                    logger.warning(f"HTTP 429 Too Many Requests for audio. Retrying in {sleep_time}s (Attempt {attempt+1}/{max_retries})...")
                    time.sleep(sleep_time)
                    continue
                    
                res.raise_for_status()
                
                content_type = res.headers.get("Content-Type", "")
                if "audio" not in content_type and "ogg" not in content_type and "mpeg" not in content_type:
                    logger.warning(f"Invalid Content-Type {content_type} for audio track: {url}")
                    break # Skip to next track
                    
                if len(res.content) == 0:
                    logger.warning(f"Downloaded audio file is empty: {url}")
                    break
                    
                with open(output_path, "wb") as f:
                    f.write(res.content)
                
                # Validate with MoviePy
                try:
                    clip = mpy.AudioFileClip(output_path)
                    if clip.duration <= 0:
                        raise ValueError("Audio duration is <= 0")
                    clip.close()
                except Exception as e:
                    logger.warning(f"MoviePy failed to read downloaded audio {url}: {e}")
                    break # Skip to next track
                
                logger.info("Audio download and validation successful.")
                return output_path, track
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"Network error downloading audio from {url}: {e}")
                if attempt == max_retries - 1:
                    break
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.warning(f"Unexpected error for {url}: {e}")
                break
                
    raise RuntimeError("All audio sources (local and online) failed. Cannot proceed with silent video.")


def create_reels_video(raw_image_path: str, output_path: str = config.OUTPUT_VIDEO_PATH, track_index: Optional[int] = None) -> str:
    """
    Creates a 15-second vertical Reels video with zoom-in effect and dynamic audio.
    """
    logger.info("Creating Reels video (1080x1920) for horizontal image...")
    
    # 1. Prepare audio
    audio_path, track_info = download_audio(track_index=track_index)
    if not audio_path or not track_info:
        raise RuntimeError("Audio generation failed unexpectedly.")
        
    try:
        audio_clip = mpy.AudioFileClip(audio_path)
        audio_duration = audio_clip.duration
        start_t = track_info.get("drop_start", 0.0)
        
        if start_t + config.REELS_DURATION > audio_duration:
            start_t = max(0, audio_duration - config.REELS_DURATION)
            
        end_t = min(start_t + config.REELS_DURATION, audio_duration)
        
        audio_clip = audio_clip.subclipped(start_t, end_t)
        # Add audio fade out
        audio_clip = audio_clip.with_effects([afx.AudioFadeOut(1.5)])
    except Exception as e:
        logger.error(f"Error processing audio clipping: {e}")
        raise RuntimeError("Failed to prepare audio clip.") from e

            
    # 2. Prepare video
    base_clip = mpy.ImageClip(raw_image_path).resized(width=config.REELS_WIDTH)
    
    # Clip 1: 0 - 3 seconds (Slow zoom in from 1.0 to 1.05)
    clip1 = base_clip.resized(lambda t: 1.0 + (0.05 * (t / 3.0))).with_duration(3.0)
    
    # Clip 2: 3 - 15 seconds (Close up zoom in from 1.5 to 1.7)
    clip2 = base_clip.resized(lambda t: 1.5 + (0.2 * (t / 12.0))).with_duration(12.0)
    
    # Concatenate the clips
    combined_clip = mpy.concatenate_videoclips([clip1, clip2])
    
    # Create a black background ColorClip
    bg_clip = mpy.ColorClip(size=(config.REELS_WIDTH, config.REELS_HEIGHT), color=(0, 0, 0), duration=config.REELS_DURATION)
    
    # Overlay the zoomed image on the background
    final_video = mpy.CompositeVideoClip([bg_clip, combined_clip.with_position("center")])
    
    final_video = final_video.with_duration(config.REELS_DURATION)
    final_video = final_video.with_audio(audio_clip)

    final_video.write_videofile(
        output_path, 
        fps=config.REELS_FPS, 
        codec="libx264", 
        audio_codec="aac",
        logger=None # Suppress moviepy progress bar in logs
    )
    
    base_clip.close()
    clip1.close()
    clip2.close()
    combined_clip.close()
    bg_clip.close()
    final_video.close()
    if audio_clip:
        audio_clip.close()
    else:
        logger.warning("⚠️ Video has no audio! This will perform poorly on Instagram.")
        
    logger.info(f"Reels video successfully created: {output_path}")
    return output_path


def upload_temp_media(file_path: str, media_type: str = "image") -> str:
    """
    Uploads processed image or video to Cloudflare R2.
    media_type: 'image' or 'video'
    Returns the public HTTP URL of the uploaded file.
    """
    logger.info(f"Uploading {media_type} to Cloudflare R2...")
    
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
    
    if media_type == "video":
        content_type = "video/mp4"
        object_key = f"videos/{timestamp}_{unique_id}.mp4"
    else:
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
