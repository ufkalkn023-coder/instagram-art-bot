import json
import os
import time
import uuid
import logging
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from typing import Set, Dict, Any, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HISTORY_OBJECT_KEY = "posted_history.json"

class ConcurrentWriteError(Exception):
    """Raised when R2 conditional write (If-Match) fails due to concurrent modification."""
    pass

class CorruptedHistoryError(Exception):
    """Raised when R2 history JSON is malformed."""
    pass

def _get_s3_client():
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
    
    if not all([account_id, access_key, secret_key]):
        raise ValueError("Missing CLOUDFLARE_R2 credentials in environment!")
        
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto"
    )

def _get_bucket_name() -> str:
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
    if not bucket:
        raise ValueError("Missing CLOUDFLARE_R2_BUCKET_NAME")
    return bucket

def load_history_with_etag() -> Tuple[Dict[str, Any], str]:
    """Loads posted history JSON from Cloudflare R2 and returns (data, etag)."""
    try:
        s3 = _get_s3_client()
        bucket = _get_bucket_name()
        logger.info(f"Downloading {HISTORY_OBJECT_KEY} from R2...")
        
        response = s3.get_object(Bucket=bucket, Key=HISTORY_OBJECT_KEY)
        content = response['Body'].read().decode('utf-8')
        etag = response.get('ETag', '').strip('"')
        
        try:
            data = json.loads(content)
            return data, etag
        except json.JSONDecodeError as e:
            logger.error(f"Malformed history JSON in R2: {e}")
            raise CorruptedHistoryError("Corrupted history.json in R2") from e
            
    except ValueError as e:
        logger.warning(f"R2 credentials not found, assuming local/dry-run mode: {e}")
        return {"posted_artworks": []}, None
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logger.info("History file not found in R2, starting fresh.")
            return {"posted_artworks": []}, None
        else:
            logger.error(f"Error fetching history from R2: {e}")
            raise
    except CorruptedHistoryError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error reading history file ({e}). Failing closed.")
        raise

def _upload_history(history: Dict[str, Any], etag: str = None):
    """Uploads the history dict back to Cloudflare R2 using Conditional Write if etag provided."""
    s3 = _get_s3_client()
    bucket = _get_bucket_name()
    content = json.dumps(history, ensure_ascii=False, indent=2)
    
    kwargs = {
        "Bucket": bucket,
        "Key": HISTORY_OBJECT_KEY,
        "Body": content.encode('utf-8'),
        "ContentType": "application/json"
    }
    
    if etag:
        # Boto3/S3 standard for conditional write
        kwargs["IfMatch"] = etag
    
    logger.info(f"Uploading {HISTORY_OBJECT_KEY} to R2 (ETag: {etag})...")
    
    try:
        s3.put_object(**kwargs)
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code in ['PreconditionFailed', '412']:
            logger.error(f"Concurrent write detected! R2 ETag {etag} rejected.")
            raise ConcurrentWriteError(f"Conditional write failed for ETag {etag}") from e
        raise

def get_posted_ids() -> Set[str]:
    """Returns a set of artwork IDs that have already been posted or are pending."""
    history, _ = load_history_with_etag()
    posted_list = history.get("posted_artworks", [])
    # We consider both PUBLISHED and PENDING as "posted" so we don't pick them again
    return {item["id"] for item in posted_list if "id" in item}

def get_recent_history() -> list:
    """Returns the list of recently posted artworks."""
    history, _ = load_history_with_etag()
    return history.get("posted_artworks", [])

def reserve_artwork(artwork_data: Dict[str, Any]):
    """
    PRE-WRITE: Adds artwork to history with PENDING status and uploads to R2.
    If this fails (e.g. Conditional Write 412), it throws an exception and 
    the bot aborts BEFORE posting to Instagram.
    """
    history, etag = load_history_with_etag()
    
    # Remove if it was pending before (to avoid duplicates in array if it somehow was reserved before)
    history["posted_artworks"] = [
        item for item in history.get("posted_artworks", []) 
        if item.get("id") != artwork_data["id"]
    ]
    
    record = {
        "id": artwork_data["id"],
        "title": artwork_data.get("title"),
        "artist": artwork_data.get("artist"),
        "museum_name": artwork_data.get("museum"), # mapped to museum_name for consistency
        "artist_name": artwork_data.get("artist"), # mapped to artist_name for consistency
        "visual_category": artwork_data.get("visual_category", "other"),
        "medium": artwork_data.get("medium", "other"),
        "period": artwork_data.get("period", "unknown"),
        "content_type": artwork_data.get("content_type", "SINGLE_ARTWORK"),
        "status": "PENDING",
        "media_id": None,
        "reservation_id": str(uuid.uuid4()),
        "reserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    
    history["posted_artworks"].append(record)
    _upload_history(history, etag)
    logger.info(f"Reserved artwork {artwork_data['id']} in R2 history (PENDING).")

def confirm_artwork(artwork_id: str, media_id: str):
    """
    POST-WRITE: Updates the PENDING artwork to PUBLISHED status in R2.
    """
    history, etag = load_history_with_etag()
    
    found = False
    for item in history.get("posted_artworks", []):
        if item.get("id") == artwork_id:
            item["status"] = "PUBLISHED"
            item["media_id"] = media_id
            item["posted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            found = True
            break
            
    if found:
        _upload_history(history, etag)
        logger.info(f"Confirmed artwork {artwork_id} in R2 history (PUBLISHED).")
    else:
        logger.warning(f"Could not find artwork {artwork_id} in history to confirm!")

def get_grid_color_tone() -> str:
    """
    Determines the color tone for the current post to ensure Instagram grid harmony (3 items per row).
    If starting a new row (posts % 3 == 0), picks a new color. Otherwise, uses the active color.
    """
    history, etag = load_history_with_etag()
    posted_count = len(history.get("posted_artworks", []))
    
    predefined_tones = ["red", "blue", "green", "yellow", "purple", "brown", "monochrome", "warm", "cool"]
    
    current_tone = history.get("active_color_tone", "warm")
    
    if posted_count % 3 == 0 or not history.get("active_color_tone"):
        import random
        # Pick a new tone that is different from the current one
        available_tones = [t for t in predefined_tones if t != current_tone]
        new_tone = random.choice(available_tones)
        history["active_color_tone"] = new_tone
        _upload_history(history, etag)
        logger.info(f"Started new Grid Row. Selected new color tone: {new_tone}")
        return new_tone
        
    logger.info(f"Continuing Grid Row. Using active color tone: {current_tone}")
    return current_tone
