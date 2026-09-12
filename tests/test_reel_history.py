import copy
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from src import history_tracker, models
from src.models import ReelReleaseIdentity, ReelReservationRecord


NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
PUBLICATION_ID = "12345678-1234-4234-8234-123456789abc"
SHA256 = "a" * 64


def _identity(artwork_id="aic_84774"):
    return {
        "version": "artfolio-release-v1",
        "reel_id": artwork_id,
        "created_at": "2026-09-11T11:55:00Z",
        "manifest_sha256": SHA256,
        "files_sha256": {
            "reel.mp4": SHA256,
            "caption.txt": SHA256,
            "metadata.json": SHA256,
            "qc/contact-sheet.png": SHA256,
        },
    }


def _reservation(*, status="PENDING", artwork_id="aic_84774", publication_id=PUBLICATION_ID):
    record = {
        "publication_id": publication_id,
        "artwork_id": artwork_id,
        "status": status,
        "reserved_at": "2026-09-11T12:00:00Z",
        "release_identity": _identity(artwork_id),
    }
    if status == "PUBLISHING":
        record.update(
            staging={
                "object_key": f"reels/publications/{publication_id}/reel.mp4",
                "public_url": "https://media.example/reel.mp4",
                "staged_at": "2026-09-11T12:01:00Z",
            },
            container_id="container-1", publish_started_at="2026-09-11T12:02:00Z"
        )
    elif status == "PUBLISHED":
        record.update(
            staging={
                "object_key": f"reels/publications/{publication_id}/reel.mp4",
                "public_url": "https://media.example/reel.mp4",
                "staged_at": "2026-09-11T12:01:00Z",
            },
            container_id="container-1",
            publish_started_at="2026-09-11T12:02:00Z",
            media_id="media-1",
            posted_at="2026-09-11T12:03:00Z",
        )
    elif status == "AMBIGUOUS":
        record.update(
            staging={
                "object_key": f"reels/publications/{publication_id}/reel.mp4",
                "public_url": "https://media.example/reel.mp4",
                "staged_at": "2026-09-11T12:01:00Z",
            },
            container_id="container-1",
            publish_started_at="2026-09-11T12:02:00Z",
            ambiguous_at="2026-09-11T12:03:00Z",
            ambiguity_reason="publish result unavailable",
        )
    elif status == "EXPIRED":
        record.update(
            expired_at="2026-09-11T12:03:00Z",
            expiration_reason="pre-meta failure",
        )
    return record


def _publication(*, artwork_id="aic_84774", publication_id=PUBLICATION_ID, media_id="media-1"):
    return {
        "id": publication_id,
        "artwork_id": artwork_id,
        "media_id": media_id,
        "posted_at": "2026-09-11T12:03:00Z",
        "permalink": "https://www.instagram.com/reel/example/",
        "release_identity": _identity(artwork_id),
    }


def _valid_reel_history(*, status="PUBLISHED"):
    reservation = _reservation(status=status)
    publications = [_publication()] if status == "PUBLISHED" else []
    return {
        "posted_artworks": [],
        "reel_reservations": [reservation],
        "reel_publications": publications,
        "reel_publication_count": len(publications),
        "reel_staging_cleanup_queue": [],
    }


def _install_history(monkeypatch, history):
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, '"etag-1"'))
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda value, etag: uploads.append((copy.deepcopy(value), etag)),
    )
    return uploads


def test_legacy_feed_only_history_has_empty_reel_state_without_mutation():
    history = {"posted_artworks": [{"id": "artic_84774"}]}
    original = copy.deepcopy(history)

    state = history_tracker._validated_reel_history(history)

    assert state.reservations == ()
    assert state.publications == ()
    assert state.publication_count == 0
    assert state.cleanup_queue == ()
    assert history == original


def test_validated_reel_history_is_a_model_interface_type():
    state = history_tracker._validated_reel_history({"posted_artworks": []})

    assert isinstance(state, models.ValidatedReelHistory)


def test_nonempty_reel_publications_require_exact_counter():
    history = _valid_reel_history()
    history.pop("reel_publication_count")

    with pytest.raises(history_tracker.CorruptedHistoryError, match="reel_publication_count"):
        history_tracker._validated_reel_history(history)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("publication_id", "not-a-uuid", "reservation"),
        ("reserved_at", "2026-09-11T12:00:00", "reservation"),
        ("status", "UNKNOWN", "reservation"),
    ],
)
def test_reel_reservation_validation_fails_closed(field, value, message):
    history = _valid_reel_history(status="PENDING")
    history["reel_reservations"][0][field] = value

    with pytest.raises(history_tracker.CorruptedHistoryError, match=message):
        history_tracker._validated_reel_history(history)


def test_reel_release_identity_requires_exact_hash_keys_and_canonical_reel_id():
    invalid = _identity("artic_84774")
    invalid["files_sha256"].pop("caption.txt")

    with pytest.raises(Exception):
        ReelReleaseIdentity.model_validate(invalid)

    history = _valid_reel_history(status="PENDING")
    history["reel_reservations"][0]["release_identity"]["reel_id"] = "met_1"
    with pytest.raises(history_tracker.CorruptedHistoryError, match="reel_id"):
        history_tracker._validated_reel_history(history)


@pytest.mark.parametrize("status", ["PUBLISHING", "AMBIGUOUS"])
def test_reel_receipt_is_valid_during_publish_or_ambiguity(status):
    record = _reservation(status=status)
    record["publish_response_media_id"] = "instagram-media-1"

    validated = ReelReservationRecord.model_validate(record)

    assert validated.publish_response_media_id == "instagram-media-1"


@pytest.mark.parametrize("status", ["PENDING", "EXPIRED"])
def test_reel_receipt_is_forbidden_before_or_after_the_publish_lifecycle(status):
    record = _reservation(status=status)
    record["publish_response_media_id"] = "instagram-media-1"

    with pytest.raises(Exception):
        ReelReservationRecord.model_validate(record)


def test_reel_published_reservation_and_publication_must_match_identity():
    history = _valid_reel_history()
    history["reel_publications"][0]["release_identity"]["manifest_sha256"] = "b" * 64

    with pytest.raises(history_tracker.CorruptedHistoryError, match="identity"):
        history_tracker._validated_reel_history(history)


def test_reel_history_rejects_duplicate_and_cross_format_publication_or_media_ids():
    history = _valid_reel_history()
    history["publications"] = [
        {
            "id": PUBLICATION_ID,
            "type": "single",
            "media_id": "media-1",
            "artwork_ids": ["met_1"],
            "posted_at": "2026-09-11T12:03:00Z",
        }
    ]
    history["grid_publication_count"] = 1

    with pytest.raises(history_tracker.CorruptedHistoryError, match="conflicts"):
        history_tracker._validated_reel_history(history)


def test_reel_ambiguous_blocks_feed_reservation(monkeypatch):
    history = _valid_reel_history(status="AMBIGUOUS")
    _install_history(monkeypatch, history)

    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_artwork({"id": "artic_84774"})


@pytest.mark.parametrize("status", ["PENDING", "PUBLISHED"])
def test_active_or_published_reel_blocks_feed_reservation(monkeypatch, status):
    history = _valid_reel_history(status=status)
    if status == "PENDING":
        history["reel_reservations"][0]["reserved_at"] = (
            datetime.now(timezone.utc) - history_tracker.PENDING_RESERVATION_TTL / 4
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _install_history(monkeypatch, history)

    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_artwork({"id": "artic_84774"})


def test_feed_published_blocks_reel_reservation(monkeypatch):
    history = {
        "posted_artworks": [{"id": "artic_84774", "status": "PUBLISHED"}],
    }
    uploads = _install_history(monkeypatch, history)

    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_reel(
            "aic_84774", ReelReleaseIdentity.model_validate(_identity())
        )
    assert uploads == []


def test_reserve_reel_is_idempotent_only_for_exact_identity(monkeypatch):
    history = {"posted_artworks": []}
    uploads = _install_history(monkeypatch, history)
    identity = ReelReleaseIdentity.model_validate(_identity())

    assert history_tracker.reserve_reel("artic_84774", identity, PUBLICATION_ID) == PUBLICATION_ID
    assert history_tracker.reserve_reel("aic_84774", identity, PUBLICATION_ID) == PUBLICATION_ID
    assert len(uploads) == 1
    assert history["reel_reservations"][0]["artwork_id"] == "aic_84774"

    different = identity.model_copy(update={"manifest_sha256": "b" * 64})
    with pytest.raises(RuntimeError, match="conflicting"):
        history_tracker.reserve_reel("aic_84774", different, PUBLICATION_ID)


@pytest.mark.parametrize("status", ["EXPIRED", "PENDING"])
def test_expired_or_stale_pending_reel_does_not_protect(status):
    history = _valid_reel_history(status=status)
    if status == "PENDING":
        history["reel_reservations"][0]["reserved_at"] = (
            NOW - history_tracker.PENDING_RESERVATION_TTL - timedelta(seconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert not history_tracker.artwork_is_globally_protected(
        history, "artic_84774", now=NOW
    )


def test_reserve_reel_appends_after_expired_reel_audit_record(monkeypatch):
    history = _valid_reel_history(status="EXPIRED")
    uploads = _install_history(monkeypatch, history)
    identity = ReelReleaseIdentity.model_validate(_identity())
    next_id = "22345678-1234-4234-8234-123456789abc"

    assert history_tracker.reserve_reel("artic_84774", identity, next_id) == next_id
    assert [row["publication_id"] for row in history["reel_reservations"]] == [
        PUBLICATION_ID,
        next_id,
    ]
    assert len(uploads) == 1


def test_global_protection_includes_confirmed_feed_and_reel_publications_without_locks():
    history = {
        "posted_artworks": [],
        "publications": [
            {
                "id": "feed-publication",
                "type": "single",
                "media_id": "feed-media",
                "artwork_ids": ["artic_84774"],
                "posted_at": "2026-09-11T12:03:00Z",
            }
        ],
        "grid_publication_count": 1,
        "reel_publications": [_publication(artwork_id="met_1")],
        "reel_publication_count": 1,
    }

    assert history_tracker.globally_protected_artwork_ids(history, now=NOW) == {
        "aic_84774",
        "met_1",
    }


def test_reel_reservation_retries_after_conditional_conflict(monkeypatch):
    current = {"posted_artworks": []}
    snapshots = []
    attempts = 0

    def load():
        snapshots.append(copy.deepcopy(current))
        return snapshots[-1], '"etag-1"'

    def upload(value, etag):
        nonlocal attempts, current
        attempts += 1
        assert etag == '"etag-1"'
        if attempts == 1:
            raise history_tracker.ConcurrentWriteError("race")
        current = copy.deepcopy(value)

    monkeypatch.setattr(history_tracker, "load_history_with_etag", load)
    monkeypatch.setattr(history_tracker, "_upload_history", upload)

    identity = ReelReleaseIdentity.model_validate(_identity())
    assert history_tracker.reserve_reel("aic_84774", identity, PUBLICATION_ID) == PUBLICATION_ID
    assert attempts == 2
    assert len(current["reel_reservations"]) == 1
    assert UUID(current["reel_reservations"][0]["publication_id"]) == UUID(PUBLICATION_ID)


def test_reel_reservation_raises_after_bounded_conditional_conflicts(monkeypatch):
    history = {"posted_artworks": []}
    loads = 0
    uploads = 0

    def load():
        nonlocal loads
        loads += 1
        return copy.deepcopy(history), '"etag-1"'

    def upload(value, etag):
        nonlocal uploads
        uploads += 1
        assert etag == '"etag-1"'
        raise history_tracker.ConcurrentWriteError("race")

    monkeypatch.setattr(history_tracker, "load_history_with_etag", load)
    monkeypatch.setattr(history_tracker, "_upload_history", upload)
    identity = ReelReleaseIdentity.model_validate(_identity())

    with pytest.raises(history_tracker.ConcurrentWriteError):
        history_tracker.reserve_reel("aic_84774", identity, PUBLICATION_ID)
    assert loads == history_tracker.HISTORY_CONDITIONAL_WRITE_ATTEMPTS
    assert uploads == history_tracker.HISTORY_CONDITIONAL_WRITE_ATTEMPTS


def test_feed_and_reel_race_has_one_winner_after_reloading_same_etag(monkeypatch):
    current = {"posted_artworks": []}
    first_history = copy.deepcopy(current)
    second_history = copy.deepcopy(current)
    loads = iter([(first_history, '"etag-1"'), (second_history, '"etag-1"')])
    upload_count = 0

    def load():
        try:
            return next(loads)
        except StopIteration:
            return copy.deepcopy(current), '"etag-2"'

    def upload(value, etag):
        nonlocal upload_count, current
        upload_count += 1
        if upload_count == 1:
            current = copy.deepcopy(value)
            return
        raise history_tracker.ConcurrentWriteError("stale second writer")

    monkeypatch.setattr(history_tracker, "load_history_with_etag", load)
    monkeypatch.setattr(history_tracker, "_upload_history", upload)
    identity = ReelReleaseIdentity.model_validate(_identity())

    assert history_tracker.reserve_artwork({"id": "artic_84774"}, "feed-winner") == "feed-winner"
    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_reel("aic_84774", identity, PUBLICATION_ID)
    assert upload_count == 2
