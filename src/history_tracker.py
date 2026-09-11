import copy
import json
import os
import random
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import ValidationError
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence, Set, Tuple, TypeVar
from src.carousel_themes import CarouselFormat, ThemeFamily, ThemeHistorySlot
from src.engagement_features import EngagementFeatureVector
from src.carousel_policy import (
    MAX_FEATURED_WORKS,
    MAX_TOTAL_SLIDES,
    MIN_FEATURED_WORKS,
    MIN_TOTAL_SLIDES,
)
from src.models import (
    CarouselExperimentMetadata,
    PublicationRecord,
    ReelCleanupQueueEntry,
    ReelPublicationRecord,
    ReelPublicationStatus,
    ReelReleaseIdentity,
    ReelReservationRecord,
    ValidatedReelHistory,
    normalize_artwork_id,
)
from src import r2_media

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HISTORY_OBJECT_KEY = "posted_history.json"
# Must exceed the workflow's 45-minute hard timeout and normal publish duration.
PENDING_RESERVATION_TTL = timedelta(hours=2)
HISTORY_CONDITIONAL_WRITE_ATTEMPTS = 3
STAGING_MEDIA_CLEANUP_QUEUE_KEY = "staging_media_cleanup_queue"
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


REEL_HISTORY_KEYS = (
    "reel_reservations",
    "reel_publications",
    "reel_publication_count",
    "reel_staging_cleanup_queue",
)


def _validated_reel_history(history: Mapping[str, Any]) -> ValidatedReelHistory:
    """Validate additive Reel state without adding defaults to legacy history."""
    if not any(key in history for key in REEL_HISTORY_KEYS):
        return ValidatedReelHistory((), (), 0, ())

    def records_for(key: str, model: Any) -> tuple[Any, ...]:
        value = history.get(key, [])
        if not isinstance(value, list):
            raise CorruptedHistoryError(f"{key} must be a list")
        records = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise CorruptedHistoryError(f"{key} entry {index} must be an object")
            try:
                records.append(model.model_validate(item))
            except ValidationError as exc:
                raise CorruptedHistoryError(
                    f"Malformed {key} entry {index}: {exc}"
                ) from exc
        return tuple(records)

    reservations = records_for("reel_reservations", ReelReservationRecord)
    publications = records_for("reel_publications", ReelPublicationRecord)
    cleanup_queue = records_for("reel_staging_cleanup_queue", ReelCleanupQueueEntry)

    count = history.get("reel_publication_count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CorruptedHistoryError("reel_publication_count must be a non-negative integer")
    if publications and "reel_publication_count" not in history:
        raise CorruptedHistoryError("reel_publication_count is required for Reel publications")
    if count != len(publications):
        raise CorruptedHistoryError(
            "reel_publication_count must equal the number of reel_publications"
        )

    reservation_ids: set[str] = set()
    reservations_by_id: dict[str, ReelReservationRecord] = {}
    for reservation in reservations:
        if reservation.publication_id in reservation_ids:
            raise CorruptedHistoryError(
                f"Duplicate Reel reservation ID: {reservation.publication_id}"
            )
        reservation_ids.add(reservation.publication_id)
        reservations_by_id[reservation.publication_id] = reservation

    publication_ids: set[str] = set()
    media_ids: set[str] = set()
    for publication in publications:
        if publication.id in publication_ids:
            raise CorruptedHistoryError(f"Duplicate Reel publication ID: {publication.id}")
        if publication.media_id in media_ids:
            raise CorruptedHistoryError(
                f"Duplicate Reel publication media ID: {publication.media_id}"
            )
        publication_ids.add(publication.id)
        media_ids.add(publication.media_id)
        reservation = reservations_by_id.get(publication.id)
        if reservation is not None and (
            reservation.status is not ReelPublicationStatus.PUBLISHED
            or normalize_artwork_id(reservation.artwork_id)
            != normalize_artwork_id(publication.artwork_id)
            or reservation.release_identity != publication.release_identity
            or reservation.media_id != publication.media_id
        ):
            raise CorruptedHistoryError(
                f"Reel reservation/publication identity mismatch: {publication.id}"
            )

    queue_ids: set[str] = set()
    for entry in cleanup_queue:
        if entry.publication_id in queue_ids:
            raise CorruptedHistoryError(
                f"Duplicate Reel cleanup publication: {entry.publication_id}"
            )
        queue_ids.add(entry.publication_id)

    # Existing feed validation is intentionally unchanged; it is only invoked
    # when additive Reel state needs cross-format collision validation.
    feed_publications = _validated_publications(history)
    feed_ids = {publication["id"] for publication in feed_publications}
    feed_media_ids = {publication["media_id"] for publication in feed_publications}
    if publication_ids & feed_ids:
        raise CorruptedHistoryError("Reel publication ID conflicts with feed publication")
    if media_ids & feed_media_ids:
        raise CorruptedHistoryError("Reel publication media ID conflicts with feed publication")

    return ValidatedReelHistory(reservations, publications, count, cleanup_queue)


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


def _validated_staging_media_cleanup_queue(
    history: Dict[str, Any],
) -> list[Dict[str, str]]:
    value = history.get(STAGING_MEDIA_CLEANUP_QUEUE_KEY, [])
    if not isinstance(value, list):
        raise CorruptedHistoryError("Staging-media cleanup queue must be a list")
    validated: list[Dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise CorruptedHistoryError(
                f"Staging-media cleanup entry {index} must be an object"
            )
        publication_id = entry.get("publication_id")
        eligible_at = entry.get("eligible_at")
        reason = entry.get("reason")
        if (
            not r2_media.is_valid_publication_id(publication_id)
            or _parse_reserved_at(eligible_at) is None
            or not isinstance(reason, str)
            or not reason
        ):
            raise CorruptedHistoryError(
                f"Malformed staging-media cleanup entry at index {index}"
            )
        if publication_id in seen:
            raise CorruptedHistoryError(
                f"Duplicate staging-media cleanup publication: {publication_id}"
            )
        seen.add(publication_id)
        validated.append(entry)
    return validated


def _enqueue_staging_media_cleanup(
    history: Dict[str, Any],
    publication_id: str,
    records: Sequence[Dict[str, Any]],
    *,
    eligible_at: str,
    reason: str,
) -> bool:
    """Durably queue only an explicit, application-owned publication ID."""
    if (
        not r2_media.is_valid_publication_id(publication_id)
        or not records
        or any(record.get("publication_id") != publication_id for record in records)
    ):
        logger.warning(
            "staging_media_cleanup_not_queued publication_id=%s "
            "reason=legacy_or_malformed_ownership",
            publication_id,
        )
        return False
    queue = _validated_staging_media_cleanup_queue(history)
    if any(entry["publication_id"] == publication_id for entry in queue):
        return False
    history.setdefault(STAGING_MEDIA_CLEANUP_QUEUE_KEY, []).append(
        {
            "publication_id": publication_id,
            "eligible_at": eligible_at,
            "reason": reason,
        }
    )
    return True


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
    *,
    history_mutation: Callable[
        [Dict[str, Any], str, list[Dict[str, Any]]], None
    ]
    | None = None,
) -> _MutationResult:
    """Reload and re-evaluate bounded lifecycle writes after an ETag conflict."""
    canonical_ids = tuple(normalize_artwork_id(value) for value in artwork_ids)
    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        publication_id, records = _publication_records(history, canonical_ids)
        try:
            result, changed = mutation(publication_id, records)
            if changed and history_mutation is not None:
                history_mutation(history, publication_id, records)
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        if not changed:
            return result
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
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
            _restore_history_snapshot_in_place(history, original_history)
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
    original_history = copy.deepcopy(history)
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
    try:
        for records in groups.values():
            if records and all(
                _is_stale_pending(item, recovery_time) for item in records
            ):
                _set_status(records, PublicationStatus.EXPIRED, authoritative=True)
                publication_id = _publication_key(records[0])
                for item in records:
                    item["expired_at"] = expired_at
                    item["expiration_reason"] = (
                        "pending_ttl_expired_before_publish_boundary"
                    )
                _enqueue_staging_media_cleanup(
                    history,
                    publication_id,
                    records,
                    eligible_at=expired_at,
                    reason="pending_ttl_expired_before_publish_boundary",
                )
                recovered += len(records)
    except Exception:
        _restore_history_snapshot_in_place(history, original_history)
        raise

    if recovered:
        try:
            _upload_history(history, etag)
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        logger.info(f"Marked {recovered} stale reservation(s) as EXPIRED.")

    return recovered

def _is_stale_reel_pending(
    reservation: ReelReservationRecord, now: datetime
) -> bool:
    return (
        reservation.status is ReelPublicationStatus.PENDING
        and now.astimezone(timezone.utc) - _parse_reserved_at(reservation.reserved_at)
        >= PENDING_RESERVATION_TTL
    )


def globally_protected_artwork_ids(
    history: Mapping[str, Any], *, now: datetime
) -> set[str]:
    """Return the shared feed/Reel duplicate lock set for one history snapshot."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Global protection time must be timezone-aware")
    current_time = now.astimezone(timezone.utc)
    reel_history = _validated_reel_history(history)
    protected: set[str] = set()

    posted_list = history.get("posted_artworks", [])
    if isinstance(posted_list, list):
        for item in posted_list:
            if not isinstance(item, Mapping):
                continue
            artwork_id = item.get("id")
            if not isinstance(artwork_id, str):
                continue
            status = str(item.get("status", "")).upper()
            if status == PublicationStatus.EXPIRED.value:
                continue
            if status == PublicationStatus.PENDING.value and _is_stale_pending(item, current_time):
                continue
            protected.add(normalize_artwork_id(artwork_id))

    # Preserve legacy feed reads: a malformed forward index never suppresses a
    # valid legacy artwork lock, while each individually valid feed publication
    # still contributes its proven artwork IDs.
    publications = history.get("publications", [])
    if isinstance(publications, list):
        for publication in publications:
            if not isinstance(publication, Mapping):
                continue
            try:
                validated = PublicationRecord.model_validate(publication)
            except ValidationError:
                continue
            protected.update(normalize_artwork_id(value) for value in validated.artwork_ids)

    for reservation in reel_history.reservations:
        if reservation.status is ReelPublicationStatus.EXPIRED:
            continue
        if _is_stale_reel_pending(reservation, current_time):
            continue
        protected.add(normalize_artwork_id(reservation.artwork_id))
    protected.update(
        normalize_artwork_id(publication.artwork_id)
        for publication in reel_history.publications
    )
    return protected


def artwork_is_globally_protected(
    history: Mapping[str, Any], artwork_id: str, *, now: datetime
) -> bool:
    return normalize_artwork_id(artwork_id) in globally_protected_artwork_ids(
        history, now=now
    )


def get_posted_ids() -> Set[str]:
    """Return the shared feed/Reel set protected from automatic reuse."""
    history, _ = load_history_with_etag()
    return globally_protected_artwork_ids(history, now=datetime.now(timezone.utc))

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
    publication_metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    artwork_id = normalize_artwork_id(artwork_data["id"])
    engagement_features = EngagementFeatureVector.from_candidate(artwork_data)
    canonical_features = engagement_features.model_dump(exclude_none=True)
    record = {
        "id": artwork_id,
        "title": artwork_data.get("title"),
        "artist": artwork_data.get("artist"),
        "museum_name": artwork_data.get("museum"),
        "artist_name": artwork_data.get("artist"),
        "visual_category": artwork_data.get("visual_category", "other"),
        "medium": artwork_data.get("medium", "other"),
        "period": artwork_data.get("period", "unknown"),
        "period_or_style": engagement_features.period_or_style or "UNKNOWN",
        "style_or_period": engagement_features.period_or_style or "UNKNOWN",
        "region": artwork_data.get("region", "unknown"),
        "artist_group": engagement_features.artist_group or "UNKNOWN",
        "published_orientation": engagement_features.orientation or "UNKNOWN",
        "orientation": engagement_features.orientation or "UNKNOWN",
        "normalized_artist_key": engagement_features.artist_group,
        "semantic_family": engagement_features.semantic_family or "UNKNOWN",
        "visual_tone": engagement_features.luminance_bucket or "UNKNOWN",
        "luminance_bucket": engagement_features.luminance_bucket or "UNKNOWN",
        "visual_color_family": engagement_features.dominant_color or "UNKNOWN",
        "dominant_color": engagement_features.dominant_color or "UNKNOWN",
        "engagement_features": canonical_features,
        "quality_score": artwork_data.get("quality_score"),
        "measurement_coverage": artwork_data.get("measurement_coverage"),
        "selection_score": artwork_data.get("selection_score"),
        "learned_score": artwork_data.get("learned_score"),
        "engagement_confidence": artwork_data.get("engagement_confidence"),
        "quality_component": artwork_data.get("quality_component"),
        "engagement_component": artwork_data.get("engagement_component"),
        "diversity_component": artwork_data.get("diversity_component"),
        "exploration_component": artwork_data.get("exploration_component"),
        "exploration_selected": artwork_data.get("exploration_selected"),
        "engagement_applied": artwork_data.get("engagement_applied", False),
        "source": artwork_data.get("source"),
        "artwork_url": artwork_data.get("artwork_url"),
        "credit_line": artwork_data.get("credit_line"),
        "license": artwork_data.get("license"),
        "is_public_domain": artwork_data.get("is_public_domain"),
        "rights_status": artwork_data.get("rights_status"),
        "rights_text": artwork_data.get("rights_text"),
        "copyright_notice": artwork_data.get("copyright_notice"),
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
        if publication_role == "COVER" and publication_metadata is not None:
            record["publication_metadata"] = dict(publication_metadata)
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

    requested_ids = set(artwork_ids)
    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        now = datetime.now(timezone.utc)
        try:
            protected = globally_protected_artwork_ids(history, now=now)
            collision = requested_ids & protected
            if collision:
                raise RuntimeError(
                    f"Artwork {sorted(collision)[0]} is already protected by history"
                )

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
                    str(item.get("status", "")).upper()
                    == PublicationStatus.EXPIRED.value
                    or _is_stale_pending(item, now)
                ):
                    continue
                raise RuntimeError(
                    f"Artwork {canonical_id} is already protected by history"
                )

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
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            logger.warning(
                "feed_reservation_history_conflict publication_id=%s retry=%s/%s",
                stable_publication_id,
                attempt + 1,
                HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
            )
            continue
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
    raise AssertionError("unreachable")


def reserve_artwork(
    artwork_data: Dict[str, Any], publication_id: str | None = None
) -> str:
    """Backward-compatible one-artwork reservation helper."""
    return reserve_artworks([artwork_data], "single", publication_id)


def reserve_reel(
    artwork_id: str,
    release_identity: ReelReleaseIdentity,
    publication_id: str | None = None,
) -> str:
    """Atomically reserve one Reel while protecting the shared artwork pool."""
    if not isinstance(artwork_id, str) or not artwork_id.strip():
        raise ValueError("Reel artwork ID must be a nonempty string")
    canonical_artwork_id = normalize_artwork_id(artwork_id.strip())
    if not isinstance(release_identity, ReelReleaseIdentity):
        raise TypeError("release_identity must be a ReelReleaseIdentity")
    if release_identity.reel_id != canonical_artwork_id:
        raise ValueError("Reel release identity must match the canonical artwork ID")

    stable_publication_id = publication_id or str(uuid.uuid4())
    try:
        stable_publication_id = str(UUID(stable_publication_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Reel publication ID must be a UUID") from exc

    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        now = datetime.now(timezone.utc)
        try:
            reel_history = _validated_reel_history(history)
            existing = next(
                (
                    reservation
                    for reservation in reel_history.reservations
                    if reservation.publication_id == stable_publication_id
                ),
                None,
            )
            if existing is not None:
                if (
                    normalize_artwork_id(existing.artwork_id) == canonical_artwork_id
                    and existing.release_identity == release_identity
                ):
                    return stable_publication_id
                raise RuntimeError("Reel reservation replay has conflicting identity")

            if artwork_is_globally_protected(history, canonical_artwork_id, now=now):
                raise RuntimeError(
                    f"Artwork {canonical_artwork_id} is already protected by history"
                )

            history.setdefault("reel_reservations", []).append(
                {
                    "publication_id": stable_publication_id,
                    "artwork_id": canonical_artwork_id,
                    "status": ReelPublicationStatus.PENDING.value,
                    "reserved_at": _utc_timestamp(now),
                    "release_identity": release_identity.model_dump(mode="json"),
                }
            )
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            logger.warning(
                "reel_reservation_history_conflict publication_id=%s retry=%s/%s",
                stable_publication_id,
                attempt + 1,
                HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
            )
            continue
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        logger.info("Reserved Reel artwork %s in R2 history (PENDING).", canonical_artwork_id)
        return stable_publication_id
    raise AssertionError("unreachable")


def _reel_lifecycle_inputs(
    publication_id: str, release_identity: ReelReleaseIdentity
) -> str:
    """Validate immutable identity inputs shared by Reel lifecycle mutations."""
    if not isinstance(publication_id, str):
        raise ValueError("Reel publication ID must be a UUID")
    try:
        normalized_publication_id = str(UUID(publication_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Reel publication ID must be a UUID") from exc
    if not isinstance(release_identity, ReelReleaseIdentity):
        raise TypeError("release_identity must be a ReelReleaseIdentity")
    return normalized_publication_id


def _conditional_reel_update(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    mutation: Callable[
        [
            Dict[str, Any],
            ValidatedReelHistory,
            ReelReservationRecord,
            Dict[str, Any],
        ],
        tuple[_MutationResult, bool],
    ],
) -> _MutationResult:
    """Run one Reel lifecycle mutation under bounded reload-and-CAS semantics."""
    normalized_publication_id = _reel_lifecycle_inputs(
        publication_id, release_identity
    )
    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        try:
            reel_history = _validated_reel_history(history)
            reservation = next(
                (
                    record
                    for record in reel_history.reservations
                    if record.publication_id == normalized_publication_id
                ),
                None,
            )
            if reservation is None:
                raise RuntimeError(
                    f"Missing Reel reservation: {normalized_publication_id}"
                )
            if reservation.release_identity != release_identity:
                raise RuntimeError("Reel lifecycle replay has conflicting release identity")

            raw_reservations = history.get("reel_reservations")
            if not isinstance(raw_reservations, list):
                raise CorruptedHistoryError("reel_reservations must be a list")
            raw_reservation = next(
                (
                    record
                    for record in raw_reservations
                    if isinstance(record, dict)
                    and record.get("publication_id") == reservation.publication_id
                ),
                None,
            )
            if raw_reservation is None:
                raise CorruptedHistoryError(
                    "Validated Reel reservation is not mutable history state"
                )

            result, changed = mutation(
                history, reel_history, reservation, raw_reservation
            )
            if changed:
                # Validate the complete additive state before committing it.
                _validated_reel_history(history)
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        if not changed:
            return result
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            logger.warning(
                "reel_lifecycle_history_conflict publication_id=%s retry=%s/%s",
                normalized_publication_id,
                attempt + 1,
                HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
            )
            continue
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        return result
    raise AssertionError("unreachable")


def get_reel_reservation(publication_id: str) -> ReelReservationRecord:
    """Return one validated durable Reel lifecycle record."""
    if not isinstance(publication_id, str):
        raise ValueError("Reel publication ID must be a UUID")
    try:
        normalized_publication_id = str(UUID(publication_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Reel publication ID must be a UUID") from exc
    history, _ = load_history_with_etag()
    reel_history = _validated_reel_history(history)
    reservation = next(
        (
            record
            for record in reel_history.reservations
            if record.publication_id == normalized_publication_id
        ),
        None,
    )
    if reservation is None:
        raise RuntimeError(f"Missing Reel reservation: {normalized_publication_id}")
    return reservation


def record_reel_staging(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    upload: r2_media.TempReelUpload,
    *,
    now: datetime | None = None,
) -> ReelReservationRecord:
    """Persist a validated Reel R2 handle while the reservation is PENDING."""
    normalized_publication_id = _reel_lifecycle_inputs(
        publication_id, release_identity
    )
    if not isinstance(upload, r2_media.TempReelUpload):
        raise TypeError("upload must be a TempReelUpload")
    if upload.publication_id != normalized_publication_id:
        raise RuntimeError("Reel staging upload is not owned by this publication")
    r2_media.validate_owned_reel_object_key(upload.object_key, normalized_publication_id)
    staged_at = _utc_timestamp(now)

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status is not ReelPublicationStatus.PENDING:
            raise RuntimeError("Reel staging may only be recorded while PENDING")
        existing = reservation.staging
        if existing is not None:
            if (
                existing.object_key == upload.object_key
                and existing.public_url == upload.public_url
            ):
                return reservation, False
            raise RuntimeError("Reel reservation already has conflicting staging")
        raw_reservation["staging"] = {
            "object_key": upload.object_key,
            "public_url": upload.public_url,
            "staged_at": staged_at,
        }
        return ReelReservationRecord.model_validate(raw_reservation), True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def start_reel_publication_attempt(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    container_id: str,
    *,
    now: datetime | None = None,
) -> ReelReservationRecord:
    """Durably cross the Reel media_publish boundary after staging succeeds."""
    _reel_lifecycle_inputs(publication_id, release_identity)
    if not isinstance(container_id, str) or not container_id.strip():
        raise ValueError("A non-empty Reel container ID is required")
    container_id = container_id.strip()
    started_at = _utc_timestamp(now)

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status is ReelPublicationStatus.PUBLISHING:
            if reservation.container_id == container_id:
                return reservation, False
            raise RuntimeError(
                "Reel reservation is already crossing a different publish boundary"
            )
        if reservation.status is not ReelPublicationStatus.PENDING:
            raise RuntimeError(
                f"Cannot start Reel publication from {reservation.status.value}"
            )
        if reservation.staging is None:
            raise RuntimeError("Reel staging must be durable before PUBLISHING")
        raw_reservation["status"] = ReelPublicationStatus.PUBLISHING.value
        raw_reservation["container_id"] = container_id
        raw_reservation["publish_started_at"] = started_at
        return ReelReservationRecord.model_validate(raw_reservation), True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def record_reel_publish_response(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    media_id: str,
) -> ReelReservationRecord:
    """Persist the media_publish receipt before any Reel finalization."""
    _reel_lifecycle_inputs(publication_id, release_identity)
    if not isinstance(media_id, str) or not media_id.strip():
        raise ValueError("A non-empty Instagram media ID is required")
    media_id = media_id.strip()

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status is ReelPublicationStatus.PUBLISHED:
            _published_reel_replay_publication(
                reel_history,
                reservation,
                reservation.publication_id,
                release_identity,
                media_id,
            )
            return reservation, False
        if reservation.status not in {
            ReelPublicationStatus.PUBLISHING,
            ReelPublicationStatus.AMBIGUOUS,
        }:
            raise RuntimeError(
                f"Cannot record Reel publish response from {reservation.status.value}"
            )
        if reservation.publish_response_media_id is not None:
            if reservation.publish_response_media_id == media_id:
                return reservation, False
            raise RuntimeError("Reel reservation has conflicting media receipt")
        raw_reservation["publish_response_media_id"] = media_id
        return ReelReservationRecord.model_validate(raw_reservation), True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def _published_reel_replay_publication(
    reel_history: ValidatedReelHistory,
    reservation: ReelReservationRecord,
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    media_id: str,
) -> ReelPublicationRecord:
    """Require a PUBLISHED reservation to have one exact durable success record."""
    if (
        reservation.media_id != media_id
        or reservation.publish_response_media_id != media_id
    ):
        raise CorruptedHistoryError(
            "Published Reel reservation has conflicting media identity"
        )
    publication = next(
        (
            record
            for record in reel_history.publications
            if record.id == publication_id
        ),
        None,
    )
    if publication is None:
        raise CorruptedHistoryError("Published Reel reservation has no publication")
    if (
        publication.media_id != media_id
        or normalize_artwork_id(publication.artwork_id)
        != normalize_artwork_id(reservation.artwork_id)
        or publication.release_identity != release_identity
    ):
        raise CorruptedHistoryError(
            "Published Reel reservation has conflicting publication identity"
        )
    return publication


def mark_reel_ambiguous(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    reason: str,
    *,
    now: datetime | None = None,
    reconciliation_result: str | None = None,
    reconciliation_evidence: str | None = None,
) -> ReelReservationRecord:
    """Quarantine a crossed Reel publish boundary without permitting replay."""
    _reel_lifecycle_inputs(publication_id, release_identity)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A non-empty Reel ambiguity reason is required")
    reason = reason.strip()
    if reconciliation_result is not None and (
        not isinstance(reconciliation_result, str) or not reconciliation_result.strip()
    ):
        raise ValueError("reconciliation_result must be non-empty when supplied")
    if reconciliation_evidence is not None and (
        not isinstance(reconciliation_evidence, str) or not reconciliation_evidence.strip()
    ):
        raise ValueError("reconciliation_evidence must be non-empty when supplied")
    ambiguous_at = _utc_timestamp(now)

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status is ReelPublicationStatus.AMBIGUOUS:
            if reservation.ambiguity_reason == reason:
                return reservation, False
            raise RuntimeError("Reel reservation already has conflicting ambiguity")
        if reservation.status is not ReelPublicationStatus.PUBLISHING:
            raise RuntimeError(
                f"Cannot mark Reel reservation ambiguous from {reservation.status.value}"
            )
        raw_reservation["status"] = ReelPublicationStatus.AMBIGUOUS.value
        raw_reservation["ambiguous_at"] = ambiguous_at
        raw_reservation["ambiguity_reason"] = reason
        if reconciliation_result is not None:
            raw_reservation["reconciliation_result"] = reconciliation_result.strip()
            raw_reservation["reconciliation_evidence"] = reconciliation_evidence.strip() if reconciliation_evidence else None
            raw_reservation["last_reconciled_at"] = ambiguous_at
            raw_reservation["reconciliation_attempt_count"] = 1
        return ReelReservationRecord.model_validate(raw_reservation), True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def finalize_reel_publication(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    media_id: str,
    *,
    permalink: str | None = None,
    now: datetime | None = None,
    reconciliation_result: str | None = None,
    reconciliation_evidence: str | None = None,
) -> ReelPublicationRecord:
    """Atomically record one proven Reel without touching feed lifecycle state."""
    normalized_publication_id = _reel_lifecycle_inputs(
        publication_id, release_identity
    )
    if not isinstance(media_id, str) or not media_id.strip():
        raise ValueError("A non-empty Instagram media ID is required")
    media_id = media_id.strip()
    posted_at = _utc_timestamp(now)

    def mutation(history, reel_history, reservation, raw_reservation):
        feed_publications = _validated_publications(history)
        _grid_publication_count(history, feed_publications)
        if reservation.status is ReelPublicationStatus.PUBLISHED:
            existing = _published_reel_replay_publication(
                reel_history,
                reservation,
                normalized_publication_id,
                release_identity,
                media_id,
            )
            if permalink is None or existing.permalink == permalink:
                return existing, False
            raise CorruptedHistoryError(
                "Reel finalization conflicts with existing publication identity"
            )
        existing = next(
            (
                publication
                for publication in reel_history.publications
                if publication.id == normalized_publication_id
            ),
            None,
        )
        if existing is not None:
            raise CorruptedHistoryError(
                "Reel finalization conflicts with existing publication identity"
            )

        if reservation.status not in {
            ReelPublicationStatus.PUBLISHING,
            ReelPublicationStatus.AMBIGUOUS,
        }:
            raise RuntimeError(
                f"Cannot finalize Reel publication from {reservation.status.value}"
            )
        if reservation.publish_response_media_id != media_id:
            raise RuntimeError("Reel finalization requires the matching durable receipt")

        feed_ids = {publication["id"] for publication in feed_publications}
        feed_media_ids = {publication["media_id"] for publication in feed_publications}
        if normalized_publication_id in feed_ids or media_id in feed_media_ids:
            raise CorruptedHistoryError(
                "Reel finalization conflicts with feed publication identity"
            )
        if any(publication.media_id == media_id for publication in reel_history.publications):
            raise CorruptedHistoryError(
                "Reel finalization conflicts with existing Reel media identity"
            )

        raw_reservation["status"] = ReelPublicationStatus.PUBLISHED.value
        raw_reservation["media_id"] = media_id
        raw_reservation["publish_response_media_id"] = media_id
        raw_reservation["posted_at"] = posted_at
        raw_reservation.pop("ambiguous_at", None)
        raw_reservation.pop("ambiguity_reason", None)
        if permalink is not None:
            raw_reservation["permalink"] = permalink
        if reconciliation_result is not None:
            raw_reservation["reconciliation_result"] = reconciliation_result
        if reconciliation_evidence is not None:
            raw_reservation["reconciliation_evidence"] = reconciliation_evidence

        publication = ReelPublicationRecord.model_validate(
            {
                "id": normalized_publication_id,
                "artwork_id": reservation.artwork_id,
                "media_id": media_id,
                "posted_at": posted_at,
                "permalink": permalink,
                "release_identity": release_identity,
            }
        )
        history.setdefault("reel_publications", []).append(
            publication.model_dump(mode="json", exclude_none=True)
        )
        history["reel_publication_count"] = reel_history.publication_count + 1
        return publication, True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def record_reel_permalink(
    publication_id: str, media_id: str, permalink: str
) -> ReelPublicationRecord:
    """Idempotently enrich an already-finalized Reel's optional permalink."""
    if not isinstance(publication_id, str):
        raise ValueError("Reel publication ID must be a UUID")
    try:
        normalized_publication_id = str(UUID(publication_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Reel publication ID must be a UUID") from exc
    if not isinstance(media_id, str) or not media_id.strip():
        raise ValueError("A non-empty Instagram media ID is required")
    media_id = media_id.strip()

    history, _ = load_history_with_etag()
    initial_reels = _validated_reel_history(history)
    reservation = next(
        (
            record
            for record in initial_reels.reservations
            if record.publication_id == normalized_publication_id
        ),
        None,
    )
    if reservation is None:
        raise RuntimeError(f"Missing Reel reservation: {normalized_publication_id}")

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status is not ReelPublicationStatus.PUBLISHED:
            raise RuntimeError("Reel permalink may only be recorded after PUBLISHED")
        if reservation.media_id != media_id:
            raise RuntimeError("Reel permalink media ID does not match reservation")
        publication = next(
            (
                record
                for record in reel_history.publications
                if record.id == normalized_publication_id
            ),
            None,
        )
        if publication is None:
            raise CorruptedHistoryError("Published Reel reservation has no publication")
        if publication.media_id != media_id:
            raise CorruptedHistoryError("Reel permalink media ID conflicts with publication")
        if reservation.permalink != publication.permalink:
            raise CorruptedHistoryError("Reel reservation/publication permalink mismatch")
        if publication.permalink is not None:
            if publication.permalink == permalink:
                return publication, False
            raise RuntimeError("Reel permalink conflicts with existing permalink")

        updated = publication.model_copy(update={"permalink": permalink})
        raw_reservation["permalink"] = permalink
        raw_publications = history.get("reel_publications")
        if not isinstance(raw_publications, list):
            raise CorruptedHistoryError("reel_publications must be a list")
        raw_publication = next(
            (
                record
                for record in raw_publications
                if isinstance(record, dict) and record.get("id") == normalized_publication_id
            ),
            None,
        )
        if raw_publication is None:
            raise CorruptedHistoryError("Validated Reel publication is not mutable history state")
        raw_publication["permalink"] = permalink
        return updated, True

    return _conditional_reel_update(
        publication_id, reservation.release_identity, mutation
    )


def list_unresolved_reel_reservations(
    *,
    limit: int,
    now: datetime | None = None,
    max_age: timedelta | None = None,
    publication_id: str | None = None,
) -> list[ReelReservationRecord]:
    """Return newest-first unresolved Reel reservations from validated history."""
    if limit < 1:
        raise ValueError("Reel reconciliation limit must be positive")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("Reconciliation time must be timezone-aware")
    reference_time = reference_time.astimezone(timezone.utc)
    if publication_id is not None:
        try:
            publication_id = str(UUID(publication_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Reel publication ID must be a UUID") from exc

    history, _ = load_history_with_etag()
    reel_history = _validated_reel_history(history)
    unresolved = [
        reservation
        for reservation in reel_history.reservations
        if reservation.status
        in {
            ReelPublicationStatus.PENDING,
            ReelPublicationStatus.PUBLISHING,
            ReelPublicationStatus.AMBIGUOUS,
        }
        and (publication_id is None or reservation.publication_id == publication_id)
    ]

    def lifecycle_time(reservation: ReelReservationRecord) -> datetime:
        value = (
            reservation.publish_started_at
            or reservation.ambiguous_at
            or reservation.reserved_at
        )
        parsed = _parse_reserved_at(value)
        if parsed is None:
            raise CorruptedHistoryError("Reel reservation lifecycle timestamp is invalid")
        return parsed

    unresolved.sort(key=lifecycle_time, reverse=True)
    if max_age is not None:
        unresolved = [
            reservation
            for reservation in unresolved
            if reservation.status is ReelPublicationStatus.AMBIGUOUS
            or reference_time - lifecycle_time(reservation) <= max_age
        ]
    return unresolved[:limit]


def expire_reel_before_media_publish(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    *,
    reason: str,
    expected_status: ReelPublicationStatus,
    expected_container_id: str | None = None,
    now: datetime | None = None,
) -> ReelReservationRecord:
    """Atomically expire proven pre-Meta work and enqueue only its Reel prefix."""
    _reel_lifecycle_inputs(publication_id, release_identity)
    if expected_status not in {
        ReelPublicationStatus.PENDING,
        ReelPublicationStatus.PUBLISHING,
    }:
        raise ValueError("Only PENDING or PUBLISHING Reel work may expire pre-Meta")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Reel expiration reason must be non-empty")
    if expected_status is ReelPublicationStatus.PUBLISHING and (
        not isinstance(expected_container_id, str) or not expected_container_id.strip()
    ):
        raise ValueError("PUBLISHING expiry requires the exact durable container ID")
    if expected_status is ReelPublicationStatus.PENDING and expected_container_id is not None:
        raise ValueError("PENDING expiry must not include a container ID")
    expired_at = _utc_timestamp(now)
    normalized_reason = reason.strip()

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status is not expected_status:
            raise RuntimeError(
                f"Cannot expire Reel reservation from {reservation.status.value}"
            )
        if expected_status is ReelPublicationStatus.PUBLISHING and (
            reservation.container_id != expected_container_id.strip()
        ):
            raise RuntimeError("Reel expiry container ID does not match durable boundary")
        if reservation.publish_response_media_id is not None:
            raise RuntimeError("Cannot expire Reel reservation with a durable publish receipt")
        raw_reservation["status"] = ReelPublicationStatus.EXPIRED.value
        raw_reservation["expired_at"] = expired_at
        raw_reservation["expiration_reason"] = normalized_reason
        queue = history.setdefault("reel_staging_cleanup_queue", [])
        if not isinstance(queue, list):
            raise CorruptedHistoryError("reel_staging_cleanup_queue must be a list")
        if not any(
            isinstance(entry, dict)
            and entry.get("publication_id") == reservation.publication_id
            for entry in queue
        ):
            queue.append(
                {
                    "publication_id": reservation.publication_id,
                    "eligible_at": expired_at,
                    "reason": normalized_reason,
                }
            )
        return ReelReservationRecord.model_validate(raw_reservation), True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def record_reel_reconciliation_evidence(
    publication_id: str,
    release_identity: ReelReleaseIdentity,
    *,
    result: str,
    evidence: str,
    now: datetime | None = None,
) -> ReelReservationRecord:
    """Append bounded reconciliation evidence without reopening lifecycle state."""
    _reel_lifecycle_inputs(publication_id, release_identity)
    if not isinstance(result, str) or not result.strip():
        raise ValueError("Reel reconciliation result must be non-empty")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("Reel reconciliation evidence must be non-empty")
    reconciled_at = _utc_timestamp(now)

    def mutation(history, reel_history, reservation, raw_reservation):
        if reservation.status not in {
            ReelPublicationStatus.PENDING,
            ReelPublicationStatus.PUBLISHING,
            ReelPublicationStatus.AMBIGUOUS,
        }:
            raise RuntimeError("Only unresolved Reel reservations may be reconciled")
        raw_reservation["last_reconciled_at"] = reconciled_at
        raw_reservation["reconciliation_attempt_count"] = (
            reservation.reconciliation_attempt_count or 0
        ) + 1
        raw_reservation["reconciliation_result"] = result.strip()
        raw_reservation["reconciliation_evidence"] = evidence.strip()
        return ReelReservationRecord.model_validate(raw_reservation), True

    return _conditional_reel_update(publication_id, release_identity, mutation)


def list_reel_staging_cleanup_publication_ids(*, limit: int) -> list[str]:
    """Return only expired Reel cleanup prefixes; active or published state wins."""
    if limit < 1:
        raise ValueError("Reel staging cleanup limit must be positive")
    history, _ = load_history_with_etag()
    reel_history = _validated_reel_history(history)
    publications = {publication.id for publication in reel_history.publications}
    reservation_statuses = {
        reservation.publication_id: reservation.status
        for reservation in reel_history.reservations
    }
    return [
        entry.publication_id
        for entry in reel_history.cleanup_queue
        if entry.publication_id not in publications
        and reservation_statuses.get(entry.publication_id) is ReelPublicationStatus.EXPIRED
    ][:limit]


def acknowledge_reel_staging_cleanup(publication_id: str) -> bool:
    """CAS-remove one completed Reel cleanup entry without touching feed queues."""
    try:
        normalized_publication_id = str(UUID(publication_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Reel publication ID must be a UUID") from exc
    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        try:
            reel_history = _validated_reel_history(history)
            if not any(
                entry.publication_id == normalized_publication_id
                for entry in reel_history.cleanup_queue
            ):
                return False
            queue = history.get("reel_staging_cleanup_queue")
            if not isinstance(queue, list):
                raise CorruptedHistoryError("reel_staging_cleanup_queue must be a list")
            history["reel_staging_cleanup_queue"] = [
                entry
                for entry in queue
                if not isinstance(entry, dict)
                or entry.get("publication_id") != normalized_publication_id
            ]
            _validated_reel_history(history)
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            continue
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        return True
    raise AssertionError("unreachable")


def reserve_carousel(
    cover_artwork: Dict[str, Any],
    featured_artworks: Sequence[Dict[str, Any]],
    *,
    theme_id: str | None = None,
    theme_family: str | None = None,
    carousel_format: str | None = None,
    publication_metadata: Mapping[str, Any] | None = None,
) -> str:
    """Atomically reserve one cover and 5–8 featured works with explicit roles."""
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

    validated_publication_metadata = None
    if publication_metadata is not None:
        try:
            validated_publication_metadata = CarouselExperimentMetadata.model_validate(
                publication_metadata
            )
        except ValidationError as error:
            raise ValueError("Invalid carousel experiment metadata") from error
        if validated_publication_metadata.featured_count != len(featured_artworks):
            raise ValueError(
                "Carousel experiment featured_count must match reserved artworks"
            )

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
            publication_metadata=(
                validated_publication_metadata.model_dump(exclude_none=True)
                if validated_publication_metadata
                else None
            ),
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

    def queue_cleanup(history, publication_id, records):
        if authoritative:
            _enqueue_staging_media_cleanup(
                history,
                publication_id,
                records,
                eligible_at=expired_at,
                reason=reason,
            )

    return _conditional_publication_update(
        artwork_ids, mutation, history_mutation=queue_cleanup
    )


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
    permalink: str | None = None,
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
    experiment_metadata: dict[str, Any] = {}
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
        cover_record = next(
            record for record in role_records if record.get("publication_role") == "COVER"
        )
        if theme is None and isinstance(cover_record.get("theme_id"), str):
            theme = cover_record["theme_id"]
        if content_type is None:
            content_type = "CAROUSEL"
        raw_metadata = cover_record.get("publication_metadata")
        if raw_metadata is not None:
            try:
                validated_metadata = CarouselExperimentMetadata.model_validate(
                    raw_metadata
                )
            except ValidationError as error:
                raise CorruptedHistoryError(
                    "Carousel reservation has malformed experiment metadata"
                ) from error
            if validated_metadata.featured_count != len(featured):
                raise CorruptedHistoryError(
                    "Carousel experiment metadata does not match reserved roles"
                )
            experiment_metadata = validated_metadata.model_dump(exclude_none=True)

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
        if permalink is not None:
            existing_permalink = existing_publication.get("permalink")
            if existing_permalink is None:
                PublicationRecord.model_validate(
                    {**existing_publication, "permalink": permalink}
                )
                for item in target_records:
                    artwork_permalink = item.get("permalink")
                    if artwork_permalink is not None and artwork_permalink != permalink:
                        raise CorruptedHistoryError(
                            f"Conflicting publication permalink: {stable_publication_id}"
                        )
                existing_publication["permalink"] = permalink
                for item in target_records:
                    item["permalink"] = permalink
                return existing_publication, True
            if existing_permalink != permalink:
                raise CorruptedHistoryError(
                    f"Conflicting publication permalink: {stable_publication_id}"
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
        permalink=permalink,
        **experiment_metadata,
    ).model_dump(exclude_none=True)
    for item in target_records:
        item["status"] = PublicationStatus.PUBLISHED.value
        item["media_id"] = media_id
        item["publish_response_media_id"] = media_id
        item["posted_at"] = posted_at
        item["publication_id"] = stable_publication_id
        item["publication_type"] = publication_type
        if permalink is not None:
            item["permalink"] = permalink

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
    permalink: str | None = None,
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
            permalink,
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
    *,
    permalink: str | None = None,
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

    finalization_kwargs = {"permalink": permalink} if permalink is not None else {}
    publication = confirm_artworks_and_record_publication(
        [cover_id, *featured_ids], media_id, "carousel", **finalization_kwargs
    )
    return len(publication["artwork_ids"])


def list_unresolved_publication_units(
    *,
    limit: int,
    now: datetime | None = None,
    max_age: timedelta | None = None,
    publication_id: str | None = None,
) -> list[PublicationUnit]:
    """Return bounded unresolved publication units, newest first."""
    if limit < 1:
        raise ValueError("Publication reconciliation limit must be positive")
    if publication_id is not None:
        if (
            not isinstance(publication_id, str)
            or not publication_id
            or publication_id != publication_id.strip()
        ):
            raise ValueError("Publication ID must be a non-empty trimmed string")
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
        } and (publication_id is None or _publication_key(item) == publication_id):
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


def list_staging_media_cleanup_publication_ids(*, limit: int) -> list[str]:
    """Return a bounded queue of authoritatively expired publication prefixes.

    Active or published state always wins over a stale queue entry. Malformed queue
    state fails closed and yields no destructive work.
    """
    if limit < 1:
        raise ValueError("Staging-media cleanup limit must be positive")
    history, _ = load_history_with_etag()
    try:
        queue = _validated_staging_media_cleanup_queue(history)
    except CorruptedHistoryError:
        logger.exception("staging_media_cleanup_queue_invalid result=keep_all")
        return []

    blocked_publication_ids = {
        item["publication_id"]
        for item in history.get("posted_artworks", [])
        if isinstance(item, dict)
        and r2_media.is_valid_publication_id(item.get("publication_id"))
        and str(item.get("status", "")).upper()
        != PublicationStatus.EXPIRED.value
    }
    for publication in history.get("publications", []):
        if isinstance(publication, dict) and r2_media.is_valid_publication_id(
            publication.get("id")
        ):
            blocked_publication_ids.add(publication["id"])

    selected: list[str] = []
    for entry in queue:
        publication_id = entry["publication_id"]
        if publication_id in blocked_publication_ids:
            logger.warning(
                "staging_media_cleanup_skipped publication_id=%s "
                "reason=active_or_published_state",
                publication_id,
            )
            continue
        selected.append(publication_id)
        if len(selected) == limit:
            break
    return selected


def acknowledge_staging_media_cleanup(publication_id: str) -> bool:
    """Remove one cleanup queue entry after idempotent R2 cleanup succeeds."""
    normalized = r2_media.validate_publication_id(publication_id)
    for attempt in range(1, HISTORY_CONDITIONAL_WRITE_ATTEMPTS + 1):
        history, etag = load_history_with_etag()
        original_history = copy.deepcopy(history)
        queue = _validated_staging_media_cleanup_queue(history)
        retained = [
            entry for entry in queue if entry["publication_id"] != normalized
        ]
        if len(retained) == len(queue):
            return False
        history[STAGING_MEDIA_CLEANUP_QUEUE_KEY] = retained
        try:
            _upload_history(history, etag)
        except ConcurrentWriteError:
            _restore_history_snapshot_in_place(history, original_history)
            if attempt == HISTORY_CONDITIONAL_WRITE_ATTEMPTS:
                raise
            logger.warning(
                "staging_media_cleanup_ack_conflict publication_id=%s retry=%s/%s",
                normalized,
                attempt + 1,
                HISTORY_CONDITIONAL_WRITE_ATTEMPTS,
            )
            continue
        except Exception:
            _restore_history_snapshot_in_place(history, original_history)
            raise
        return True
    raise AssertionError("unreachable")


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
    permalink: str | None = None,
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
                permalink,
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

    def queue_cleanup(history, publication_id, records):
        if target_status is PublicationStatus.EXPIRED and authoritative:
            _enqueue_staging_media_cleanup(
                history,
                publication_id,
                records,
                eligible_at=reconciled_at,
                reason=evidence,
            )

    return _conditional_publication_update(
        artwork_ids, mutation, history_mutation=queue_cleanup
    )

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
