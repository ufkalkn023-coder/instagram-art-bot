import logging
import requests
import io
from PIL import Image
from src.models import NormalizedArtwork
import config

logger = logging.getLogger(__name__)

def validate_and_download_image(url: str, output_path: str) -> bool:
    """
    Downloads the entire image to output_path and verifies it with Pillow.
    HARD REJECT rules applied here (403, 404, corrupted, invalid type).
    """
    if not url or not url.startswith("http"):
        return False
        
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(url, headers=headers, stream=True, timeout=30)
        
        if res.status_code != 200:
            logger.warning(f"Image validation failed: HTTP {res.status_code} for {url}")
            return False
            
        content_type = res.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            logger.warning(f"Image validation failed: Invalid Content-Type {content_type}")
            return False
            
        # Download the entire file
        with open(output_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
            
        # Verify with Pillow
        try:
            img = Image.open(output_path)
            img.verify()
            
            # Additional check: minimum dimensions
            img = Image.open(output_path)
            if img.width < 100 or img.height < 100:
                logger.warning(f"Image validation failed: Image too small ({img.width}x{img.height})")
                return False
                
            return True
        except Exception as e:
            logger.warning(f"Image validation failed: Pillow could not verify image: {e}")
            return False
            
    except Exception as e:
        logger.error(f"Image validation error: {e}")
        return False

def calculate_quality_score(artwork: NormalizedArtwork, museum_weights: dict) -> int:
    """
    Calculates a deterministic 0-100 score based on metadata and source.
    """
    score = 0
    
    # 1. Image Quality / Resolution Info (Max 40)
    # If dimensions are not provided by the API, we give a neutral 30/40.
    if artwork.image_width and artwork.image_height:
        if max(artwork.image_width, artwork.image_height) >= 1080:
            score += 40
        elif max(artwork.image_width, artwork.image_height) >= 800:
            score += 30
        else:
            score += 15 # Soft penalty for low res
    else:
        score += 30 # Neutral fallback when dimensions are unknown
        
    # 2. Metadata Completeness (Max 25)
    meta_score = 25
    if not artwork.title or artwork.title.lower() == "untitled":
        meta_score -= 5
    if not artwork.artist_name or "unknown" in artwork.artist_name.lower():
        meta_score -= 10
    if not artwork.creation_date or "unknown" in artwork.creation_date.lower():
        meta_score -= 5
    if not artwork.medium:
        meta_score -= 5
    score += max(0, meta_score)
    
    # 3. Source Confidence (Max 15)
    # Uses the configured weights (defaults to 15 for all to keep them equal unless configured otherwise)
    source_weight = museum_weights.get(artwork.source, 15)
    score += min(15, source_weight)
    
    # 4. Instagram Suitability / Aspect Ratio (Max 20)
    if artwork.image_width and artwork.image_height:
        ratio = artwork.image_width / artwork.image_height
        if 0.5 <= ratio <= 1.0:
            score += 20 # Perfect for vertical Reels / Square
        elif 1.0 < ratio <= 1.5:
            score += 15 # Standard landscape, very good
        else:
            score += 10 # Extreme landscape or very tall, soft penalty
    else:
        score += 15 # Neutral fallback
        
    return min(100, max(0, score))
