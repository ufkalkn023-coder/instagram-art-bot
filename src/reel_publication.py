"""Manual one-release orchestration for verified Artfolio Reel packages."""

import logging
from pathlib import Path
from urllib.parse import urlsplit

from src import history_tracker, instagram_poster, r2_media, reel_release
from src.models import ReelPublicationRecord, ReelPublicationStatus


logger = logging.getLogger(__name__)

STAGING_FAILED = "reel_staging_failed"
STAGING_HANDLE_PERSISTENCE_FAILED = "reel_staging_handle_persistence_failed"
CONTAINER_FAILED_BEFORE_PUBLISH = "reel_container_failed_before_publish"
PRE_PUBLISH_BOUNDARY_UNCONFIRMED = "reel_pre_publish_boundary_unconfirmed"
PUBLISH_OUTCOME_UNVERIFIED = "reel_publish_outcome_unverified"
RECEIPT_NOT_DURABLE = "reel_receipt_not_durable"
FINALIZATION_NOT_DURABLE = "reel_finalization_not_durable"


class ReelPublicationPersistenceError(RuntimeError):
    """Meta accepted a Reel publish but its receipt could not be durably recorded."""


def publish_verified_reel(
    *,
    release: str | Path,
    reels_repository: str | Path,
    account_id: str,
    access_token: str,
) -> ReelPublicationRecord:
    """Publish exactly one deeply verified release package as an Instagram Reel."""
    with reel_release.verified_reel_release_snapshot(
        release, reels_repository=reels_repository
    ) as verified:
        return _publish_verified_snapshot(
            verified, account_id=account_id, access_token=access_token
        )


def _publish_verified_snapshot(verified, *, account_id: str, access_token: str):
    publication_id = history_tracker.reserve_reel(
        verified.artwork_id, verified.release_identity
    )
    try:
        upload = r2_media.stage_reel_mp4(str(verified.video_path), publication_id)
    except Exception:
        _expire_pre_meta_reel(
            publication_id,
            verified.release_identity,
            reason=STAGING_FAILED,
            expected_status=ReelPublicationStatus.PENDING,
        )
        raise
    try:
        history_tracker.record_reel_staging(
            publication_id, verified.release_identity, upload
        )
    except Exception:
        _recover_staged_reel(
            publication_id,
            verified.release_identity,
            upload,
            reason=STAGING_HANDLE_PERSISTENCE_FAILED,
            expected_status=ReelPublicationStatus.PENDING,
            rollback_handle=True,
        )
        raise

    boundary = {"container_id": None, "durable": False}

    def before_publish(container_id: str, _extra: tuple[str, ...]) -> None:
        boundary["container_id"] = container_id
        history_tracker.start_reel_publication_attempt(
            publication_id, verified.release_identity, container_id
        )
        boundary["durable"] = True

    try:
        media_id = instagram_poster.post_to_instagram_graph_api(
            media_url=upload.public_url,
            caption=verified.caption,
            account_id=account_id,
            access_token=access_token,
            media_type="REELS",
            before_publish=before_publish,
        )
    except instagram_poster.InstagramPrePublishBoundaryError:
        _expire_unconfirmed_boundary_reel(
            publication_id, verified.release_identity, boundary
        )
        raise
    except Exception:
        if not boundary["durable"]:
            _recover_staged_reel(
                publication_id,
                verified.release_identity,
                upload,
                reason=CONTAINER_FAILED_BEFORE_PUBLISH,
                expected_status=ReelPublicationStatus.PENDING,
                rollback_handle=False,
            )
        else:
            _mark_reel_ambiguous_best_effort(
                publication_id,
                verified.release_identity,
                reason=PUBLISH_OUTCOME_UNVERIFIED,
            )
        raise

    try:
        history_tracker.record_reel_publish_response(
            publication_id, verified.release_identity, media_id
        )
    except Exception as error:
        _mark_reel_ambiguous_best_effort(
            publication_id,
            verified.release_identity,
            reason=RECEIPT_NOT_DURABLE,
        )
        raise ReelPublicationPersistenceError(
            "Meta accepted the Reel publish but its media receipt could not be "
            f"durably recorded for publication {publication_id}"
        ) from error
    permalink = _best_effort_permalink(media_id, access_token)
    try:
        return history_tracker.finalize_reel_publication(
            publication_id,
            verified.release_identity,
            media_id,
            permalink=permalink,
        )
    except Exception as error:
        _mark_reel_ambiguous_best_effort(
            publication_id,
            verified.release_identity,
            reason=FINALIZATION_NOT_DURABLE,
        )
        raise ReelPublicationPersistenceError(
            "Meta accepted the Reel publish but its publication could not be "
            f"durably finalized for publication {publication_id}"
        ) from error


def _mark_reel_ambiguous_best_effort(
    publication_id: str, release_identity, *, reason: str
) -> None:
    """Quarantine a crossed publish boundary; never undo durable protection."""
    try:
        history_tracker.mark_reel_ambiguous(publication_id, release_identity, reason)
    except Exception as error:
        logger.warning(
            "reel_ambiguous_mark_failed publication_id=%s reason=%s error=%s",
            publication_id,
            reason,
            type(error).__name__,
        )


def _expire_pre_meta_reel(
    publication_id: str,
    release_identity,
    *,
    reason: str,
    expected_status: ReelPublicationStatus,
    expected_container_id: str | None = None,
) -> bool:
    """Durably expire provably pre-Meta work; the queue entry is one CAS."""
    try:
        history_tracker.expire_reel_before_media_publish(
            publication_id,
            release_identity,
            reason=reason,
            expected_status=expected_status,
            expected_container_id=expected_container_id,
        )
        return True
    except Exception as error:
        logger.warning(
            "reel_pre_meta_expiry_failed publication_id=%s reason=%s error=%s",
            publication_id,
            reason,
            type(error).__name__,
        )
        return False


def _recover_staged_reel(
    publication_id: str,
    release_identity,
    upload,
    *,
    reason: str,
    expected_status: ReelPublicationStatus,
    rollback_handle: bool,
) -> None:
    """Expire durably first, then roll back the owned handle and Reel prefix."""
    expired = _expire_pre_meta_reel(
        publication_id,
        release_identity,
        reason=reason,
        expected_status=expected_status,
    )
    if rollback_handle:
        try:
            r2_media.cleanup_temp_reel_upload(upload, reason=reason)
        except Exception as error:
            logger.warning(
                "reel_upload_cleanup_failed publication_id=%s reason=%s error=%s",
                publication_id,
                reason,
                type(error).__name__,
            )
    if expired:
        _cleanup_reel_prefix(publication_id, reason=reason)


def _cleanup_reel_prefix(publication_id: str, *, reason: str) -> None:
    try:
        summary = r2_media.cleanup_publication_reels(publication_id, reason=reason)
    except Exception as error:
        logger.warning(
            "reel_prefix_cleanup_failed publication_id=%s reason=%s error=%s",
            publication_id,
            reason,
            type(error).__name__,
        )
        return
    if not summary.complete:
        logger.warning(
            "reel_prefix_cleanup_incomplete publication_id=%s reason=%s deleted=%s",
            publication_id,
            reason,
            summary.deleted,
        )


def _expire_unconfirmed_boundary_reel(
    publication_id: str, release_identity, boundary: dict
) -> None:
    """Expire only when re-read state proves the publish never reached media_publish."""
    try:
        reservation = history_tracker.get_reel_reservation(publication_id)
    except Exception as error:
        logger.warning(
            "reel_boundary_reread_failed publication_id=%s error=%s",
            publication_id,
            type(error).__name__,
        )
        return
    container_id = boundary["container_id"]
    if reservation.status is ReelPublicationStatus.PENDING:
        expected_status = ReelPublicationStatus.PENDING
        expected_container_id = None
    elif (
        reservation.status is ReelPublicationStatus.PUBLISHING
        and container_id is not None
        and reservation.container_id == container_id
        and reservation.publish_response_media_id is None
    ):
        expected_status = ReelPublicationStatus.PUBLISHING
        expected_container_id = container_id
    else:
        logger.warning(
            "reel_boundary_state_not_provably_pre_media publication_id=%s status=%s",
            publication_id,
            reservation.status.value,
        )
        return
    if _expire_pre_meta_reel(
        publication_id,
        release_identity,
        reason=PRE_PUBLISH_BOUNDARY_UNCONFIRMED,
        expected_status=expected_status,
        expected_container_id=expected_container_id,
    ):
        _cleanup_reel_prefix(publication_id, reason=PRE_PUBLISH_BOUNDARY_UNCONFIRMED)


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
