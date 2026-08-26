import copy
import json
import os
import random
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import ValidationError
from typing import Callable, Iterable, Sequence, Set, Dict, Any, Tuple, TypeVar
from src.carousel_themes import CarouselFormat, ThemeFamily, ThemeHistorySlot
from src.carousel_policy import (
    MAX_FEATURED_WORKS,
    MAX_TOTAL_SLIDES,
    MIN_FEATURED_WORKS,
    MIN_TOTAL_SLIDES,
)
from src.models import PublicationRecord, normalize_artwork_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HISTORY_OBJECT_KEY = "posted_history.json"
# Must exceed the workflow's 45-minute hard timeout and normal publish duration.
PENDING_RESERVATION_TTL = timedelta(hours=2)
HISTORY_CONDITIONAL_WRITE_ATTEMPTS = 3
GRID_COLOR_TONES = [
    "red", "blue", "green", "yellow", "purple", "brown",
    "monochrome", "warm", "cool",
]
PUBLICATION_TYPES = {"single", "carousel"}
R2_CLIENT_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"total_max_attempts": 1, "mode": "standard"},
)

class ConcurrentWriteError(Exception):
    """Raised when an R2 history write loses its compare-and-swap condition."""
    pass

class CorruptedHistoryError(Exception):
    """Raised when R2 history JSON is malformed."""
    pass


def _validated_publications(history: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return validated forward-only publication records; absent means legacy."""
    if "publications" not in history:
        return []
    publications = history["publications"]
    if not isinstance(publications, list):
        raise CorruptedHistoryError("History publications must be a list")

    seen_ids: set[str] = set()
    seen_media_ids: set[str] = set()
    for index, publication in enumerate(publications):
        if not isinstance(publication, dict):
            raise CorruptedHistoryError(
                f"History publication at index {index} must be an object"
            )
        try:
            validated = PublicationRecord.model_validate(publication)
        except ValidationError as exc:
            raise CorruptedHistoryError(
                f"Malformed history publication at index {index}"
            ) from exc
        if validated.id in seen_ids:
            raise CorruptedHistoryError(f"Duplicate publication ID: {validated.id}")
        if validated.media_id in seen_media_ids:
            raise CorruptedHistoryError(
                f"Duplicate publication media ID: {validated.media_id}"
            )
        seen_ids.add(validated.id)
        seen_media_ids.add(validated.media_id)
    return publications


def _grid_publication_count(
    history: Dict[str, Any], publications: list[Dict[str, Any]]
) -> int:
    """Read the forward-only grid counter without inferring legacy rows."""
    if "grid_publication_count" not in history:
        if publications:
            raise CorruptedHistoryError(
                "Publication history is missing grid_publication_count"
            )
        return 0
    count = history["grid_publication_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CorruptedHistoryError(
            "grid_publication_count must be a non-negative integer"
        )
    if count != len(publications):
        raise CorruptedHistoryError(
            "grid_publication_count must equal the number of publications"
        )
    return count


class PublicationStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    AMBIGUOUS = "AMBIGUOUS"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PublicationUnit:
    publication_id: str
    publication_type: str
    artwork_ids: tuple[str, ...]
    status: PublicationStatus | None
    record_statuses: tuple[str, ...]
    container_id: str | None
    child_container_ids: tuple[str, ...]
    publish_started_at: datetime | None
    publish_response_media_id: str | None
    reserved_at: datetime | None


_MutationResult = TypeVar("_MutationResult")


def _validated_etag(value: Any) -> str:
    """Return an opaque, strong S3 ETag without changing its quoted form."""
    if not isinstance(value, str) or len(value) < 3:
        raise RuntimeError("R2 history response is missing a valid ETag")
    if not (value.startswith('"') and value.endswith('"')):
        raise RuntimeError("R2 history response returned a malformed ETag")
    if any(character in value for character in ("\r", "\n")):
        raise RuntimeError("R2 history response returned a malformed ETag")
    return value


def _is_precondition_failed(error: ClientError) -> bool:
    response = error.response
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "412"} or status == 412


def _is_missing_object(error: ClientError) -> bool:
    response = error.response
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404

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
        region_name="auto",
        config=R2_CLIENT_CONFIG,
    )

def _get_bucket_name() -> str:
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
    if not bucket:
        raise ValueError("Missing CLOUDFLARE_R2_BUCKET_NAME")
    return bucket

def load_history_with_etag() -> Tuple[Dict[str, Any], str | None]:
    """Loads posted history JSON from Cloudflare R2 and returns (data, etag)."""
    try:
        s3 = _get_s3_client()
        bucket = _get_bucket_name()
        logger.info(f"Downloading {HISTORY_OBJECT_KEY} from R2...")
        
        response = s3.get_object(Bucket=bucket, Key=HISTORY_OBJECT_KEY)
        content = response['Body'].read().decode('utf-8')
        # Preserve the exact quoted value returned by S3/R2. ETags are opaque
        # validators, not content MD5 values.
        etag = _validated_etag(response.get("ETag"))
        
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
        if _is_missing_object(e):
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

def _upload_history(history: Dict[str, Any], etag: str | None = None):
    """Write history with create-if-absent or exact-ETag CAS semantics."""
    s3 = _get_s3_client()
    bucket = _get_bucket_name()
    content = json.dumps(history, ensure_ascii=False, indent=2)
    
    kwargs = {
        "Bucket": bucket,
        "Key": HISTORY_OBJECT_KEY,
        "Body": content.encode('utf-8'),
        "ContentType": "application/json"
    }
    
    if etag is None:
        # The missing-object load and first write must also be atomic. Without
        # this condition, simultaneous first reservations are last-writer-wins.
        kwargs["IfNoneMatch"] = "*"
    else:
        kwargs["IfMatch"] = _validated_etag(etag)
    
    logger.info(
        "Uploading %s to R2 with %s.",
        HISTORY_OBJECT_KEY,
        "create-if-absent" if etag is None else "ETag precondition",
    )
    
    try:
        s3.put_object(**kwargs)
    except ClientError as e:
        if _is_precondition_failed(e):
            logger.error("Concurrent R2 history write rejected by precondition.")
            raise ConcurrentWriteError("Conditional R2 history write failed") from e
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


def _utc_timestamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Lifecycle timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _publication_key(item: Dict[str, Any]) -> str:
    publication_id = item.get("publication_id")
    if isinstance(publication_id, str) and publication_id:
        return publication_id
    reservation_id = item.get("reservation_id")
    if isinstance(reservation_id, str) and reservation_id:
        return f"legacy-single:{reservation_id}"
    artwork_id = item.get("id")
    return f"legacy-artwork:{normalize_artwork_id(artwork_id) if isinstance(artwork_id, str) else 'unknown'}"


def _publication_records(
    history: Dict[str, Any], artwork_ids: Iterable[str]
) -> tuple[str, list[Dict[str, Any]]]:
    canonical_ids = {normalize_artwork_id(artwork_id) for artwork_id in artwork_ids}
    if not canonical_ids:
        raise ValueError("Publication lifecycle update requires at least one artwork ID")

    matching = [
        item
        for item in history.get("posted_artworks", [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and normalize_artwork_id(item["id"]) in canonical_ids
    ]
    found_ids = {normalize_artwork_id(item["id"]) for item in matching}
    missing = canonical_ids.difference(found_ids)
    if missing:
        raise RuntimeError("Missing publication reservation(s): " + ", ".join(sorted(missing)))

    keys = {_publication_key(item) for item in matching}
    if len(keys) != 1:
        raise RuntimeError("Artwork IDs do not identify exactly one publication unit")
    publication_key = next(iter(keys))
    grouped = [
        item
        for item in history.get("posted_artworks", [])
        if isinstance(item, dict) and _publication_key(item) == publication_key
    ]
    grouped_ids = {
        normalize_artwork_id(item["id"])
        for item in grouped
        if isinstance(item.get("id"), str)
    }
    if grouped_ids != canonical_ids:
        raise RuntimeError(
            "Publication lifecycle update must include the whole publication unit"
        )
    return publication_key, grouped


def _restore_records(
    records: Sequence[Dict[str, Any]], snapshots: Sequence[Dict[str, Any]]
) -> None:
    for record, snapshot in zip(records, snapshots):
        record.clear()
        record.update(snapshot)


def _restore_history_snapshot_in_place(
    history: Dict[str, Any], snapshot: Dict[str, Any]
) -> None:
    """Restore values and any externally-held artwork record references."""
    current_records = history.get("posted_artworks")
    snapshot_records = snapshot.get("posted_artworks")
    if isinstance(current_records, list) and isinstance(snapshot_records, list):
        for current, original in zip(current_records, snapshot_records):
            if isinstance(current, dict) and isinstance(original, dict):
                current.clear()
                current.update(copy.deepcopy(original))
    history.clear()
    history.update(copy.deepcopy(snapshot))


def _conditional_publication_update(
    artwork_ids: Iterable[str],
    mutation: Callable[[str, list[Dict[str, Any]]], tuple[_MutationResult, bool]],
) -> _MutationResult:
    """Reload and re-evaluate bounded lifecycle writes after an ETag conflict."""
    canonical_ids = tuple(normalize_artwork_id(value) for value in artwork_ids)
    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        publication_id, records = _publication_records(history, canonical_ids)
        snapshots = [dict(record) for record in records]
        result, changed = mutation(publication_id, records)
        if not changed:
            return result
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_records(records, snapshots)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            logger.warning(
                "publication_history_conflict publication_id=%s retry=%s/%s",
                publication_id,
                attempt + 1,
                HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
            )
            continue
        except Exception:
            _restore_records(records, snapshots)
            raise
        return result
    raise AssertionError("unreachable")


def _uniform_status(records: Sequence[Dict[str, Any]]) -> PublicationStatus:
    statuses = {str(record.get("status", "")).upper() for record in records}
    if len(statuses) != 1:
        raise RuntimeError(
            "Publication unit has inconsistent lifecycle states: "
            + ", ".join(sorted(statuses))
        )
    try:
        return PublicationStatus(next(iter(statuses)))
    except ValueError as error:
        raise RuntimeError("Publication unit has an unknown lifecycle state") from error


def _set_status(
    records: Sequence[Dict[str, Any]],
    target: PublicationStatus,
    *,
    authoritative: bool = False,
) -> PublicationStatus:
    current = _uniform_status(records)
    if current is target:
        return current
    legal = {
        PublicationStatus.PENDING: {
            PublicationStatus.PUBLISHING,
            PublicationStatus.AMBIGUOUS,
            PublicationStatus.EXPIRED,
        },
        PublicationStatus.PUBLISHING: {
            PublicationStatus.PUBLISHED,
            PublicationStatus.AMBIGUOUS,
            PublicationStatus.EXPIRED,
        },
        PublicationStatus.AMBIGUOUS: {
            PublicationStatus.PUBLISHED,
            PublicationStatus.EXPIRED,
        },
        PublicationStatus.PUBLISHED: set(),
        PublicationStatus.EXPIRED: set(),
    }
    if target not in legal[current]:
        raise RuntimeError(f"Illegal publication transition {current.value} -> {target.value}")
    if current is PublicationStatus.AMBIGUOUS and target is PublicationStatus.EXPIRED and not authoritative:
        raise RuntimeError("AMBIGUOUS may expire only with authoritative non-publication evidence")
    for record in records:
        record["status"] = target.value
    return current


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

    groups: dict[str, list[Dict[str, Any]]] = {}
    for item in history.get("posted_artworks", []):
        if isinstance(item, dict):
            groups.setdefault(_publication_key(item), []).append(item)

    recovered = 0
    expired_at = _utc_timestamp(recovery_time)
    for records in groups.values():
        if records and all(_is_stale_pending(item, recovery_time) for item in records):
            _set_status(records, PublicationStatus.EXPIRED, authoritative=True)
            for item in records:
                item["expired_at"] = expired_at
                item["expiration_reason"] = "pending_ttl_expired_before_publish_boundary"
            recovered += len(records)

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
        and str(item.get("status", "")).upper() in {"", "PUBLISHED"}
    ]


def _ordered_confirmed_artworks(
    history: Dict[str, Any], publications: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Order forward records by publication while preserving legacy rows."""
    confirmed = _confirmed_artworks(history)
    known_publication_ids = {publication["id"] for publication in publications}
    legacy_artworks: list[Dict[str, Any]] = []
    artworks_by_publication: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for artwork in confirmed:
        publication_id = artwork.get("publication_id")
        if not isinstance(publication_id, str):
            legacy_artworks.append(artwork)
            continue
        if publication_id not in known_publication_ids:
            # Donor role-bearing history may predate the additive publication index.
            # Reconciliation can also prove publication from a container status
            # without yielding the published media ID required by PublicationRecord.
            unindexed_reconciliation = (
                artwork.get("reconciliation_result") == "CONFIRMED_PUBLISHED"
                and not artwork.get("media_id")
            )
            if not publications or unindexed_reconciliation:
                legacy_artworks.append(artwork)
                continue
            raise CorruptedHistoryError(
                f"Published artwork {artwork.get('id')} references an unknown publication"
            )
        artwork_id = artwork.get("id")
        if not isinstance(artwork_id, str):
            raise CorruptedHistoryError(
                f"Published artwork in {publication_id} has no string ID"
            )
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
        expected_ids = [
            normalize_artwork_id(artwork_id)
            for artwork_id in publication["artwork_ids"]
        ]
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
    """Return proven published history for diversity decisions."""
    history, _ = load_history_with_etag()
    publications = _validated_publications(history)
    return _ordered_confirmed_artworks(history, publications)


def get_recent_publications(limit: int | None = None) -> list[Dict[str, Any]]:
    """Return proven forward-only publications; legacy history yields none."""
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
    ):
        raise ValueError("Publication limit must be a non-negative integer or None")
    history, _ = load_history_with_etag()
    publications = _validated_publications(history)
    if limit is None:
        return list(publications)
    if limit == 0:
        return []
    return list(publications[-limit:])


def get_recent_artworks_by_publication(
    publication_limit: int,
) -> list[Dict[str, Any]]:
    """Flatten artworks from the last N logical publication slots."""
    if (
        isinstance(publication_limit, bool)
        or not isinstance(publication_limit, int)
        or publication_limit < 0
    ):
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
            groups.append([artwork])
    return [artwork for group in groups[-publication_limit:] for artwork in group]


def get_recent_carousel_theme_history(limit: int = 24) -> list[ThemeHistorySlot]:
    """Return carousel-theme history as publication slots, never artwork rows.

    Current carousel records share a publication ID and collapse exactly. Older
    records use a shared media ID or reservation timestamp when available. A
    bare legacy ``theme`` entry remains readable without inventing a family or
    format; explicit single-artwork records are always ignored.
    """
    if limit < 1:
        raise ValueError("Theme history limit must be positive")

    history, _ = load_history_with_etag()
    slots: list[ThemeHistorySlot] = []
    seen_slot_keys: set[tuple[str, ...]] = set()
    previous_unkeyed_artwork_theme: str | None = None

    for index, item in enumerate(history.get("posted_artworks", [])):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).upper()
        if status not in {"", "PUBLISHED", "AMBIGUOUS"}:
            continue

        publication_type = str(item.get("publication_type", "")).upper()
        content_type = str(item.get("content_type", "")).upper()
        if publication_type and publication_type != "CAROUSEL":
            continue
        if content_type == "SINGLE_ARTWORK":
            continue

        raw_theme_id = item.get("theme_id", item.get("theme"))
        if not isinstance(raw_theme_id, str) or not (theme_id := raw_theme_id.strip()):
            continue

        raw_family = item.get("theme_family")
        try:
            theme_family = ThemeFamily(raw_family) if raw_family else None
        except (TypeError, ValueError):
            theme_family = None
        raw_format = item.get("carousel_format")
        try:
            carousel_format = CarouselFormat(raw_format) if raw_format else None
        except (TypeError, ValueError):
            carousel_format = None

        publication_id = item.get("publication_id")
        media_id = item.get("media_id")
        reserved_at = item.get("reserved_at")
        if isinstance(publication_id, str) and publication_id:
            slot_key = ("publication", publication_id)
        elif isinstance(media_id, str) and media_id:
            slot_key = ("media", media_id, theme_id)
        elif isinstance(reserved_at, str) and reserved_at:
            slot_key = ("reserved", reserved_at, theme_id)
        elif "id" not in item:
            # A publication-level legacy row represents one slot by itself.
            slot_key = ("legacy_publication", str(index))
        elif previous_unkeyed_artwork_theme == theme_id:
            # Consecutive legacy artwork rows for one carousel collapse safely.
            continue
        else:
            slot_key = ("legacy_artwork_run", str(index), theme_id)

        previous_unkeyed_artwork_theme = theme_id if "id" in item else None
        if slot_key in seen_slot_keys:
            continue
        seen_slot_keys.add(slot_key)
        slots.append(
            ThemeHistorySlot(
                theme_id=theme_id,
                theme_family=theme_family,
                carousel_format=carousel_format,
                publication_id=publication_id if isinstance(publication_id, str) else None,
            )
        )

    return slots[-limit:]

def _reservation_record(
    artwork_data: Dict[str, Any],
    *,
    publication_id: str | None = None,
    publication_role: str | None = None,
    cover_artwork_id: str | None = None,
    featured_artwork_ids: Sequence[str] | None = None,
    featured_position: int | None = None,
    theme_id: str | None = None,
    theme_family: str | None = None,
    carousel_format: str | None = None,
) -> Dict[str, Any]:
    artwork_id = normalize_artwork_id(artwork_data["id"])
    record = {
        "id": artwork_id,
        "title": artwork_data.get("title"),
        "artist": artwork_data.get("artist"),
        "museum_name": artwork_data.get("museum"),
        "artist_name": artwork_data.get("artist"),
        "visual_category": artwork_data.get("visual_category", "other"),
        "medium": artwork_data.get("medium", "other"),
        "period": artwork_data.get("period", "unknown"),
        "region": artwork_data.get("region", "unknown"),
        "published_orientation": artwork_data.get(
            "published_orientation", "UNKNOWN"
        ),
        "normalized_artist_key": artwork_data.get("normalized_artist_key"),
        "semantic_family": artwork_data.get("semantic_family", "UNKNOWN"),
        "visual_tone": artwork_data.get("visual_tone", "UNKNOWN"),
        "visual_color_family": artwork_data.get(
            "visual_color_family", "UNKNOWN"
        ),
        "quality_score": artwork_data.get("quality_score"),
        "measurement_coverage": artwork_data.get("measurement_coverage"),
        "selection_score": artwork_data.get("selection_score"),
        "image_width": artwork_data.get("image_width"),
        "image_height": artwork_data.get("image_height"),
        "published_width": artwork_data.get("published_width"),
        "published_height": artwork_data.get("published_height"),
        "published_image_format": artwork_data.get("published_image_format"),
        "published_image_file_size": artwork_data.get("published_image_file_size"),
        "source_width": artwork_data.get("source_width"),
        "source_height": artwork_data.get("source_height"),
        "source_image_format": artwork_data.get("source_image_format"),
        "source_image_file_size": artwork_data.get("source_image_file_size"),
        "exif_orientation": artwork_data.get("exif_orientation"),
        "image_processing": artwork_data.get("image_processing"),
        "compatibility_conversion": artwork_data.get("compatibility_conversion"),
        "source_bytes_preserved": artwork_data.get("source_bytes_preserved"),
        "jpeg_compatibility_quality": artwork_data.get(
            "jpeg_compatibility_quality"
        ),
        "compatibility_attempts": artwork_data.get("compatibility_attempts"),
        "content_type": artwork_data.get("content_type", "SINGLE_ARTWORK"),
        "publication_type": "SINGLE",
        "status": "PENDING",
        "media_id": None,
        "reservation_id": str(uuid.uuid4()),
        "reserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if publication_id is not None:
        record.update(
            {
                "publication_id": publication_id,
            }
        )
    if publication_role is not None:
        record.update(
            {
                "publication_type": "CAROUSEL",
                "publication_role": publication_role,
                "cover_artwork_id": cover_artwork_id,
                "featured_artwork_ids": list(featured_artwork_ids or ()),
            }
        )
        if featured_position is not None:
            record["featured_position"] = featured_position
        if theme_id is not None:
            record.update(
                {
                    "theme_id": theme_id,
                    "theme_family": theme_family,
                    "carousel_format": carousel_format,
                }
            )
    return record


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
        canonical_id = (
            normalize_artwork_id(item_id) if isinstance(item_id, str) else None
        )
        if canonical_id not in requested_ids:
            retained_records.append(item)
            continue
        if (
            str(item.get("status", "")).upper() == PublicationStatus.EXPIRED.value
            or _is_stale_pending(item, now)
        ):
            continue
        raise RuntimeError(f"Artwork {canonical_id} is already protected by history")

    records = []
    for artwork in artworks:
        record = _reservation_record(
            artwork,
            publication_id=stable_publication_id,
        )
        record["publication_type"] = publication_type
        record["content_type"] = artwork.get("content_type")
        records.append(record)
    history["posted_artworks"] = retained_records + records
    try:
        _upload_history(history, etag)
    except Exception:
        _restore_history_snapshot_in_place(history, original_history)
        raise
    logger.info(
        "Reserved %s artwork(s) for %s publication %s in R2 history (PENDING).",
        len(artworks),
        publication_type,
        stable_publication_id,
    )
    return stable_publication_id


def reserve_artwork(
    artwork_data: Dict[str, Any], publication_id: str | None = None
) -> str:
    """Backward-compatible one-artwork reservation helper."""
    return reserve_artworks([artwork_data], "single", publication_id)


def reserve_carousel(
    cover_artwork: Dict[str, Any],
    featured_artworks: Sequence[Dict[str, Any]],
    *,
    theme_id: str | None = None,
    theme_family: str | None = None,
    carousel_format: str | None = None,
) -> str:
    """Atomically reserve one cover and 3–8 featured works with explicit roles."""
    if not MIN_FEATURED_WORKS <= len(featured_artworks) <= MAX_FEATURED_WORKS:
        raise ValueError(
            "Carousel history reservation requires between "
            f"{MIN_FEATURED_WORKS} and {MAX_FEATURED_WORKS} featured artworks"
        )

    cover_id = normalize_artwork_id(cover_artwork["id"])
    featured_ids = [normalize_artwork_id(artwork["id"]) for artwork in featured_artworks]
    publication_ids = [cover_id, *featured_ids]
    if len(set(publication_ids)) != len(publication_ids):
        raise ValueError(
            "Carousel cover and featured artwork IDs must all be distinct canonical IDs"
        )

    theme_metadata = (theme_id, theme_family, carousel_format)
    if any(value is not None for value in theme_metadata):
        if not all(isinstance(value, str) and value for value in theme_metadata):
            raise ValueError("Carousel theme_id, theme_family, and carousel_format must be supplied together")
        try:
            ThemeFamily(theme_family)
            CarouselFormat(carousel_format)
        except ValueError as error:
            raise ValueError(f"Invalid carousel theme metadata: {error}") from error

    history, etag = load_history_with_etag()
    now = datetime.now(timezone.utc)
    protected_existing = {
        normalize_artwork_id(item.get("id", ""))
        for item in history.get("posted_artworks", [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and str(item.get("status", "")).upper() != "EXPIRED"
        and not _is_stale_pending(item, now)
    }
    collisions = set(publication_ids).intersection(protected_existing)
    if collisions:
        raise RuntimeError(
            "Carousel reservation collided with protected artwork(s): "
            + ", ".join(sorted(collisions))
        )

    # Remove only expired/stale records for these IDs. Legacy and active records
    # were rejected above and are never reinterpreted.
    publication_id_set = set(publication_ids)
    history["posted_artworks"] = [
        item
        for item in history.get("posted_artworks", [])
        if not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or normalize_artwork_id(item["id"]) not in publication_id_set
    ]
    publication_id = str(uuid.uuid4())
    cover_payload = dict(cover_artwork)
    cover_payload["content_type"] = "CAROUSEL_COVER"
    history["posted_artworks"].append(
        _reservation_record(
            cover_payload,
            publication_id=publication_id,
            publication_role="COVER",
            cover_artwork_id=cover_id,
            featured_artwork_ids=featured_ids,
            theme_id=theme_id,
            theme_family=theme_family,
            carousel_format=carousel_format,
        )
    )
    for position, artwork in enumerate(featured_artworks, start=1):
        featured_payload = dict(artwork)
        featured_payload["content_type"] = "CAROUSEL_FEATURED"
        history["posted_artworks"].append(
            _reservation_record(
                featured_payload,
                publication_id=publication_id,
                publication_role="FEATURED",
                cover_artwork_id=cover_id,
                featured_artwork_ids=featured_ids,
                featured_position=position,
                theme_id=theme_id,
                theme_family=theme_family,
                carousel_format=carousel_format,
            )
        )

    _upload_history(history, etag)
    logger.info(
        "Reserved carousel publication=%s cover=%s featured=%s in one R2 write.",
        publication_id,
        cover_id,
        ",".join(featured_ids),
    )
    return publication_id


def start_publication_attempt(
    artwork_ids: Iterable[str],
    container_id: str,
    child_container_ids: Sequence[str] = (),
) -> int:
    """Persist the irreversible boundary and its container evidence atomically."""
    if not isinstance(container_id, str) or not container_id:
        raise ValueError("A non-empty parent/creation container ID is required")
    children = tuple(child_container_ids)
    if any(not isinstance(value, str) or not value for value in children):
        raise ValueError("Child container IDs must be non-empty strings")
    started_at = _utc_timestamp()

    def mutation(publication_id, records):
        existing_status = _uniform_status(records)
        if existing_status is PublicationStatus.PUBLISHING:
            existing_containers = {record.get("container_id") for record in records}
            existing_children = {
                tuple(record.get("child_container_ids", ())) for record in records
            }
            if existing_containers == {container_id} and existing_children == {children}:
                return len(records), False
            raise RuntimeError(
                "Publication unit is already crossing a different publish boundary"
            )
        current = _set_status(records, PublicationStatus.PUBLISHING)
        for record in records:
            record["container_id"] = container_id
            record["publish_started_at"] = started_at
            record["publishing_at"] = started_at
            if children:
                record["child_container_ids"] = list(children)
        logger.info(
            "publication_transition publication_id=%s from=%s to=PUBLISHING container_id=%s",
            publication_id,
            current.value,
            container_id,
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)


def mark_artworks_ambiguous(
    artwork_ids: Iterable[str], ambiguity_reason: str = "uncertain_media_publish_result"
) -> int:
    """Quarantine a whole publication unit after an unprovable publish result."""
    ambiguous_at = _utc_timestamp()

    def mutation(publication_id, records):
        current = _uniform_status(records)
        if current is PublicationStatus.PUBLISHED:
            return 0, False
        if current is PublicationStatus.AMBIGUOUS:
            return 0, False
        previous = _set_status(records, PublicationStatus.AMBIGUOUS)
        for record in records:
            record["ambiguous_at"] = ambiguous_at
            record["ambiguity_reason"] = ambiguity_reason
        logger.error(
            "publication_transition publication_id=%s from=%s to=AMBIGUOUS reason=%s",
            publication_id,
            previous.value,
            ambiguity_reason,
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)


def mark_artwork_ambiguous(
    artwork_id: str, ambiguity_reason: str = "uncertain_media_publish_result"
) -> int:
    """Mark one reserved artwork AMBIGUOUS; see mark_artworks_ambiguous."""
    return mark_artworks_ambiguous([artwork_id], ambiguity_reason)


def mark_artworks_publishing(artwork_ids: Iterable[str]) -> int:
    """Compatibility transition for pre-container callers.

    Production publishing uses :func:`start_publication_attempt`, which also
    persists the creation container immediately before ``media_publish``.
    """
    started_at = _utc_timestamp()

    def mutation(publication_id, records):
        current = _set_status(records, PublicationStatus.PUBLISHING)
        for record in records:
            record["publish_started_at"] = started_at
            record["publishing_at"] = started_at
        logger.info(
            "publication_transition publication_id=%s from=%s to=PUBLISHING container_id=unknown",
            publication_id,
            current.value,
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)


def mark_artworks_pending(artwork_ids: Iterable[str]) -> int:
    """Rollback only a legacy pre-container lock where no publish could start."""

    def mutation(publication_id, records):
        if _uniform_status(records) is not PublicationStatus.PUBLISHING:
            return 0, False
        if any(record.get("container_id") for record in records):
            raise RuntimeError("A container-backed PUBLISHING unit cannot return to PENDING")
        for record in records:
            record["status"] = PublicationStatus.PENDING.value
            record.pop("publish_started_at", None)
            record.pop("publishing_at", None)
        logger.info(
            "publication_transition publication_id=%s from=PUBLISHING to=PENDING pre_boundary=true",
            publication_id,
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)


def mark_publication_not_published(
    artwork_ids: Iterable[str], reason: str, *, authoritative: bool = False
) -> int:
    """Release a whole unit only after conclusive non-publication evidence."""
    expired_at = _utc_timestamp()

    def mutation(publication_id, records):
        current = _set_status(
            records, PublicationStatus.EXPIRED, authoritative=authoritative
        )
        for record in records:
            record["expired_at"] = expired_at
            record["expiration_reason"] = reason
        logger.info(
            "publication_transition publication_id=%s from=%s to=EXPIRED reason=%s",
            publication_id,
            current.value,
            reason,
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)


def record_publish_response(artwork_ids: Iterable[str], media_id: str) -> int:
    """Durably retain the authoritative media ID before final confirmation."""
    if not isinstance(media_id, str) or not media_id:
        raise ValueError("A non-empty Instagram media ID is required")

    def mutation(publication_id, records):
        status = _uniform_status(records)
        if status not in {
            PublicationStatus.PUBLISHING,
            PublicationStatus.AMBIGUOUS,
            PublicationStatus.PUBLISHED,
        }:
            raise RuntimeError(f"Cannot record publish response from {status.value}")
        if all(record.get("publish_response_media_id") == media_id for record in records):
            return 0, False
        for record in records:
            record["publish_response_media_id"] = media_id
        logger.info(
            "publication_response_recorded publication_id=%s media_id=%s",
            publication_id,
            media_id,
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)


def _finalize_publication_history(
    history: Dict[str, Any],
    canonical_ids: list[str],
    media_id: str,
    publication_type: str,
    publication_id: str | None,
    theme: str | None,
    content_type: str | None,
    *,
    allowed_statuses: frozenset[PublicationStatus] = frozenset(
        {PublicationStatus.PUBLISHING}
    ),
) -> tuple[Dict[str, Any], bool]:
    """Validate and mutate one loaded history snapshot for finalization."""
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
            "Could not find reservation(s) to finalize: "
            + ", ".join(sorted(missing_ids))
        )
    duplicate_records = [
        artwork_id
        for artwork_id in canonical_ids
        if len(records_by_id[artwork_id]) != 1
    ]
    if duplicate_records:
        raise CorruptedHistoryError(
            "History contains duplicate artwork records: "
            + ", ".join(sorted(duplicate_records))
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
        if any(
            item.get("publication_id") != stable_publication_id
            for item in target_records
        ):
            raise CorruptedHistoryError("Only some target artworks carry the publication ID")
        stored_group = [
            item
            for records in records_by_id.values()
            for item in records
            if item.get("publication_id") == stable_publication_id
        ]
        stored_group_ids = [normalize_artwork_id(item["id"]) for item in stored_group]
        if (
            len(stored_group_ids) != len(set(stored_group_ids))
            or stored_group_ids != canonical_ids
        ):
            raise RuntimeError(
                "Finalization artwork IDs do not match the complete reservation group"
            )
        if any(
            str(item.get("publication_type", "")).casefold()
            != publication_type
            for item in stored_group
        ):
            raise RuntimeError(
                "Stored reservation publication type does not match finalization"
            )

    role_records = [record for record in target_records if record.get("publication_role")]
    if role_records:
        if publication_type != "carousel":
            raise RuntimeError("Role-bearing reservation must finalize as carousel")
        if sum(record.get("publication_role") == "COVER" for record in role_records) != 1:
            raise RuntimeError("Carousel reservation requires exactly one cover role")
        featured = [
            record
            for record in role_records
            if record.get("publication_role") == "FEATURED"
        ]
        if {record.get("featured_position") for record in featured} != set(
            range(1, len(featured) + 1)
        ):
            raise RuntimeError("Carousel featured role/order mismatch")

    if content_type is None and publication_type == "single":
        stored_types = {
            item.get("content_type")
            for item in target_records
            if item.get("content_type")
        }
        if len(stored_types) == 1:
            content_type = next(iter(stored_types))

    existing_publication = next(
        (
            publication
            for publication in publications
            if publication["id"] == stable_publication_id
        ),
        None,
    )
    if existing_publication is not None:
        expected_core = {
            "id": stable_publication_id,
            "type": publication_type,
            "media_id": media_id,
            "artwork_ids": canonical_ids,
        }
        if any(
            existing_publication.get(key) != value
            for key, value in expected_core.items()
        ):
            raise CorruptedHistoryError(
                f"Conflicting publication ID: {stable_publication_id}"
            )
        if existing_publication.get("theme") != theme:
            raise CorruptedHistoryError(
                f"Conflicting publication theme: {stable_publication_id}"
            )
        if existing_publication.get("content_type") != content_type:
            raise CorruptedHistoryError(
                f"Conflicting publication content type: {stable_publication_id}"
            )
        if any(
            str(item.get("status", "")).upper() != "PUBLISHED"
            or item.get("media_id") != media_id
            or item.get("publication_id") != stable_publication_id
            for item in target_records
        ):
            raise CorruptedHistoryError(
                f"Publication artwork state is inconsistent: {stable_publication_id}"
            )
        return existing_publication, False

    if any(publication["media_id"] == media_id for publication in publications):
        raise CorruptedHistoryError(
            f"Instagram media ID already belongs to another publication: {media_id}"
        )
    invalid_states = {
        artwork_id: str(record.get("status", "")).upper() or "missing"
        for artwork_id, record in zip(canonical_ids, target_records)
        if str(record.get("status", "")).upper()
        not in {status.value for status in allowed_statuses}
    }
    if invalid_states:
        details = ", ".join(
            f"{artwork_id}={status}"
            for artwork_id, status in sorted(invalid_states.items())
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
        item["status"] = PublicationStatus.PUBLISHED.value
        item["media_id"] = media_id
        item["publish_response_media_id"] = media_id
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
        current_tone = random.choice(
            [tone for tone in GRID_COLOR_TONES if tone != current_tone]
        )
    history["active_color_tone"] = current_tone
    return publication, True


def confirm_artworks_and_record_publication(
    artwork_ids: Iterable[str],
    media_id: str,
    publication_type: str,
    publication_id: str | None = None,
    theme: str | None = None,
    content_type: str | None = None,
) -> Dict[str, Any]:
    """Confirm lifecycle rows and append one publication in one bounded CAS."""
    canonical_ids = [normalize_artwork_id(value) for value in artwork_ids]
    if not canonical_ids:
        raise ValueError("At least one artwork ID is required")
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("Publication artwork IDs must be unique")
    if publication_type not in PUBLICATION_TYPES:
        raise ValueError(f"Unsupported publication type: {publication_type}")
    if not isinstance(media_id, str) or not media_id.strip():
        raise ValueError("Instagram media ID must not be empty")
    media_id = media_id.strip()

    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        publication, changed = _finalize_publication_history(
            history,
            canonical_ids,
            media_id,
            publication_type,
            publication_id,
            theme,
            content_type,
        )
        if not changed:
            return publication
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            logger.warning(
                "publication_finalization_conflict publication_id=%s retry=%s/%s",
                publication_id or publication["id"],
                attempt + 1,
                HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
            )
            continue
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        return publication
    raise AssertionError("unreachable")


def confirm_publication(
    artwork_ids: Iterable[str],
    media_id: str | None,
    *,
    authoritative: bool = False,
    reconciliation_evidence: str | None = None,
    record_validator: Callable[[Sequence[Dict[str, Any]]], None] | None = None,
) -> int:
    """Atomically confirm one single or carousel publication unit."""
    posted_at = _utc_timestamp()

    def mutation(publication_id, records):
        if record_validator is not None:
            record_validator(records)
        status = _uniform_status(records)
        if status is PublicationStatus.PUBLISHED:
            return 0, False
        if status not in {PublicationStatus.PUBLISHING, PublicationStatus.AMBIGUOUS}:
            raise RuntimeError(f"Cannot confirm publication from {status.value}")
        if not authoritative and not media_id:
            raise RuntimeError("A media ID is required for response-based confirmation")
        previous = _set_status(records, PublicationStatus.PUBLISHED)
        for record in records:
            if media_id:
                record["media_id"] = media_id
                record["publish_response_media_id"] = media_id
            record["posted_at"] = posted_at
            if reconciliation_evidence:
                record["last_reconciled_at"] = posted_at
                record["reconciliation_evidence"] = reconciliation_evidence
                record["reconciliation_attempt_count"] = int(
                    record.get("reconciliation_attempt_count", 0)
                ) + 1
        logger.info(
            "publication_transition publication_id=%s from=%s to=PUBLISHED evidence=%s",
            publication_id,
            previous.value,
            reconciliation_evidence or "media_publish_response",
        )
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)

def confirm_artwork(artwork_id: str, media_id: str):
    """Finalize a normal single publish and create its additive index row."""
    return confirm_artworks_and_record_publication(
        [artwork_id], media_id, "single"
    )


def confirm_carousel_publication(
    cover_artwork_id: str,
    featured_artwork_ids: Sequence[str],
    media_id: str,
) -> int:
    """Finalize all variable-length role-bearing carousel records atomically."""
    cover_id = normalize_artwork_id(cover_artwork_id)
    featured_ids = [normalize_artwork_id(artwork_id) for artwork_id in featured_artwork_ids]
    expected_ids = {cover_id, *featured_ids}
    if (
        not MIN_FEATURED_WORKS <= len(featured_ids) <= MAX_FEATURED_WORKS
        or len(expected_ids) != len(featured_ids) + 1
    ):
        raise ValueError(
            "Carousel finalization requires one cover and between "
            f"{MIN_FEATURED_WORKS} and {MAX_FEATURED_WORKS} distinct featured artworks"
        )

    publication = confirm_artworks_and_record_publication(
        [cover_id, *featured_ids], media_id, "carousel"
    )
    return len(publication["artwork_ids"])


def list_unresolved_publication_units(
    *,
    limit: int,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> list[PublicationUnit]:
    """Return bounded unresolved publication units, newest first."""
    if limit < 1:
        raise ValueError("Publication reconciliation limit must be positive")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("Reconciliation time must be timezone-aware")
    reference_time = reference_time.astimezone(timezone.utc)
    history, _ = load_history_with_etag()
    groups: dict[str, list[Dict[str, Any]]] = {}
    group_order: list[str] = []
    unresolved_keys: set[str] = set()
    for item in history.get("posted_artworks", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status", "")).upper() in {
            PublicationStatus.PENDING.value,
            PublicationStatus.PUBLISHING.value,
            PublicationStatus.AMBIGUOUS.value,
        }:
            unresolved_keys.add(_publication_key(item))

    for item in reversed(history.get("posted_artworks", [])):
        if not isinstance(item, dict):
            continue
        key = _publication_key(item)
        if key not in unresolved_keys:
            continue
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(item)

    units: list[PublicationUnit] = []
    for key in group_order:
        records = list(reversed(groups[key]))
        statuses = tuple(str(record.get("status", "")).upper() for record in records)
        publication_types = {
            str(record.get("publication_type", "SINGLE")).upper()
            for record in records
        }
        container_values = {
            record.get("container_id")
            if isinstance(record.get("container_id"), str)
            else None
            for record in records
        }
        child_values = {
            tuple(record.get("child_container_ids", ()))
            if isinstance(record.get("child_container_ids", ()), list)
            else ()
            for record in records
        }
        response_media_values = {
            record.get("publish_response_media_id")
            if isinstance(record.get("publish_response_media_id"), str)
            else None
            for record in records
        }
        artwork_ids = tuple(
            normalize_artwork_id(record["id"])
            for record in records
            if isinstance(record.get("id"), str)
        )
        publication_type = next(iter(publication_types))
        shape_is_valid = (
            len(artwork_ids) == len(records)
            and len(artwork_ids) == len(set(artwork_ids))
            and (
                (publication_type == "SINGLE" and len(records) == 1)
                or (
                    publication_type == "CAROUSEL"
                    and MIN_TOTAL_SLIDES <= len(records) <= MAX_TOTAL_SLIDES
                    and sum(
                        record.get("publication_role") == "COVER"
                        for record in records
                    )
                    == 1
                    and sum(
                        record.get("publication_role") == "FEATURED"
                        for record in records
                    )
                    == len(records) - 1
                    and {
                        record.get("featured_position")
                        for record in records
                        if record.get("publication_role") == "FEATURED"
                    }
                    == set(range(1, len(records)))
                )
            )
        )
        metadata_is_consistent = (
            len(publication_types) == 1
            and len(container_values) == 1
            and len(child_values) == 1
            and len(response_media_values) == 1
            and shape_is_valid
        )
        try:
            status = (
                PublicationStatus(statuses[0])
                if len(set(statuses)) == 1 and metadata_is_consistent
                else None
            )
        except ValueError:
            status = None
        reserved_at = _parse_reserved_at(records[0].get("reserved_at"))
        publish_started_at = _parse_reserved_at(
            records[0].get("publish_started_at", records[0].get("publishing_at"))
        )
        lifecycle_at = publish_started_at or _parse_reserved_at(
            records[0].get("ambiguous_at")
        ) or reserved_at
        if max_age is not None and lifecycle_at is not None:
            if reference_time - lifecycle_at > max_age:
                continue
        child_ids = records[0].get("child_container_ids")
        child_container_ids = (
            tuple(value for value in child_ids if isinstance(value, str) and value)
            if isinstance(child_ids, list)
            else ()
        )
        media_id = records[0].get("publish_response_media_id")
        container_id = records[0].get("container_id")
        units.append(
            PublicationUnit(
                publication_id=key,
                publication_type=publication_type,
                artwork_ids=artwork_ids,
                status=status,
                record_statuses=statuses,
                container_id=container_id if isinstance(container_id, str) and container_id else None,
                child_container_ids=child_container_ids,
                publish_started_at=publish_started_at,
                publish_response_media_id=media_id if isinstance(media_id, str) and media_id else None,
                reserved_at=reserved_at,
            )
        )
        if len(units) == limit:
            break
    return units


def record_reconciliation_result(
    artwork_ids: Iterable[str],
    *,
    target_status: PublicationStatus | None,
    result: str,
    evidence: str,
    media_id: str | None = None,
    authoritative: bool = False,
    expected_status: PublicationStatus | None = None,
    now: datetime | None = None,
) -> int:
    """Atomically record one publication-level reconciliation result."""
    reconciled_at = _utc_timestamp(now)

    if target_status is PublicationStatus.PUBLISHED and media_id:
        canonical_ids = [normalize_artwork_id(value) for value in artwork_ids]
        if not canonical_ids:
            raise ValueError("Publication reconciliation requires artwork IDs")
        for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
            history, etag = load_history_with_etag()
            original_history = copy.deepcopy(history)
            publication_id, records = _publication_records(history, canonical_ids)
            current = _uniform_status(records)
            if expected_status is not None and current is not expected_status:
                if current is PublicationStatus.PUBLISHED:
                    return 0
                raise RuntimeError(
                    "Publication state changed while reconciliation was in progress"
                )
            publication_types = {
                str(record.get("publication_type", "single")).casefold()
                for record in records
            }
            if len(publication_types) != 1:
                raise RuntimeError("Publication unit has inconsistent publication types")
            publication_type = next(iter(publication_types))
            theme_values = {
                record.get("theme_id", record.get("theme")) for record in records
            }
            theme = next(iter(theme_values)) if len(theme_values) == 1 else None
            content_type_values = {
                record.get("content_type") for record in records if record.get("content_type")
            }
            content_type = (
                next(iter(content_type_values))
                if len(content_type_values) == 1
                else None
            )
            _, changed = _finalize_publication_history(
                history,
                canonical_ids,
                media_id.strip(),
                publication_type,
                publication_id,
                theme,
                content_type,
                allowed_statuses=frozenset(
                    {PublicationStatus.PUBLISHING, PublicationStatus.AMBIGUOUS}
                ),
            )
            if not changed:
                return 0
            for record in records:
                record["last_reconciled_at"] = reconciled_at
                record["reconciliation_attempt_count"] = int(
                    record.get("reconciliation_attempt_count", 0)
                ) + 1
                record["reconciliation_result"] = result
                record["reconciliation_evidence"] = evidence
            try:
                _upload_history(history, etag)
            except ConcurrentWriteError:
                _restore_history_snapshot_in_place(history, original_history)
                if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                    raise
                logger.warning(
                    "reconciliation_history_conflict publication_id=%s retry=%s/%s",
                    publication_id,
                    attempt + 1,
                    HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
                )
                continue
            except Exception:
                _restore_history_snapshot_in_place(history, original_history)
                raise
            return len(records)
        raise AssertionError("unreachable")

    def mutation(publication_id, records):
        current = _uniform_status(records)
        if expected_status is not None and current is not expected_status:
            if current is PublicationStatus.PUBLISHED:
                return 0, False
            raise RuntimeError(
                "Publication state changed while reconciliation was in progress"
            )
        if target_status is not None and current is not target_status:
            _set_status(records, target_status, authoritative=authoritative)
        for record in records:
            record["last_reconciled_at"] = reconciled_at
            record["reconciliation_attempt_count"] = int(
                record.get("reconciliation_attempt_count", 0)
            ) + 1
            record["reconciliation_result"] = result
            record["reconciliation_evidence"] = evidence
            if target_status is PublicationStatus.AMBIGUOUS:
                record["ambiguous_at"] = record.get("ambiguous_at", reconciled_at)
                record["ambiguity_reason"] = evidence
            elif target_status is PublicationStatus.PUBLISHED:
                record["posted_at"] = record.get("posted_at", reconciled_at)
                if media_id:
                    record["media_id"] = media_id
                    record["publish_response_media_id"] = media_id
            elif target_status is PublicationStatus.EXPIRED:
                record["expired_at"] = reconciled_at
                record["expiration_reason"] = evidence
        return len(records), True

    return _conditional_publication_update(artwork_ids, mutation)

def get_grid_color_tone(read_only: bool = False) -> str:
    """Return the persisted tone; successful finalization advances rows."""
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
