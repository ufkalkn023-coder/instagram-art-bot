import copy
import json
import os
import random
import uuid
import logging
from datetime import datetime, timedelta, timezone
import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError
from typing import Iterable, Set, Dict, Any, Tuple
from src.models import PublicationRecord, normalize_artwork_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HISTORY_OBJECT_KEY = "posted_history.json"
# Must exceed the workflow's 20-minute hard timeout and normal publish duration.
PENDING_RESERVATION_TTL = timedelta(hours=2)
GRID_COLOR_TONES = ["red", "blue", "green", "yellow", "purple", "brown", "monochrome", "warm", "cool"]
PUBLICATION_TYPES = {"single", "carousel"}

class ConcurrentWriteError(Exception):
    """Raised when R2 conditional write (If-Match) fails due to concurrent modification."""
    pass

class CorruptedHistoryError(Exception):
    """Raised when R2 history JSON is malformed."""
    pass


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validated_publications(history: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return validated forward-only publication records; absent means legacy."""
    if "publications" not in history:
        return []

    publications = history["publications"]
    if not isinstance(publications, list):
        raise CorruptedHistoryError("History publications must be a list")

    seen_ids = set()
    seen_media_ids = set()
    for index, publication in enumerate(publications):
        if not isinstance(publication, dict):
            raise CorruptedHistoryError(f"History publication at index {index} must be an object")
        try:
            validated = PublicationRecord.model_validate(publication)
        except ValidationError as exc:
            raise CorruptedHistoryError(f"Malformed history publication at index {index}") from exc
        if validated.id in seen_ids:
            raise CorruptedHistoryError(f"Duplicate publication ID: {validated.id}")
        if validated.media_id in seen_media_ids:
            raise CorruptedHistoryError(f"Duplicate publication media ID: {validated.media_id}")
        seen_ids.add(validated.id)
        seen_media_ids.add(validated.media_id)
    return publications


def _grid_publication_count(history: Dict[str, Any], publications: list[Dict[str, Any]]) -> int:
    """Read the forward-only grid counter without inferring legacy publications."""
    if "grid_publication_count" not in history:
        if publications:
            raise CorruptedHistoryError("Publication history is missing grid_publication_count")
        return 0

    count = history["grid_publication_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CorruptedHistoryError("grid_publication_count must be a non-negative integer")
    if count != len(publications):
        raise CorruptedHistoryError("grid_publication_count must equal the number of publications")
    return count

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


def _confirmed_artworks(history: Dict[str, Any]) -> list[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [
        item
        for item in history.get("posted_artworks", [])
        if isinstance(item, dict)
        and not _is_stale_pending(item, now)
        # Status-less records predate lifecycle tracking and are legacy
        # published history. Transient reservations must not create fatigue.
        and str(item.get("status", "")).upper() in {"", "PUBLISHED"}
    ]


def _ordered_confirmed_artworks(
    history: Dict[str, Any], publications: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Order forward records by publication, keeping ungrouped legacy records first."""
    confirmed = _confirmed_artworks(history)
    known_publication_ids = {publication["id"] for publication in publications}
    legacy_artworks = []
    artworks_by_publication: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for artwork in confirmed:
        publication_id = artwork.get("publication_id")
        if not isinstance(publication_id, str):
            legacy_artworks.append(artwork)
            continue
        if publication_id not in known_publication_ids:
            raise CorruptedHistoryError(
                f"Published artwork {artwork.get('id')} references an unknown publication"
            )
        artwork_id = artwork.get("id")
        if not isinstance(artwork_id, str):
            raise CorruptedHistoryError(f"Published artwork in {publication_id} has no string ID")
        canonical_id = normalize_artwork_id(artwork_id)
        publication_artworks = artworks_by_publication.setdefault(publication_id, {})
        if canonical_id in publication_artworks:
            raise CorruptedHistoryError(
                f"Publication {publication_id} has duplicate artwork {canonical_id}"
            )
        publication_artworks[canonical_id] = artwork

    ordered = list(legacy_artworks)
    for publication in publications:
        publication_id = publication["id"]
        publication_artworks = artworks_by_publication.pop(publication_id, {})
        expected_ids = [normalize_artwork_id(artwork_id) for artwork_id in publication["artwork_ids"]]
        if set(publication_artworks) != set(expected_ids):
            raise CorruptedHistoryError(
                f"Publication {publication_id} artwork links do not match posted_artworks"
            )
        for artwork_id in expected_ids:
            artwork = publication_artworks[artwork_id]
            if artwork.get("media_id") != publication["media_id"]:
                raise CorruptedHistoryError(
                    f"Publication {publication_id} media ID does not match artwork {artwork_id}"
                )
            ordered.append(artwork)

    if artworks_by_publication:
        raise CorruptedHistoryError("Published artworks reference unvalidated publications")
    return ordered


def get_recent_history() -> list:
    """Return confirmed published history for diversity decisions.

    AMBIGUOUS records remain duplicate locks but are excluded here because the
    Instagram publish result was not proven successful.
    """
    history, _ = load_history_with_etag()
    publications = _validated_publications(history)
    return _ordered_confirmed_artworks(history, publications)


def get_recent_publications(limit: int | None = None) -> list[Dict[str, Any]]:
    """Return proven forward-only publications; legacy history yields an empty list."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("Publication limit must be a non-negative integer or None")

    history, _ = load_history_with_etag()
    publications = _validated_publications(history)
    if limit is None:
        return list(publications)
    if limit == 0:
        return []
    return list(publications[-limit:])


def get_recent_artworks_by_publication(publication_limit: int) -> list[Dict[str, Any]]:
    """Flatten artworks from the last N publication slots without guessing legacy groups."""
    if isinstance(publication_limit, bool) or not isinstance(publication_limit, int) or publication_limit < 0:
        raise ValueError("Publication limit must be a non-negative integer")
    if publication_limit == 0:
        return []

    history, _ = load_history_with_etag()
    publications = _validated_publications(history)
    groups: list[list[Dict[str, Any]]] = []
    groups_by_publication_id: Dict[str, list[Dict[str, Any]]] = {}

    for artwork in _ordered_confirmed_artworks(history, publications):
        publication_id = artwork.get("publication_id")
        if isinstance(publication_id, str):
            group = groups_by_publication_id.get(publication_id)
            if group is None:
                group = []
                groups_by_publication_id[publication_id] = group
                groups.append(group)
            group.append(artwork)
        else:
            # Each ungrouped legacy artwork keeps its historical one-record slot.
            groups.append([artwork])

    return [artwork for group in groups[-publication_limit:] for artwork in group]


def _reservation_record(
    artwork_data: Dict[str, Any], publication_id: str, publication_type: str, reserved_at: str
) -> Dict[str, Any]:
    artwork_id = normalize_artwork_id(artwork_data["id"])
    return {
        "id": artwork_id,
        "title": artwork_data.get("title"),
        "artist": artwork_data.get("artist"),
        "museum_name": artwork_data.get("museum"),
        "artist_name": artwork_data.get("artist"),
        "visual_category": artwork_data.get("visual_category", "other"),
        "medium": artwork_data.get("medium", "other"),
        "period": artwork_data.get("period", "unknown"),
        "region": artwork_data.get("region", "unknown"),
        "quality_score": artwork_data.get("quality_score"),
        "measurement_coverage": artwork_data.get("measurement_coverage"),
        "selection_score": artwork_data.get("selection_score"),
        "image_width": artwork_data.get("image_width"),
        "image_height": artwork_data.get("image_height"),
        # Editorial type is independent from single/carousel publication shape.
        "content_type": artwork_data.get("content_type"),
        "publication_id": publication_id,
        "publication_type": publication_type,
        "status": "PENDING",
        "media_id": None,
        "reservation_id": str(uuid.uuid4()),
        "reserved_at": reserved_at,
    }


def reserve_artworks(
    artwork_data_items: Iterable[Dict[str, Any]],
    publication_type: str,
    publication_id: str | None = None,
) -> str:
    """Atomically reserve every artwork for one future feed publication."""
    artworks = list(artwork_data_items)
    if publication_type not in PUBLICATION_TYPES:
        raise ValueError(f"Unsupported publication type: {publication_type}")
    if publication_type == "single" and len(artworks) != 1:
        raise ValueError("Single publications require exactly one artwork")
    if publication_type == "carousel" and len(artworks) < 2:
        raise ValueError("Carousel publications require at least two artworks")

    artwork_ids = [normalize_artwork_id(artwork["id"]) for artwork in artworks]
    if len(artwork_ids) != len(set(artwork_ids)):
        raise ValueError("Cannot reserve duplicate artwork IDs in one publication")

    stable_publication_id = (publication_id or str(uuid.uuid4())).strip()
    if not stable_publication_id:
        raise ValueError("Publication ID must not be empty")

    history, etag = load_history_with_etag()
    original_history = copy.deepcopy(history)
    now = datetime.now(timezone.utc)
    requested_ids = set(artwork_ids)
    retained_records = []
    for item in history.get("posted_artworks", []):
        item_id = item.get("id") if isinstance(item, dict) else None
        canonical_id = normalize_artwork_id(item_id) if isinstance(item_id, str) else None
        if canonical_id not in requested_ids:
            retained_records.append(item)
            continue
        if str(item.get("status", "")).upper() == "EXPIRED" or _is_stale_pending(item, now):
            continue
        raise RuntimeError(f"Artwork {canonical_id} is already protected by history")

    reserved_at = _utc_timestamp()
    history["posted_artworks"] = retained_records + [
        _reservation_record(artwork, stable_publication_id, publication_type, reserved_at)
        for artwork in artworks
    ]
    try:
        _upload_history(history, etag)
    except Exception:
        history.clear()
        history.update(original_history)
        raise
    logger.info(
        "Reserved %s artwork(s) for %s publication %s in R2 history (PENDING).",
        len(artworks),
        publication_type,
        stable_publication_id,
    )
    return stable_publication_id


def reserve_artwork(artwork_data: Dict[str, Any], publication_id: str | None = None) -> str:
    """Backward-compatible one-artwork reservation helper."""
    return reserve_artworks([artwork_data], "single", publication_id)


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


def confirm_artworks_and_record_publication(
    artwork_ids: Iterable[str],
    media_id: str,
    publication_type: str,
    publication_id: str | None = None,
    theme: str | None = None,
    content_type: str | None = None,
) -> Dict[str, Any]:
    """Atomically confirm artwork locks and append one proven publication.

    Callers must invoke this only after Instagram returns a definite media ID.
    A failed conditional upload leaves the durable R2 records PUBLISHING.
    """
    canonical_ids = [normalize_artwork_id(artwork_id) for artwork_id in artwork_ids]
    if not canonical_ids:
        raise ValueError("At least one artwork ID is required")
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("Publication artwork IDs must be unique")
    if publication_type not in PUBLICATION_TYPES:
        raise ValueError(f"Unsupported publication type: {publication_type}")
    if not isinstance(media_id, str) or not media_id.strip():
        raise ValueError("Instagram media ID must not be empty")
    media_id = media_id.strip()

    history, etag = load_history_with_etag()
    original_history = copy.deepcopy(history)
    publications = _validated_publications(history)
    grid_count = _grid_publication_count(history, publications)

    records_by_id: Dict[str, list[Dict[str, Any]]] = {}
    for item in history.get("posted_artworks", []):
        artwork_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(artwork_id, str):
            records_by_id.setdefault(normalize_artwork_id(artwork_id), []).append(item)

    missing_ids = set(canonical_ids).difference(records_by_id)
    if missing_ids:
        raise RuntimeError(
            "Could not find reservation(s) to finalize: " + ", ".join(sorted(missing_ids))
        )
    duplicate_records = [artwork_id for artwork_id in canonical_ids if len(records_by_id[artwork_id]) != 1]
    if duplicate_records:
        raise CorruptedHistoryError(
            "History contains duplicate artwork records: " + ", ".join(sorted(duplicate_records))
        )

    target_records = [records_by_id[artwork_id][0] for artwork_id in canonical_ids]
    stored_publication_ids = {
        item.get("publication_id")
        for item in target_records
        if isinstance(item.get("publication_id"), str) and item.get("publication_id")
    }
    if publication_id is None:
        if len(stored_publication_ids) > 1:
            raise CorruptedHistoryError("Target artworks belong to different publications")
        stable_publication_id = next(iter(stored_publication_ids), str(uuid.uuid4()))
    else:
        stable_publication_id = publication_id.strip()
        if not stable_publication_id:
            raise ValueError("Publication ID must not be empty")
    if stored_publication_ids and stored_publication_ids != {stable_publication_id}:
        raise RuntimeError("Target artwork reservation does not belong to this publication")
    if stored_publication_ids:
        if any(item.get("publication_id") != stable_publication_id for item in target_records):
            raise CorruptedHistoryError("Only some target artworks carry the publication ID")
        stored_group = [
            item
            for records in records_by_id.values()
            for item in records
            if item.get("publication_id") == stable_publication_id
        ]
        stored_group_ids = [normalize_artwork_id(item["id"]) for item in stored_group]
        if len(stored_group_ids) != len(set(stored_group_ids)) or stored_group_ids != canonical_ids:
            raise RuntimeError("Finalization artwork IDs do not match the complete reservation group")
        if any(item.get("publication_type") != publication_type for item in stored_group):
            raise RuntimeError("Stored reservation publication type does not match finalization")

    if content_type is None and publication_type == "single":
        stored_content_types = {item.get("content_type") for item in target_records if item.get("content_type")}
        if len(stored_content_types) == 1:
            content_type = next(iter(stored_content_types))

    existing_publication = next(
        (publication for publication in publications if publication["id"] == stable_publication_id),
        None,
    )
    if existing_publication is not None:
        expected_core = {
            "id": stable_publication_id,
            "type": publication_type,
            "media_id": media_id,
            "artwork_ids": canonical_ids,
        }
        if any(existing_publication.get(key) != value for key, value in expected_core.items()):
            raise CorruptedHistoryError(f"Conflicting publication ID: {stable_publication_id}")
        if existing_publication.get("theme") != theme:
            raise CorruptedHistoryError(f"Conflicting publication theme: {stable_publication_id}")
        if existing_publication.get("content_type") != content_type:
            raise CorruptedHistoryError(f"Conflicting publication content type: {stable_publication_id}")
        if any(
            str(item.get("status", "")).upper() != "PUBLISHED"
            or item.get("media_id") != media_id
            or item.get("publication_id") != stable_publication_id
            for item in target_records
        ):
            raise CorruptedHistoryError(f"Publication artwork state is inconsistent: {stable_publication_id}")
        logger.info("Publication %s was already finalized; no history write needed.", stable_publication_id)
        return existing_publication

    if any(publication["media_id"] == media_id for publication in publications):
        raise CorruptedHistoryError(f"Instagram media ID already belongs to another publication: {media_id}")

    invalid_states = {
        artwork_id: str(record.get("status", "")).upper() or "missing"
        for artwork_id, record in zip(canonical_ids, target_records)
        if str(record.get("status", "")).upper() != "PUBLISHING"
    }
    if invalid_states:
        details = ", ".join(
            f"{artwork_id}={status}" for artwork_id, status in sorted(invalid_states.items())
        )
        raise RuntimeError(f"Cannot finalize reservation(s): {details}")

    posted_at = _utc_timestamp()
    publication = PublicationRecord(
        id=stable_publication_id,
        type=publication_type,
        media_id=media_id,
        artwork_ids=canonical_ids,
        posted_at=posted_at,
        theme=theme,
        content_type=content_type,
    ).model_dump(exclude_none=True)

    for item in target_records:
        item["status"] = "PUBLISHED"
        item["media_id"] = media_id
        item["posted_at"] = posted_at
        item["publication_id"] = stable_publication_id
        item["publication_type"] = publication_type

    history.setdefault("publications", []).append(publication)
    new_grid_count = grid_count + 1
    history["grid_publication_count"] = new_grid_count

    current_tone = history.get("active_color_tone")
    if not isinstance(current_tone, str) or not current_tone.strip():
        current_tone = "warm"
    if new_grid_count % 3 == 0:
        available_tones = [tone for tone in GRID_COLOR_TONES if tone != current_tone]
        current_tone = random.choice(available_tones)
    history["active_color_tone"] = current_tone

    try:
        _upload_history(history, etag)
    except Exception:
        history.clear()
        history.update(original_history)
        raise

    logger.info(
        "Finalized %s artwork(s) and recorded one %s publication %s (media_id=%s).",
        len(canonical_ids),
        publication_type,
        stable_publication_id,
        media_id,
    )
    return publication


def confirm_artwork(artwork_id: str, media_id: str) -> Dict[str, Any]:
    """Backward-compatible single-publication finalizer."""
    return confirm_artworks_and_record_publication([artwork_id], media_id, "single")


def get_grid_color_tone(read_only: bool = False) -> str:
    """Return the persisted tone; successful finalization advances grid rows."""
    history, _ = load_history_with_etag()
    publications = _validated_publications(history)
    tracked_count = _grid_publication_count(history, publications)
    current_tone = history.get("active_color_tone", "warm")
    if not isinstance(current_tone, str) or not current_tone.strip():
        current_tone = "warm"
    logger.info(
        "%s grid tone lookup. Using %s after %s tracked publication(s).",
        "Read-only" if read_only else "Production",
        current_tone,
        tracked_count,
    )
    return current_tone
