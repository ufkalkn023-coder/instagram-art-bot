"""Bounded, conservative reconciliation and cleanup for Reel publications."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
from urllib.parse import urlsplit

from src import history_tracker, instagram_poster, r2_media
from src.models import ReelPublicationStatus


logger = logging.getLogger(__name__)

PUBLISHING_RECONCILIATION_GRACE = timedelta(minutes=50)
STARTUP_RECONCILIATION_LIMIT = 20
STARTUP_RECONCILIATION_MAX_AGE = timedelta(days=30)


class ReelReconciliationOutcome(str, Enum):
    CONFIRMED_PUBLISHED = "CONFIRMED_PUBLISHED"
    CONFIRMED_NOT_PUBLISHED = "CONFIRMED_NOT_PUBLISHED"
    STILL_AMBIGUOUS = "STILL_AMBIGUOUS"
    RECONCILIATION_ERROR = "RECONCILIATION_ERROR"


@dataclass(frozen=True)
class ReelReconciliationResult:
    publication_id: str
    previous_status: str
    outcome: ReelReconciliationOutcome
    evidence: str


@dataclass(frozen=True)
class ReelReconciliationSummary:
    inspected: int
    confirmed_published: int
    confirmed_not_published: int
    still_ambiguous: int
    errors: int
    cleanup_inspected: int
    cleanup_deleted: int
    cleanup_failures: int
    results: tuple[ReelReconciliationResult, ...]


def _result(reservation, outcome, evidence):
    return ReelReconciliationResult(
        publication_id=reservation.publication_id,
        previous_status=reservation.status.value,
        outcome=outcome,
        evidence=evidence,
    )


def _best_effort_permalink(media_id: str, access_token: str) -> str | None:
    try:
        permalink = instagram_poster.get_instagram_permalink(media_id, access_token)
        if not isinstance(permalink, str) or permalink != permalink.strip():
            return None
        parsed = urlsplit(permalink)
        if parsed.scheme != "https" or not parsed.netloc:
            return None
        parsed.port
        return permalink
    except Exception as error:
        logger.warning("reel_permalink_lookup_failed error=%s", type(error).__name__)
        return None


def _finalize_receipt(reservation, *, access_token: str, now: datetime):
    publication = history_tracker.finalize_reel_publication(
        reservation.publication_id,
        reservation.release_identity,
        reservation.publish_response_media_id,
        now=now,
        reconciliation_result=ReelReconciliationOutcome.CONFIRMED_PUBLISHED.value,
        reconciliation_evidence="durable_media_publish_response_id",
    )
    permalink = _best_effort_permalink(publication.media_id, access_token)
    if permalink is not None:
        try:
            history_tracker.record_reel_permalink(
                publication.id, publication.media_id, permalink
            )
        except Exception as error:
            logger.warning("reel_permalink_persistence_failed error=%s", type(error).__name__)


def _mark_or_record_ambiguous(reservation, *, result, evidence, now):
    if reservation.status is ReelPublicationStatus.PUBLISHING:
        return history_tracker.mark_reel_ambiguous(
            reservation.publication_id,
            reservation.release_identity,
            "reconciliation_media_identity_unverified",
            now=now,
            reconciliation_result=result,
            reconciliation_evidence=evidence,
        )
    return history_tracker.record_reel_reconciliation_evidence(
        reservation.publication_id,
        reservation.release_identity,
        result=result,
        evidence=evidence,
        now=now,
    )


def _reconcile_reservation(reservation, *, access_token: str, now: datetime):
    if reservation.status is ReelPublicationStatus.PENDING:
        reserved_at = datetime.fromisoformat(reservation.reserved_at.replace("Z", "+00:00"))
        if now - reserved_at < history_tracker.PENDING_RESERVATION_TTL:
            return None
        history_tracker.expire_reel_before_media_publish(
            reservation.publication_id,
            reservation.release_identity,
            reason="pending_ttl_expired_before_publish_boundary",
            expected_status=ReelPublicationStatus.PENDING,
            now=now,
        )
        return _result(
            reservation,
            ReelReconciliationOutcome.CONFIRMED_NOT_PUBLISHED,
            "pending_ttl_expired_before_publish_boundary",
        )

    if (
        reservation.status is ReelPublicationStatus.PUBLISHING
        and reservation.publish_started_at is not None
        and now - datetime.fromisoformat(reservation.publish_started_at.replace("Z", "+00:00"))
        < PUBLISHING_RECONCILIATION_GRACE
    ):
        return None

    if reservation.publish_response_media_id:
        _finalize_receipt(reservation, access_token=access_token, now=now)
        return _result(
            reservation,
            ReelReconciliationOutcome.CONFIRMED_PUBLISHED,
            "durable_media_publish_response_id",
        )

    if not reservation.container_id:
        evidence = "creation_container_id_missing"
    else:
        try:
            evidence = f"container_status:{instagram_poster.get_container_status(reservation.container_id, access_token)}"
        except Exception as error:
            evidence = f"container_status_error:{type(error).__name__}"
    _mark_or_record_ambiguous(
        reservation,
        result=ReelReconciliationOutcome.STILL_AMBIGUOUS.value,
        evidence=evidence,
        now=now,
    )
    return _result(reservation, ReelReconciliationOutcome.STILL_AMBIGUOUS, evidence)


def recover_reel_media_id(*, publication_id: str, media_id: str, access_token: str, now: datetime | None = None) -> ReelReconciliationResult:
    if not isinstance(media_id, str) or not media_id or media_id != media_id.strip():
        raise ValueError("Instagram media ID must be a non-empty trimmed string")
    if not access_token:
        raise ValueError("Instagram access token is required for recovery")
    recovery_time = now or datetime.now(timezone.utc)
    if recovery_time.tzinfo is None or recovery_time.utcoffset() is None:
        raise ValueError("Recovery time must be timezone-aware")
    recovery_time = recovery_time.astimezone(timezone.utc)
    reservations = history_tracker.list_unresolved_reel_reservations(
        limit=1, now=recovery_time, max_age=None, publication_id=publication_id
    )
    if len(reservations) != 1:
        raise ValueError("Reel publication must currently be unresolved")
    reservation = reservations[0]
    if reservation.status not in {ReelPublicationStatus.PUBLISHING, ReelPublicationStatus.AMBIGUOUS}:
        raise ValueError("Reel publication must be PUBLISHING or AMBIGUOUS")
    if not reservation.container_id:
        raise ValueError("Reel publication has no durable container")
    if instagram_poster.get_container_status(reservation.container_id, access_token) != "PUBLISHED":
        raise ValueError("Reel container is not proven published")
    if instagram_poster.get_instagram_media_id(media_id, access_token) != media_id:
        raise ValueError("Operator media ID could not be verified")
    history_tracker.record_reel_publish_response(
        reservation.publication_id, reservation.release_identity, media_id
    )
    refreshed = history_tracker.get_reel_reservation(reservation.publication_id)
    _finalize_receipt(refreshed, access_token=access_token, now=recovery_time)
    return _result(
        reservation,
        ReelReconciliationOutcome.CONFIRMED_PUBLISHED,
        "operator_supplied_media_id_verified",
    )


def reconcile_reel_publications(*, access_token: str, limit: int = STARTUP_RECONCILIATION_LIMIT, max_age: timedelta | None = STARTUP_RECONCILIATION_MAX_AGE, now: datetime | None = None) -> ReelReconciliationSummary:
    if not access_token:
        raise ValueError("Instagram access token is required for reconciliation")
    reconciliation_time = now or datetime.now(timezone.utc)
    if reconciliation_time.tzinfo is None or reconciliation_time.utcoffset() is None:
        raise ValueError("Reconciliation time must be timezone-aware")
    reconciliation_time = reconciliation_time.astimezone(timezone.utc)
    reservations = history_tracker.list_unresolved_reel_reservations(
        limit=limit, now=reconciliation_time, max_age=max_age
    )
    results = []
    for reservation in reservations:
        try:
            result = _reconcile_reservation(
                reservation, access_token=access_token, now=reconciliation_time
            )
        except Exception as error:
            logger.exception("reel_reconciliation_failed publication_id=%s", reservation.publication_id)
            result = _result(
                reservation,
                ReelReconciliationOutcome.RECONCILIATION_ERROR,
                f"history_update_error:{type(error).__name__}",
            )
        if result is not None:
            results.append(result)

    cleanup_inspected = cleanup_deleted = cleanup_failures = 0
    try:
        cleanup_ids = history_tracker.list_reel_staging_cleanup_publication_ids(limit=limit)
    except Exception as error:
        cleanup_ids = []
        cleanup_failures = 1
        logger.exception("reel_cleanup_queue_read_failed error=%s", type(error).__name__)
    for queued_publication_id in cleanup_ids:
        cleanup_inspected += 1
        cleanup = r2_media.cleanup_publication_reels(
            queued_publication_id, reason="authoritative_expired_recovery"
        )
        cleanup_deleted += cleanup.deleted
        if not cleanup.complete:
            cleanup_failures += 1
            continue
        try:
            history_tracker.acknowledge_reel_staging_cleanup(queued_publication_id)
        except Exception:
            cleanup_failures += 1
            logger.exception("reel_cleanup_ack_failed publication_id=%s", queued_publication_id)

    return ReelReconciliationSummary(
        inspected=len(reservations),
        confirmed_published=sum(result.outcome is ReelReconciliationOutcome.CONFIRMED_PUBLISHED for result in results),
        confirmed_not_published=sum(result.outcome is ReelReconciliationOutcome.CONFIRMED_NOT_PUBLISHED for result in results),
        still_ambiguous=sum(result.outcome is ReelReconciliationOutcome.STILL_AMBIGUOUS for result in results),
        errors=sum(result.outcome is ReelReconciliationOutcome.RECONCILIATION_ERROR for result in results),
        cleanup_inspected=cleanup_inspected,
        cleanup_deleted=cleanup_deleted,
        cleanup_failures=cleanup_failures,
        results=tuple(results),
    )
