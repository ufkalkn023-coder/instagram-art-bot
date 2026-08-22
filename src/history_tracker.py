import json
import os
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
import boto3
from botocore.exceptions import ClientError
from typing import Iterable, Set, Dict, Any, Tuple
from src.models import normalize_artwork_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HISTORY_OBJECT_KEY = "posted_history.json"
# Must exceed the workflow's 20-minute hard timeout and normal publish duration.
PENDING_RESERVATION_TTL = timedelta(hours=2)

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


def _parse_reserved_at(value: Any) -> datetime | None:
    """Parse an aware UTC reservation timestamp; return None for unsafe values."""
    if not isinstance(value, str) or not value:
        return None

    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    return timestamp.astimezone(timezone.utc)


def _is_stale_pending(item: Dict[str, Any], now: datetime) -> bool:
    """Return True only when a PENDING record is safely provably stale."""
    if str(item.get("status", "")).upper() != "PENDING":
        return False

    reserved_at = _parse_reserved_at(item.get("reserved_at"))
    if reserved_at is None:
        return False

    return now.astimezone(timezone.utc) - reserved_at >= PENDING_RESERVATION_TTL


def recover_stale_reservations(now: datetime | None = None) -> int:
    """Mark safely identifiable stale PENDING records as EXPIRED.

    The history is only written when a record changes, and the existing ETag
    conditional write protects recovery from concurrent reservations.
    """
    history, etag = load_history_with_etag()
    recovery_time = now or datetime.now(timezone.utc)
    if recovery_time.tzinfo is None or recovery_time.utcoffset() is None:
        raise ValueError("Recovery time must be timezone-aware")
    recovery_time = recovery_time.astimezone(timezone.utc)

    recovered = 0
    for item in history.get("posted_artworks", []):
        if _is_stale_pending(item, recovery_time):
            item["status"] = "EXPIRED"
            item["expired_at"] = recovery_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            recovered += 1

    if recovered:
        _upload_history(history, etag)
        logger.info(f"Marked {recovered} stale reservation(s) as EXPIRED.")

    return recovered

def get_posted_ids() -> Set[str]:
    """Return IDs protected from reuse by published, pending, or ambiguous posts."""
    history, _ = load_history_with_etag()
    posted_list = history.get("posted_artworks", [])
    now = datetime.now(timezone.utc)
    posted_ids = set()
    for item in posted_list:
        artwork_id = item.get("id")
        if not isinstance(artwork_id, str):
            continue

        status = str(item.get("status", "")).upper()
        if status == "EXPIRED":
            continue
        if status == "PENDING" and _is_stale_pending(item, now):
            continue

        # AMBIGUOUS is intentionally a permanent duplicate lock. Instagram may
        # have accepted the publish request even when the client could not prove it.
        posted_ids.add(normalize_artwork_id(artwork_id))

    return posted_ids

def get_recent_history() -> list:
    """Return confirmed published history for diversity decisions.

    AMBIGUOUS records remain duplicate locks but are excluded here because the
    Instagram publish result was not proven successful.
    """
    history, _ = load_history_with_etag()
    now = datetime.now(timezone.utc)
    return [
        item
        for item in history.get("posted_artworks", [])
        if not _is_stale_pending(item, now)
        and str(item.get("status", "")).upper() not in {"EXPIRED", "AMBIGUOUS"}
    ]

def reserve_artwork(artwork_data: Dict[str, Any]):
    """
    PRE-WRITE: Adds artwork to history with PENDING status and uploads to R2.
    If this fails (e.g. Conditional Write 412), it throws an exception and 
    the bot aborts BEFORE posting to Instagram.
    """
    history, etag = load_history_with_etag()
    
    # Remove if it was pending before (to avoid duplicates in array if it somehow was reserved before)
    artwork_id = normalize_artwork_id(artwork_data["id"])
    history["posted_artworks"] = [
        item for item in history.get("posted_artworks", [])
        if normalize_artwork_id(item.get("id", "")) != artwork_id
    ]
    
    record = {
        "id": artwork_id,
        "title": artwork_data.get("title"),
        "artist": artwork_data.get("artist"),
        "museum_name": artwork_data.get("museum"), # mapped to museum_name for consistency
        "artist_name": artwork_data.get("artist"), # mapped to artist_name for consistency
        "visual_category": artwork_data.get("visual_category", "other"),
        "medium": artwork_data.get("medium", "other"),
        "period": artwork_data.get("period", "unknown"),
        "quality_score": artwork_data.get("quality_score"),
        "measurement_coverage": artwork_data.get("measurement_coverage"),
        "selection_score": artwork_data.get("selection_score"),
        "image_width": artwork_data.get("image_width"),
        "image_height": artwork_data.get("image_height"),
        "content_type": artwork_data.get("content_type", "SINGLE_ARTWORK"),
        "status": "PENDING",
        "media_id": None,
        "reservation_id": str(uuid.uuid4()),
        "reserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    
    history["posted_artworks"].append(record)
    _upload_history(history, etag)
    logger.info(f"Reserved artwork {artwork_id} in R2 history (PENDING).")


def mark_artworks_ambiguous(artwork_ids: Iterable[str]) -> int:
    """Mark current reservations as AMBIGUOUS after an unprovable publish result.

    AMBIGUOUS records are intentionally never expired by reservation recovery,
    so they continue to prevent a possible duplicate Instagram publication.
    """
    canonical_ids = {normalize_artwork_id(artwork_id) for artwork_id in artwork_ids}
    if not canonical_ids:
        return 0

    history, etag = load_history_with_etag()
    records_by_id = {}
    for item in history.get("posted_artworks", []):
        artwork_id = item.get("id")
        if isinstance(artwork_id, str):
            records_by_id.setdefault(normalize_artwork_id(artwork_id), []).append(item)

    missing_ids = canonical_ids.difference(records_by_id)
    if missing_ids:
        raise RuntimeError(
            "Could not find reservation(s) to mark AMBIGUOUS: "
            + ", ".join(sorted(missing_ids))
        )

    ambiguous_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    for artwork_id in canonical_ids:
        for item in records_by_id[artwork_id]:
            status = str(item.get("status", "")).upper()
            if status in {"PENDING", "PUBLISHING"}:
                item["status"] = "AMBIGUOUS"
                item["ambiguous_at"] = ambiguous_at
                updated += 1
            elif status not in {"AMBIGUOUS", "PUBLISHED"}:
                raise RuntimeError(
                    f"Cannot mark {artwork_id} AMBIGUOUS from {status or 'missing'} status"
                )

    if updated:
        try:
            _upload_history(history, etag)
        except Exception:
            for artwork_id in canonical_ids:
                for item in records_by_id[artwork_id]:
                    if str(item.get("status", "")).upper() == "AMBIGUOUS":
                        item["status"] = "PUBLISHING"
                        item.pop("ambiguous_at", None)
            raise
        logger.error(
            "Marked %s reservation(s) as AMBIGUOUS after an uncertain Instagram publish result.",
            updated,
        )

    return updated


def mark_artwork_ambiguous(artwork_id: str) -> int:
    """Mark one reserved artwork AMBIGUOUS; see mark_artworks_ambiguous."""
    return mark_artworks_ambiguous([artwork_id])


def mark_artworks_publishing(artwork_ids: Iterable[str]) -> int:
    """Durably transition reservations to the non-expiring publish lock.

    This is the last history write before Instagram's publish boundary.  The
    complete batch is validated before one conditional R2 write, so callers
    must not publish if this function raises.
    """
    canonical_ids = {normalize_artwork_id(artwork_id) for artwork_id in artwork_ids}
    if not canonical_ids:
        return 0

    history, etag = load_history_with_etag()
    records_by_id = {}
    for item in history.get("posted_artworks", []):
        artwork_id = item.get("id")
        if isinstance(artwork_id, str):
            records_by_id.setdefault(normalize_artwork_id(artwork_id), []).append(item)

    missing_ids = canonical_ids.difference(records_by_id)
    if missing_ids:
        raise RuntimeError(
            "Could not find reservation(s) to mark PUBLISHING: "
            + ", ".join(sorted(missing_ids))
        )

    invalid = {
        artwork_id: str(item.get("status", "")).upper() or "missing"
        for artwork_id in canonical_ids
        for item in records_by_id[artwork_id]
        if str(item.get("status", "")).upper() != "PENDING"
    }
    if invalid:
        details = ", ".join(f"{artwork_id}={status}" for artwork_id, status in sorted(invalid.items()))
        raise RuntimeError(f"Cannot mark reservation(s) PUBLISHING: {details}")

    publishing_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for artwork_id in canonical_ids:
        for item in records_by_id[artwork_id]:
            item["status"] = "PUBLISHING"
            item["publishing_at"] = publishing_at

    try:
        _upload_history(history, etag)
    except Exception:
        for artwork_id in canonical_ids:
            for item in records_by_id[artwork_id]:
                item["status"] = "PENDING"
                item.pop("publishing_at", None)
        raise
    logger.info("Marked %s reservation(s) PUBLISHING in R2 before Instagram publish.", len(canonical_ids))
    return len(canonical_ids)


def mark_artworks_pending(artwork_ids: Iterable[str]) -> int:
    """Best-effort rollback after a definite publish failure.

    If this write fails, the durable PUBLISHING lock intentionally remains in
    R2; callers must never treat the local mutation as authoritative.
    """
    canonical_ids = {normalize_artwork_id(artwork_id) for artwork_id in artwork_ids}
    if not canonical_ids:
        return 0

    history, etag = load_history_with_etag()
    records_by_id = {}
    for item in history.get("posted_artworks", []):
        artwork_id = item.get("id")
        if isinstance(artwork_id, str):
            records_by_id.setdefault(normalize_artwork_id(artwork_id), []).append(item)

    missing_ids = canonical_ids.difference(records_by_id)
    if missing_ids:
        raise RuntimeError(
            "Could not find reservation(s) to mark PENDING: "
            + ", ".join(sorted(missing_ids))
        )

    updated = 0
    for artwork_id in canonical_ids:
        for item in records_by_id[artwork_id]:
            if str(item.get("status", "")).upper() == "PUBLISHING":
                item["status"] = "PENDING"
                item.pop("publishing_at", None)
                updated += 1

    if updated:
        try:
            _upload_history(history, etag)
        except Exception:
            for artwork_id in canonical_ids:
                for item in records_by_id[artwork_id]:
                    item["status"] = "PUBLISHING"
            raise
        logger.info("Rolled back %s reservation(s) to PENDING after definite publish failure.", updated)
    return updated

def confirm_artwork(artwork_id: str, media_id: str):
    """
    POST-WRITE: Updates the PUBLISHING artwork to PUBLISHED status in R2.
    """
    history, etag = load_history_with_etag()
    
    found = False
    found_item = None
    artwork_id = normalize_artwork_id(artwork_id)
    for item in history.get("posted_artworks", []):
        if normalize_artwork_id(item.get("id", "")) == artwork_id:
            item["status"] = "PUBLISHED"
            item["media_id"] = media_id
            item["posted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            found = True
            found_item = item
            break
            
    if found:
        try:
            _upload_history(history, etag)
        except Exception:
            found_item["status"] = "PUBLISHING"
            found_item["media_id"] = None
            found_item.pop("posted_at", None)
            raise
        logger.info(f"Confirmed artwork {artwork_id} in R2 history (PUBLISHED).")
    else:
        logger.warning(f"Could not find artwork {artwork_id} in history to confirm!")

def get_grid_color_tone(read_only: bool = False) -> str:
    """
    Determines the color tone for the current post to ensure Instagram grid harmony (3 items per row).
    If starting a new row (posts % 3 == 0), picks a new color. Otherwise, uses the active color.
    ``read_only`` returns the persisted tone without creating a new grid-row
    record, for callers that must not mutate history.
    """
    history, etag = load_history_with_etag()
    posted_count = len(history.get("posted_artworks", []))
    
    predefined_tones = ["red", "blue", "green", "yellow", "purple", "brown", "monochrome", "warm", "cool"]
    
    current_tone = history.get("active_color_tone", "warm")
    
    if posted_count % 3 == 0 or not history.get("active_color_tone"):
        if read_only:
            logger.info("Read-only grid tone lookup. Using persisted tone: %s", current_tone)
            return current_tone
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
