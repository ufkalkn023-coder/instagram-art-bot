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


def get_audio_tracks() -> List[Dict[str, Any]]:
    """
    Fetches the list of available audio tracks dynamically from R2.
    Falls back to local assets/audio/ if R2 is unavailable.
    Returns a list of dicts: {"title": "track_name", "key": "s3_key_or_local_path", "source": "r2|local"}
    """
    tracks = []
    valid_exts = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac")
    
    # 1. Try R2 First
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID")
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME")
    
    if account_id and access_key and secret_key and bucket:
        try:
            import boto3
            client = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name="auto"
            )
            
            paginator = client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix="audio/")
            
            for page in pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        key = obj['Key']
                        if key.lower().endswith(valid_exts) and obj['Size'] > 0:
                            # Use filename as title without extension
                            title = os.path.splitext(os.path.basename(key))[0]
                            tracks.append({"title": title, "key": key, "source": "r2"})
            
            if tracks:
                logger.info(f"Retrieved {len(tracks)} audio tracks from R2 bucket '{bucket}'.")
                # Sort alphabetically for determinism
                tracks.sort(key=lambda x: x["title"])
                return tracks
        except Exception as e:
            logger.warning(f"Failed to list R2 audio tracks: {e}. Falling back to local.")
            
    # 2. Local Fallback
    audio_dir = getattr(config, "AUDIO_DIR", os.path.join(config.BASE_DIR, "assets", "audio"))
    if os.path.isdir(audio_dir):
        local_files = [
            f for f in os.listdir(audio_dir)
            if f.lower().endswith(valid_exts)
        ]
        if local_files:
            logger.info(f"Found {len(local_files)} local audio file(s) as fallback.")
            for f in sorted(local_files):
                title = os.path.splitext(f)[0]
                tracks.append({"title": title, "key": os.path.join(audio_dir, f), "source": "local"})
            return tracks
            
    logger.warning("No audio tracks found in R2 or local fallback.")
    return []


def download_audio(output_path: str = config.TEMP_AUDIO_PATH, track_index: Optional[int] = None) -> Tuple[Optional[str], Optional[dict]]:
    """
    Downloads or copies the selected audio track based on the provided index.
    Raises RuntimeError if all available tracks fail to process.
    """
    import shutil
    import moviepy as mpy
    
    tracks = get_audio_tracks()
    if not tracks:
        raise RuntimeError("All audio sources failed: No audio tracks available.")
        
    # Reorder tracks to put the chosen one first, but keep others as fallbacks
    ordered_tracks = list(tracks)
    if track_index is not None and 0 <= track_index < len(tracks):
        chosen = ordered_tracks.pop(track_index)
        ordered_tracks.insert(0, chosen)
        
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID")
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME")
    s3_client = None
    
    if account_id and access_key and secret_key and bucket:
        import boto3
        s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto"
        )
        
    for track in ordered_tracks:
        logger.info(f"Attempting to process audio track: {track['title']} (Source: {track['source']})")
        
        try:
            if track["source"] == "r2":
                if not s3_client:
                    logger.warning("R2 source selected but credentials missing.")
                    continue
                    
                s3_client.download_file(bucket, track["key"], output_path)
                logger.info(f"Successfully downloaded {track['key']} from R2.")
                
            elif track["source"] == "local":
                shutil.copyfile(track["key"], output_path)
                logger.info(f"Successfully copied local file {track['key']}.")
                
            # Validate with MoviePy
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise ValueError("Audio file is empty or missing.")
                
            clip = mpy.AudioFileClip(output_path)
            if clip.duration <= 0:
                clip.close()
                raise ValueError("Audio file duration is zero or invalid.")
                
            clip.close()
            logger.info("Audio validation successful.")
            
            track_info = {
                "title": track["title"],
                "artist": "Classical Music Library",
                "url": track["key"],
                "drop_start": 0.0
            }
            return output_path, track_info
            
        except Exception as e:
            logger.warning(f"Failed to process track {track['title']}: {e}")
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            continue
            
    raise RuntimeError("All audio sources failed to process successfully.")


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
