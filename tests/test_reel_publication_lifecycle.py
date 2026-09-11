import copy
from datetime import datetime, timezone

import pytest

from src import history_tracker, r2_media
from src.models import ReelPublicationStatus, ReelReleaseIdentity


NOW = datetime(2026, 9, 11, 12, 3, tzinfo=timezone.utc)
PUBLICATION_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_PUBLICATION_ID = "22345678-1234-4234-8234-123456789abc"
SHA256 = "a" * 64
PERMALINK = "https://www.instagram.com/reel/example/"


def _identity(*, artwork_id="aic_84774", manifest_sha256=SHA256):
    return ReelReleaseIdentity.model_validate(
        {
            "version": "artfolio-release-v1",
            "reel_id": artwork_id,
            "created_at": "2026-09-11T11:55:00Z",
            "manifest_sha256": manifest_sha256,
            "files_sha256": {
                "reel.mp4": SHA256,
                "caption.txt": SHA256,
                "metadata.json": SHA256,
                "qc/contact-sheet.png": SHA256,
            },
        }
    )


RELEASE_IDENTITY = _identity()


def _upload(publication_id=PUBLICATION_ID, *, url="https://media.example/reel.mp4"):
    return r2_media.TempReelUpload(
        f"reels/publications/{publication_id}/20260911120300_"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.mp4",
        url,
        publication_id,
    )


def _reservation(*, status="PENDING", staged=False, receipt=None):
    reservation = {
        "publication_id": PUBLICATION_ID,
        "artwork_id": "aic_84774",
        "status": status,
        "reserved_at": "2026-09-11T12:00:00Z",
        "release_identity": RELEASE_IDENTITY.model_dump(mode="json"),
    }
    if staged:
        reservation["staging"] = {
            "object_key": _upload().object_key,
            "public_url": _upload().public_url,
            "staged_at": "2026-09-11T12:01:00Z",
        }
    if status in {"PUBLISHING", "AMBIGUOUS", "PUBLISHED"}:
        reservation["container_id"] = "container-1"
        reservation["publish_started_at"] = "2026-09-11T12:02:00Z"
    if receipt is not None:
        reservation["publish_response_media_id"] = receipt
    if status == "AMBIGUOUS":
        reservation["ambiguous_at"] = "2026-09-11T12:03:00Z"
        reservation["ambiguity_reason"] = "response unavailable"
    if status == "PUBLISHED":
        reservation["media_id"] = receipt or "media-reel-1"
        reservation["posted_at"] = "2026-09-11T12:03:00Z"
    return reservation


def _history(*, status="PENDING", staged=False, receipt=None):
    return {
        "posted_artworks": [{"id": "feed-art", "status": "PUBLISHED"}],
        "publications": [],
        "grid_publication_count": 0,
        "active_color_tone": "cool",
        "reel_reservations": [
            _reservation(status=status, staged=staged, receipt=receipt)
        ],
        "reel_publications": [],
        "reel_publication_count": 0,
        "reel_staging_cleanup_queue": [],
        "unknown_top_level_key": {"preserve": True},
    }


def _install_history(monkeypatch, history, events=None):
    writes = []
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, '"etag-1"')
    )

    def upload(value, etag):
        writes.append(copy.deepcopy(value))
        if events is not None:
            reservation = value["reel_reservations"][0]
            events.append(
                f"history_put:{reservation['status']}:{reservation.get('container_id', '')}"
            )

    monkeypatch.setattr(history_tracker, "_upload_history", upload)
    return writes


def test_get_reel_reservation_returns_the_pinned_typed_reservation(monkeypatch):
    history = _history()
    _install_history(monkeypatch, history)

    reservation = history_tracker.get_reel_reservation(PUBLICATION_ID)

    assert reservation.publication_id == PUBLICATION_ID
    assert reservation.release_identity == RELEASE_IDENTITY
    with pytest.raises(RuntimeError, match="Missing Reel reservation"):
        history_tracker.get_reel_reservation(OTHER_PUBLICATION_ID)


def test_record_reel_staging_persists_owned_handle_while_pending_and_replays_exactly(
    monkeypatch,
):
    history = _history()
    writes = _install_history(monkeypatch, history)
    upload = _upload()

    staged = history_tracker.record_reel_staging(
        PUBLICATION_ID, RELEASE_IDENTITY, upload, now=NOW
    )
    replay = history_tracker.record_reel_staging(
        PUBLICATION_ID, RELEASE_IDENTITY, upload, now=NOW
    )

    assert staged.status is ReelPublicationStatus.PENDING
    assert replay == staged
    assert history["reel_reservations"][0]["staging"] == {
        "object_key": upload.object_key,
        "public_url": upload.public_url,
        "staged_at": "2026-09-11T12:03:00Z",
    }
    assert len(writes) == 1


def test_record_reel_staging_fails_closed_for_conflicting_identity_owner_or_state(
    monkeypatch,
):
    history = _history()
    _install_history(monkeypatch, history)

    with pytest.raises(RuntimeError, match="release identity"):
        history_tracker.record_reel_staging(
            PUBLICATION_ID,
            _identity(manifest_sha256="b" * 64),
            _upload(),
            now=NOW,
        )
    with pytest.raises(RuntimeError, match="owned by"):
        history_tracker.record_reel_staging(
            PUBLICATION_ID, RELEASE_IDENTITY, _upload(OTHER_PUBLICATION_ID), now=NOW
        )

    history["reel_reservations"][0]["status"] = "PUBLISHING"
    history["reel_reservations"][0]["staging"] = {
        "object_key": _upload().object_key,
        "public_url": _upload().public_url,
        "staged_at": "2026-09-11T12:01:00Z",
    }
    history["reel_reservations"][0]["container_id"] = "container-1"
    history["reel_reservations"][0]["publish_started_at"] = "2026-09-11T12:02:00Z"
    with pytest.raises(RuntimeError, match="PENDING"):
        history_tracker.record_reel_staging(
            PUBLICATION_ID, RELEASE_IDENTITY, _upload(), now=NOW
        )


def test_before_publish_boundary_persists_container_before_media_publish(monkeypatch):
    history = _history(staged=True)
    events = []
    _install_history(monkeypatch, history, events)

    reservation = history_tracker.start_reel_publication_attempt(
        PUBLICATION_ID, RELEASE_IDENTITY, "container-1", now=NOW
    )
    events.append("media_publish")

    assert reservation.status is ReelPublicationStatus.PUBLISHING
    assert reservation.container_id == "container-1"
    assert reservation.publish_started_at == "2026-09-11T12:03:00Z"
    assert events == ["history_put:PUBLISHING:container-1", "media_publish"]


def test_start_reel_publication_requires_staging_and_only_replays_same_container(
    monkeypatch,
):
    history = _history()
    _install_history(monkeypatch, history)

    with pytest.raises(RuntimeError, match="staging"):
        history_tracker.start_reel_publication_attempt(
            PUBLICATION_ID, RELEASE_IDENTITY, "container-1", now=NOW
        )

    history = _history(status="PUBLISHING", staged=True)
    writes = _install_history(monkeypatch, history)
    replay = history_tracker.start_reel_publication_attempt(
        PUBLICATION_ID, RELEASE_IDENTITY, "container-1", now=NOW
    )
    assert replay.status is ReelPublicationStatus.PUBLISHING
    assert writes == []
    with pytest.raises(RuntimeError, match="different publish boundary"):
        history_tracker.start_reel_publication_attempt(
            PUBLICATION_ID, RELEASE_IDENTITY, "container-2", now=NOW
        )


def test_receipt_precedes_finalization_and_publishing_or_ambiguous_keep_it(monkeypatch):
    history = _history(status="PUBLISHING", staged=True)
    writes = _install_history(monkeypatch, history)

    receipt = history_tracker.record_reel_publish_response(
        PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1"
    )
    ambiguous = history_tracker.mark_reel_ambiguous(
        PUBLICATION_ID, RELEASE_IDENTITY, "response unavailable", now=NOW
    )
    replay = history_tracker.record_reel_publish_response(
        PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1"
    )

    assert receipt.publish_response_media_id == "media-reel-1"
    assert ambiguous.status is ReelPublicationStatus.AMBIGUOUS
    assert ambiguous.publish_response_media_id == "media-reel-1"
    assert replay.publish_response_media_id == "media-reel-1"
    assert len(writes) == 2
    with pytest.raises(RuntimeError, match="conflicting media"):
        history_tracker.record_reel_publish_response(
            PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-2"
        )


def test_receipt_replay_rejects_a_published_reservation_with_different_media(
    monkeypatch,
):
    history = _history(status="PUBLISHED", staged=True, receipt="media-reel-1")
    history["reel_reservations"][0]["media_id"] = "media-reel-2"
    history["reel_reservations"][0]["posted_at"] = "2026-09-11T12:03:00Z"
    _install_history(monkeypatch, history)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="conflicting media"):
        history_tracker.record_reel_publish_response(
            PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1"
        )


def test_receipt_replay_rejects_a_published_reservation_without_publication(
    monkeypatch,
):
    history = _history(status="PUBLISHED", staged=True, receipt="media-reel-1")
    _install_history(monkeypatch, history)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="publication"):
        history_tracker.record_reel_publish_response(
            PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1"
        )


def test_finalization_is_one_cas_counter_isolated_and_exact_replay_is_a_noop(monkeypatch):
    history = _history(status="PUBLISHING", staged=True, receipt="media-reel-1")
    writes = _install_history(monkeypatch, history)
    feed_before = {
        key: copy.deepcopy(history[key])
        for key in (
            "posted_artworks",
            "publications",
            "grid_publication_count",
            "active_color_tone",
        )
    }

    publication = history_tracker.finalize_reel_publication(
        PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1", now=NOW
    )
    replay = history_tracker.finalize_reel_publication(
        PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1", now=NOW
    )

    assert publication == replay
    assert publication.media_id == "media-reel-1"
    assert publication.permalink is None
    assert history["reel_reservations"][0]["status"] == "PUBLISHED"
    assert history["reel_publications"] == [
        publication.model_dump(mode="json", exclude_none=True)
    ]
    assert history["reel_publication_count"] == 1
    assert {key: history[key] for key in feed_before} == feed_before
    assert len(writes) == 1


def test_finalization_rejects_a_published_reservation_without_publication(monkeypatch):
    history = _history(status="PUBLISHED", staged=True, receipt="media-reel-1")
    _install_history(monkeypatch, history)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="publication"):
        history_tracker.finalize_reel_publication(
            PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1", now=NOW
        )


def test_finalization_requires_matching_receipt_and_allows_ambiguous_recovery(monkeypatch):
    history = _history(status="PUBLISHING", staged=True)
    _install_history(monkeypatch, history)
    with pytest.raises(RuntimeError, match="receipt"):
        history_tracker.finalize_reel_publication(
            PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1", now=NOW
        )

    history = _history(status="AMBIGUOUS", staged=True, receipt="media-reel-1")
    _install_history(monkeypatch, history)
    publication = history_tracker.finalize_reel_publication(
        PUBLICATION_ID,
        RELEASE_IDENTITY,
        "media-reel-1",
        permalink=PERMALINK,
        now=NOW,
        reconciliation_result="verified_media_identity",
        reconciliation_evidence="operator lookup",
    )
    assert publication.permalink == PERMALINK
    assert history["reel_reservations"][0]["status"] == "PUBLISHED"
    assert history["reel_reservations"][0]["publish_response_media_id"] == "media-reel-1"


@pytest.mark.parametrize("conflict_field", ["id", "media_id"])
def test_finalization_rejects_cross_format_identity_conflicts(monkeypatch, conflict_field):
    history = _history(status="PUBLISHING", staged=True, receipt="media-reel-1")
    feed_publication = {
        "id": "feed-publication",
        "type": "single",
        "media_id": "feed-media",
        "artwork_ids": ["feed-art"],
        "posted_at": "2026-09-11T12:00:00Z",
    }
    feed_publication[conflict_field] = (
        PUBLICATION_ID if conflict_field == "id" else "media-reel-1"
    )
    history["publications"] = [feed_publication]
    history["grid_publication_count"] = 1
    _install_history(monkeypatch, history)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="conflict"):
        history_tracker.finalize_reel_publication(
            PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1", now=NOW
        )


def test_record_reel_permalink_enriches_only_matching_published_pair(monkeypatch):
    history = _history(status="PUBLISHED", staged=True, receipt="media-reel-1")
    history["reel_reservations"][0]["posted_at"] = "2026-09-11T12:03:00Z"
    history["reel_reservations"][0]["media_id"] = "media-reel-1"
    history["reel_publications"] = [
        {
            "id": PUBLICATION_ID,
            "artwork_id": "aic_84774",
            "media_id": "media-reel-1",
            "posted_at": "2026-09-11T12:03:00Z",
            "release_identity": RELEASE_IDENTITY.model_dump(mode="json"),
        }
    ]
    history["reel_publication_count"] = 1
    writes = _install_history(monkeypatch, history)

    publication = history_tracker.record_reel_permalink(
        PUBLICATION_ID, "media-reel-1", PERMALINK
    )
    replay = history_tracker.record_reel_permalink(
        PUBLICATION_ID, "media-reel-1", PERMALINK
    )

    assert publication == replay
    assert history["reel_reservations"][0]["permalink"] == PERMALINK
    assert history["reel_publications"][0]["permalink"] == PERMALINK
    assert len(writes) == 1
    with pytest.raises(RuntimeError, match="media"):
        history_tracker.record_reel_permalink(
            PUBLICATION_ID, "other-media", PERMALINK
        )
