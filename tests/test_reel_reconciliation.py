from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src import history_tracker, instagram_poster, r2_media, reel_reconciliation


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
PUBLICATION_ID = "11111111-1111-4111-8111-111111111111"


def _identity():
    return {
        "version": "artfolio-release-v1",
        "reel_id": "met_1",
        "created_at": "2026-09-11T09:00:00Z",
        "manifest_sha256": "a" * 64,
        "files_sha256": {
            "reel.mp4": "b" * 64,
            "caption.txt": "c" * 64,
            "metadata.json": "d" * 64,
            "qc/contact-sheet.png": "e" * 64,
        },
    }


def _reservation(*, status="PUBLISHING", receipt=None):
    record = {
        "publication_id": PUBLICATION_ID,
        "artwork_id": "met_1",
        "status": status,
        "reserved_at": "2026-09-11T09:00:00Z",
        "release_identity": _identity(),
        "staging": {
            "object_key": f"reels/publications/{PUBLICATION_ID}/20260911100000_" + "f" * 32 + ".mp4",
            "public_url": "https://media.example/reel.mp4",
            "staged_at": "2026-09-11T10:00:00Z",
        },
        "container_id": "container-1",
        "publish_started_at": "2026-09-11T10:00:00Z",
    }
    if receipt is not None:
        record["publish_response_media_id"] = receipt
    return record


def _pending_reservation():
    return {
        "publication_id": PUBLICATION_ID,
        "artwork_id": "met_1",
        "status": "PENDING",
        "reserved_at": "2026-09-11T09:00:00Z",
        "release_identity": _identity(),
    }


def _backend(monkeypatch, reservation):
    history = {"reel_reservations": [reservation]}
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda *_args: None)
    return history


def test_durable_publish_receipt_finalizes_without_meta_lookup(monkeypatch):
    reservation = _reservation(receipt="media-1")
    history = _backend(monkeypatch, reservation)
    monkeypatch.setattr(
        instagram_poster,
        "get_container_status",
        lambda *_args: pytest.fail("durable receipt must not query Meta"),
    )

    summary = reel_reconciliation.reconcile_reel_publications(
        access_token="token", now=NOW
    )

    assert reservation["status"] == "PUBLISHED"
    assert history["reel_publications"][0]["media_id"] == "media-1"
    assert summary.confirmed_published == 1


def test_fresh_publishing_within_grace_window_stays_untouched(monkeypatch):
    reservation = _reservation()
    history = _backend(monkeypatch, reservation)
    monkeypatch.setattr(
        instagram_poster,
        "get_container_status",
        lambda *_args: pytest.fail("fresh publishing work must not query Meta"),
    )

    summary = reel_reconciliation.reconcile_reel_publications(
        access_token="token",
        now=datetime(2026, 9, 11, 10, 49, tzinfo=timezone.utc),
    )

    assert reservation["status"] == "PUBLISHING"
    assert "last_reconciled_at" not in reservation
    assert history.get("reel_publications", []) == []
    assert summary.results == ()


@pytest.mark.parametrize(
    "container_status",
    ["FINISHED", "IN_PROGRESS", "ERROR", "EXPIRED", "PUBLISHED", "UNKNOWN"],
)
def test_container_status_without_media_identity_stays_ambiguous(
    monkeypatch, container_status
):
    reservation = _reservation()
    history = _backend(monkeypatch, reservation)
    monkeypatch.setattr(
        instagram_poster, "get_container_status", lambda *_args: container_status
    )
    publish = Mock(side_effect=AssertionError("reconciliation must not publish"))
    monkeypatch.setattr(instagram_poster, "_publish_container", publish)

    reel_reconciliation.reconcile_reel_publications(access_token="token", now=NOW)

    assert reservation["status"] == "AMBIGUOUS"
    assert history.get("reel_publications", []) == []
    assert reservation["reconciliation_attempt_count"] == 1
    publish.assert_not_called()


def test_container_status_lookup_error_becomes_ambiguous_with_evidence(monkeypatch):
    reservation = _reservation()
    _backend(monkeypatch, reservation)
    monkeypatch.setattr(
        instagram_poster,
        "get_container_status",
        Mock(side_effect=RuntimeError("Meta unavailable")),
    )

    summary = reel_reconciliation.reconcile_reel_publications(access_token="token", now=NOW)

    assert reservation["status"] == "AMBIGUOUS"
    assert reservation["reconciliation_result"] == "STILL_AMBIGUOUS"
    assert reservation["reconciliation_evidence"] == "container_status_error:RuntimeError"
    assert reservation["last_reconciled_at"] == "2026-09-11T12:00:00Z"
    assert reservation["reconciliation_attempt_count"] == 1
    assert summary.still_ambiguous == 1


def test_recover_reel_media_id_finalizes_verified_published_container(monkeypatch):
    reservation = _reservation()
    history = _backend(monkeypatch, reservation)
    monkeypatch.setattr(instagram_poster, "get_container_status", lambda *_args: "PUBLISHED")
    monkeypatch.setattr(instagram_poster, "get_instagram_media_id", lambda *_args: "media-1")
    monkeypatch.setattr(instagram_poster, "get_instagram_permalink", lambda *_args: None)

    result = reel_reconciliation.recover_reel_media_id(
        publication_id=PUBLICATION_ID,
        media_id="media-1",
        access_token="token",
        now=NOW,
    )

    assert result.outcome is reel_reconciliation.ReelReconciliationOutcome.CONFIRMED_PUBLISHED
    assert history["reel_reservations"][0]["status"] == "PUBLISHED"
    assert history["reel_reservations"][0]["media_id"] == "media-1"
    assert history["reel_publications"][0]["media_id"] == "media-1"


def test_recover_reel_media_id_rejects_non_published_container(monkeypatch):
    reservation = _reservation()
    _backend(monkeypatch, reservation)
    monkeypatch.setattr(instagram_poster, "get_container_status", lambda *_args: "FINISHED")
    media_lookup = Mock(side_effect=AssertionError("must not verify an unpublished container"))
    monkeypatch.setattr(instagram_poster, "get_instagram_media_id", media_lookup)

    with pytest.raises(ValueError, match="not proven published"):
        reel_reconciliation.recover_reel_media_id(
            publication_id=PUBLICATION_ID, media_id="media-1", access_token="token", now=NOW
        )

    assert reservation["status"] == "PUBLISHING"
    media_lookup.assert_not_called()


def test_recover_reel_media_id_rejects_mismatched_media_identity(monkeypatch):
    reservation = _reservation()
    _backend(monkeypatch, reservation)
    monkeypatch.setattr(instagram_poster, "get_container_status", lambda *_args: "PUBLISHED")
    monkeypatch.setattr(instagram_poster, "get_instagram_media_id", lambda *_args: "other-media")

    with pytest.raises(ValueError, match="could not be verified"):
        reel_reconciliation.recover_reel_media_id(
            publication_id=PUBLICATION_ID, media_id="media-1", access_token="token", now=NOW
        )

    assert reservation["status"] == "PUBLISHING"


def test_pre_meta_expiry_atomically_queues_only_the_reel_publication(monkeypatch):
    reservation = _pending_reservation()
    history = _backend(monkeypatch, reservation)

    expired = history_tracker.expire_reel_before_media_publish(
        PUBLICATION_ID,
        history_tracker.ReelReleaseIdentity.model_validate(_identity()),
        reason="staging_failed",
        expected_status=history_tracker.ReelPublicationStatus.PENDING,
        now=NOW,
    )

    assert expired.status is history_tracker.ReelPublicationStatus.EXPIRED
    assert reservation["status"] == "EXPIRED"
    assert history["reel_staging_cleanup_queue"] == [
        {
            "publication_id": PUBLICATION_ID,
            "eligible_at": "2026-09-11T12:00:00Z",
            "reason": "staging_failed",
        }
    ]
    assert "staging_media_cleanup_queue" not in history


def test_publishing_expiry_requires_its_exact_durable_container_id(monkeypatch):
    reservation = _reservation()
    history = _backend(monkeypatch, reservation)
    identity = history_tracker.ReelReleaseIdentity.model_validate(_identity())

    with pytest.raises(ValueError, match="exact durable container ID"):
        history_tracker.expire_reel_before_media_publish(
            PUBLICATION_ID,
            identity,
            reason="publisher_stopped",
            expected_status=history_tracker.ReelPublicationStatus.PUBLISHING,
            now=NOW,
        )
    with pytest.raises(RuntimeError, match="container ID does not match"):
        history_tracker.expire_reel_before_media_publish(
            PUBLICATION_ID,
            identity,
            reason="publisher_stopped",
            expected_status=history_tracker.ReelPublicationStatus.PUBLISHING,
            expected_container_id="other-container",
            now=NOW,
        )

    assert reservation["status"] == "PUBLISHING"
    assert "reel_staging_cleanup_queue" not in history


def test_publishing_with_durable_receipt_cannot_expire(monkeypatch):
    reservation = _reservation(receipt="media-1")
    history = _backend(monkeypatch, reservation)

    with pytest.raises(RuntimeError, match="durable publish receipt"):
        history_tracker.expire_reel_before_media_publish(
            PUBLICATION_ID,
            history_tracker.ReelReleaseIdentity.model_validate(_identity()),
            reason="publisher_stopped",
            expected_status=history_tracker.ReelPublicationStatus.PUBLISHING,
            expected_container_id="container-1",
            now=NOW,
        )

    assert reservation["status"] == "PUBLISHING"
    assert "reel_staging_cleanup_queue" not in history


def test_reel_cleanup_uses_only_reel_cleanup_api_and_acknowledges_complete_work(
    monkeypatch,
):
    reservation = _reservation()
    reservation.update(
        {
            "status": "EXPIRED",
            "expired_at": "2026-09-11T11:00:00Z",
            "expiration_reason": "staging_failed",
        }
    )
    reservation.pop("container_id")
    reservation.pop("publish_started_at")
    history = _backend(monkeypatch, reservation)
    history["reel_staging_cleanup_queue"] = [
        {
            "publication_id": PUBLICATION_ID,
            "eligible_at": "2026-09-11T11:00:00Z",
            "reason": "staging_failed",
        }
    ]
    cleaned = []
    monkeypatch.setattr(
        reel_reconciliation.r2_media,
        "cleanup_publication_reels",
        lambda publication_id, **kwargs: cleaned.append((publication_id, kwargs["reason"]))
        or r2_media.MediaCleanupSummary(publication_id, 1, 1, 0, True, kwargs["reason"]),
    )
    monkeypatch.setattr(
        reel_reconciliation.r2_media,
        "cleanup_publication_media",
        lambda *_args, **_kwargs: pytest.fail("Reel cleanup must never use feed cleanup"),
    )

    summary = reel_reconciliation.reconcile_reel_publications(
        access_token="token", now=NOW
    )

    assert cleaned == [(PUBLICATION_ID, "authoritative_expired_recovery")]
    assert history["reel_staging_cleanup_queue"] == []
    assert summary.cleanup_deleted == 1


def test_incomplete_reel_cleanup_remains_queued(monkeypatch):
    reservation = _pending_reservation()
    reservation.update(
        {
            "status": "EXPIRED",
            "expired_at": "2026-09-11T11:00:00Z",
            "expiration_reason": "staging_failed",
        }
    )
    history = _backend(monkeypatch, reservation)
    history["reel_staging_cleanup_queue"] = [
        {"publication_id": PUBLICATION_ID, "eligible_at": "2026-09-11T11:00:00Z", "reason": "staging_failed"}
    ]
    monkeypatch.setattr(
        reel_reconciliation.r2_media,
        "cleanup_publication_reels",
        lambda publication_id, **kwargs: r2_media.MediaCleanupSummary(
            publication_id, 2, 1, 1, False, kwargs["reason"]
        ),
    )

    summary = reel_reconciliation.reconcile_reel_publications(access_token="token", now=NOW)

    assert history["reel_staging_cleanup_queue"][0]["publication_id"] == PUBLICATION_ID
    assert summary.cleanup_failures == 1


@pytest.mark.parametrize("status", ["AMBIGUOUS", "PUBLISHED"])
def test_ambiguous_or_published_reel_is_never_cleanup_eligible(monkeypatch, status):
    reservation = _reservation()
    if status == "AMBIGUOUS":
        reservation.update(
            {
                "status": "AMBIGUOUS",
                "ambiguous_at": "2026-09-11T11:00:00Z",
                "ambiguity_reason": "outcome_unknown",
            }
        )
    else:
        reservation.update(
            {
                "status": "PUBLISHED",
                "publish_response_media_id": "media-1",
                "media_id": "media-1",
                "posted_at": "2026-09-11T11:00:00Z",
            }
        )
    history = _backend(monkeypatch, reservation)
    history["reel_staging_cleanup_queue"] = [
        {"publication_id": PUBLICATION_ID, "eligible_at": "2026-09-11T11:00:00Z", "reason": "stale_entry"}
    ]
    if status == "PUBLISHED":
        history["reel_publications"] = [
            {
                "id": PUBLICATION_ID,
                "artwork_id": "met_1",
                "media_id": "media-1",
                "posted_at": "2026-09-11T11:00:00Z",
                "release_identity": _identity(),
            }
        ]
        history["reel_publication_count"] = 1

    assert history_tracker.list_reel_staging_cleanup_publication_ids(limit=20) == []


def test_stale_cleanup_entry_is_ignored_when_active_reel_overrides_it(monkeypatch):
    reservation = _reservation()
    history = _backend(monkeypatch, reservation)
    history["reel_staging_cleanup_queue"] = [
        {"publication_id": PUBLICATION_ID, "eligible_at": "2026-09-11T11:00:00Z", "reason": "stale_entry"}
    ]
    cleanup = Mock(side_effect=AssertionError("active Reel media must not be deleted"))
    monkeypatch.setattr(reel_reconciliation.r2_media, "cleanup_publication_reels", cleanup)

    summary = reel_reconciliation.reconcile_reel_publications(access_token="token", now=NOW)

    assert cleanup.call_count == 0
    assert summary.cleanup_inspected == 0


def test_reconciliation_never_calls_publishing_or_staging_apis(monkeypatch):
    reservation = _reservation()
    _backend(monkeypatch, reservation)
    monkeypatch.setattr(instagram_poster, "get_container_status", lambda *_args: "UNKNOWN")
    prohibited = [
        (instagram_poster, "post_to_instagram_graph_api"),
        (instagram_poster, "_create_container"),
        (instagram_poster, "_publish_container"),
        (r2_media, "stage_reel_mp4"),
    ]
    for module, name in prohibited:
        monkeypatch.setattr(
            module, name, lambda *_args, **_kwargs: pytest.fail(f"reconciliation must not call {name}")
        )

    reel_reconciliation.reconcile_reel_publications(access_token="token", now=NOW)

    assert reservation["status"] == "AMBIGUOUS"
