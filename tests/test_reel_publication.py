"""Happy-path orchestration tests for the manual Reel publication command."""

import copy
import hashlib
import json
import logging
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, Mock

import pytest
import requests

from scripts import publish_reel
from src import (
    history_tracker,
    instagram_poster,
    r2_media,
    reel_publication,
    reel_release,
)
from src.models import (
    ReelPublicationRecord,
    ReelPublicationStatus,
    ReelReleaseIdentity,
)


CREATED_AT = "2026-09-11T11:55:00.000Z"
REEL_ID = "met_123"
PUBLICATION_ID = "12345678-1234-4234-8234-123456789abc"
CONTAINER_ID = "container-1"
MEDIA_ID = "media-1"
ACCOUNT_ID = "account"
ACCESS_TOKEN = "token"
PERMALINK = "https://www.instagram.com/reel/example/"
FEED_KEYS = ("posted_artworks", "publications", "grid_publication_count", "active_color_tone")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_release(tmp_path: Path) -> Path:
    release = tmp_path / "artfolio-reels" / "output" / "releases" / REEL_ID
    (release / "qc").mkdir(parents=True)
    metadata = {
        "canonicalId": REEL_ID,
        "reelId": REEL_ID,
        "title": "The verified artwork",
        "template": "museum-reel-v1",
        "durationSeconds": 12.0,
        "hook": "A verified hook.",
        "generatedAt": CREATED_AT,
    }
    file_data = {
        "reel.mp4": b"verified mp4 bytes",
        "caption.txt": b"A verified caption.\n",
        "metadata.json": (json.dumps(metadata, sort_keys=True) + "\n").encode(),
        "qc/contact-sheet.png": b"verified contact sheet bytes",
    }
    for relative_path, data in file_data.items():
        path = release / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "version": "artfolio-release-v1",
        "reelId": REEL_ID,
        "createdAt": CREATED_AT,
        "files": {
            "video": "reel.mp4",
            "caption": "caption.txt",
            "metadata": "metadata.json",
            "qcContactSheet": "qc/contact-sheet.png",
        },
        "sha256": {
            relative_path: _sha256(data)
            for relative_path, data in file_data.items()
        },
    }
    (release / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return release


def install_verifier(
    monkeypatch: pytest.MonkeyPatch, release: Path, *, valid: bool = True
) -> None:
    result = {
        "valid": True,
        "errors": [],
        "directory": str(release.resolve()),
        "reelId": REEL_ID,
        "media": {
            "path": str((release / "reel.mp4").resolve()),
            "sizeBytes": (release / "reel.mp4").stat().st_size,
            "durationSeconds": 12.0,
            "deep": True,
            "video": {"codec": "h264", "width": 1080, "height": 1920, "fps": 30},
            "audio": {"codec": "aac"},
            "maxVolumeDb": -12.5,
        },
    }
    if not valid:
        result = {"valid": False, "errors": ["deep verification failed"]}

    def fake_run(*args, **kwargs):
        assert args[0][0:4] == ["npm", "run", "reels:verify-release", "--"]
        assert args[0][-2:] == ["--deep", "--json"]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 300
        return subprocess.CompletedProcess(
            args[0],
            0,
            "> artfolio-reels@1.0.0 reels:verify-release\n"
            "> tsx scripts/verify-release.ts --deep --json\n\n"
            + json.dumps(result)
            + "\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)


def _release_identity(release: Path) -> ReelReleaseIdentity:
    manifest_bytes = (release / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    return ReelReleaseIdentity.model_validate(
        {
            "version": manifest["version"],
            "reel_id": manifest["reelId"],
            "created_at": manifest["createdAt"],
            "manifest_sha256": _sha256(manifest_bytes),
            "files_sha256": {
                relative_path: _sha256((release / relative_path).read_bytes())
                for relative_path in manifest["files"].values()
            },
        }
    )


def _object_key(publication_id: str) -> str:
    return (
        f"reels/publications/{publication_id}/"
        "20260911120300_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.mp4"
    )


def _public_url(publication_id: str) -> str:
    return f"https://media.example/{_object_key(publication_id)}"


def _pending_history(release_identity: ReelReleaseIdentity) -> dict:
    return {
        "posted_artworks": [{"id": "feed-art", "status": "PUBLISHED"}],
        "publications": [],
        "grid_publication_count": 0,
        "active_color_tone": "cool",
        "reel_reservations": [
            {
                "publication_id": PUBLICATION_ID,
                "artwork_id": REEL_ID,
                "status": "PENDING",
                "reserved_at": "2026-09-11T12:00:00Z",
                "release_identity": release_identity.model_dump(mode="json"),
                "staging": {
                    "object_key": _object_key(PUBLICATION_ID),
                    "public_url": _public_url(PUBLICATION_ID),
                    "staged_at": "2026-09-11T12:01:00Z",
                },
            }
        ],
        "reel_publications": [],
        "reel_publication_count": 0,
        "reel_staging_cleanup_queue": [],
        "unknown_top_level_key": {"preserve": True},
    }


def install_memory_history(
    monkeypatch: pytest.MonkeyPatch, history: dict, events: list
) -> None:
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, '"etag-1"')
    )

    def upload(value, etag):
        reservation = value["reel_reservations"][0]
        events.append(
            f"history_put:{reservation['status']}:{reservation.get('container_id', '')}"
        )

    monkeypatch.setattr(history_tracker, "_upload_history", upload)


def _publication_record(release_identity: ReelReleaseIdentity) -> ReelPublicationRecord:
    return ReelPublicationRecord.model_validate(
        {
            "id": PUBLICATION_ID,
            "artwork_id": REEL_ID,
            "media_id": MEDIA_ID,
            "posted_at": "2026-09-11T12:03:00Z",
            "permalink": PERMALINK,
            "release_identity": release_identity.model_dump(mode="json"),
        }
    )


def test_publish_verified_reel_happy_path_publishes_one_verified_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    release = build_release(tmp_path)
    install_verifier(monkeypatch, release)
    release_identity = _release_identity(release)
    history = _pending_history(release_identity)
    events: list = []
    install_memory_history(monkeypatch, history, events)

    verified_holder = {}
    real_snapshot = reel_release.verified_reel_release_snapshot

    @contextmanager
    def recording_snapshot(release_arg, *, reels_repository, snapshot_root=None):
        events.append("verify_and_snapshot")
        with real_snapshot(
            release_arg,
            reels_repository=reels_repository,
            snapshot_root=snapshot_root,
        ) as verified:
            verified_holder["verified"] = verified
            yield verified

    monkeypatch.setattr(
        reel_release, "verified_reel_release_snapshot", recording_snapshot
    )

    reserve_calls = []

    def fake_reserve(artwork_id, identity, publication_id=None):
        events.append("reserve_reel")
        reserve_calls.append((artwork_id, identity, publication_id))
        return PUBLICATION_ID

    monkeypatch.setattr(history_tracker, "reserve_reel", fake_reserve)

    stage_calls = []
    staged_upload_holder = {}

    def fake_stage(file_path, publication_id):
        events.append(f"stage_reel_mp4:{file_path}")
        stage_calls.append((file_path, publication_id))
        upload = r2_media.TempReelUpload(
            _object_key(publication_id), _public_url(publication_id), publication_id
        )
        staged_upload_holder["upload"] = upload
        return upload

    monkeypatch.setattr(r2_media, "stage_reel_mp4", fake_stage)

    staging_calls = []

    def fake_record_staging(publication_id, identity, upload):
        events.append("record_reel_staging")
        staging_calls.append((publication_id, identity, upload))

    monkeypatch.setattr(history_tracker, "record_reel_staging", fake_record_staging)

    publisher_kwargs = {}

    def fake_publisher(**kwargs):
        events.append("post_to_instagram_graph_api")
        publisher_kwargs.update(kwargs)
        events.append(f"before_publish:{CONTAINER_ID}:{()}")
        kwargs["before_publish"](CONTAINER_ID, ())
        events.append("media_publish")
        return MEDIA_ID

    monkeypatch.setattr(instagram_poster, "post_to_instagram_graph_api", fake_publisher)

    receipt_calls = []

    def fake_record_publish_response(publication_id, identity, media_id):
        events.append(f"record_reel_publish_response:{media_id}")
        receipt_calls.append((publication_id, identity, media_id))

    monkeypatch.setattr(
        history_tracker, "record_reel_publish_response", fake_record_publish_response
    )

    permalink_calls = []

    def fake_get_permalink(media_id, access_token):
        events.append(f"get_permalink:{media_id}")
        permalink_calls.append((media_id, access_token))
        return PERMALINK

    monkeypatch.setattr(
        instagram_poster, "get_instagram_permalink", fake_get_permalink
    )

    finalize_calls = []
    publication_record = _publication_record(release_identity)

    def fake_finalize(publication_id, identity, media_id, *, permalink=None, **_kwargs):
        events.append(f"finalize_reel_publication:{media_id}")
        finalize_calls.append((publication_id, identity, media_id, permalink))
        return publication_record

    monkeypatch.setattr(history_tracker, "finalize_reel_publication", fake_finalize)

    result = reel_publication.publish_verified_reel(
        release=release,
        reels_repository=tmp_path / "artfolio-reels",
        account_id=ACCOUNT_ID,
        access_token=ACCESS_TOKEN,
    )

    verified = verified_holder["verified"]
    assert events == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:PUBLISHING:container-1",
        "media_publish",
        f"record_reel_publish_response:{MEDIA_ID}",
        f"get_permalink:{MEDIA_ID}",
        f"finalize_reel_publication:{MEDIA_ID}",
    ]
    assert publisher_kwargs == {
        "media_url": _public_url(PUBLICATION_ID),
        "caption": verified.caption,
        "account_id": ACCOUNT_ID,
        "access_token": ACCESS_TOKEN,
        "media_type": "REELS",
        "before_publish": ANY,
    }
    assert reserve_calls == [(verified.artwork_id, verified.release_identity, None)]
    assert stage_calls == [(str(verified.video_path), PUBLICATION_ID)]
    staged_source = Path(stage_calls[0][0])
    assert staged_source.is_relative_to(verified.snapshot_directory)
    assert staged_source != release / "reel.mp4"
    assert staging_calls == [
        (PUBLICATION_ID, verified.release_identity, staged_upload_holder["upload"])
    ]
    reservation = history["reel_reservations"][0]
    assert reservation["status"] == "PUBLISHING"
    assert reservation["container_id"] == CONTAINER_ID
    assert reservation["publish_started_at"]
    assert receipt_calls == [(PUBLICATION_ID, verified.release_identity, MEDIA_ID)]
    assert permalink_calls == [(MEDIA_ID, ACCESS_TOKEN)]
    assert finalize_calls == [
        (PUBLICATION_ID, verified.release_identity, MEDIA_ID, PERMALINK)
    ]
    assert result is publication_record


def _publication_history(
    release_identity: ReelReleaseIdentity,
    *,
    staged: bool = False,
    status: str = "PENDING",
    receipt: str | None = None,
) -> dict:
    reservation = {
        "publication_id": PUBLICATION_ID,
        "artwork_id": REEL_ID,
        "status": status,
        "reserved_at": "2026-09-11T12:00:00Z",
        "release_identity": release_identity.model_dump(mode="json"),
    }
    if staged:
        reservation["staging"] = {
            "object_key": _object_key(PUBLICATION_ID),
            "public_url": _public_url(PUBLICATION_ID),
            "staged_at": "2026-09-11T12:01:00Z",
        }
    if status in {"PUBLISHING", "AMBIGUOUS", "PUBLISHED"}:
        reservation["container_id"] = CONTAINER_ID
        reservation["publish_started_at"] = "2026-09-11T12:02:00Z"
    if receipt is not None:
        reservation["publish_response_media_id"] = receipt
    if status == "AMBIGUOUS":
        reservation["ambiguous_at"] = "2026-09-11T12:03:00Z"
        reservation["ambiguity_reason"] = "response unavailable"
    if status == "PUBLISHED":
        reservation["media_id"] = receipt or MEDIA_ID
        reservation["posted_at"] = "2026-09-11T12:03:00Z"
    return {
        "posted_artworks": [{"id": "feed-art", "status": "PUBLISHED"}],
        "publications": [],
        "grid_publication_count": 0,
        "active_color_tone": "cool",
        "reel_reservations": [reservation],
        "reel_publications": [],
        "reel_publication_count": 0,
        "reel_staging_cleanup_queue": [],
        "unknown_top_level_key": {"preserve": True},
    }


def install_publication_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    staged: bool = False,
    verifier_valid: bool = True,
    seed_status: str = "PENDING",
    seed_receipt: str | None = None,
    reserve_error: Exception | None = None,
    stage_error: Exception | None = None,
    staging_error: Exception | None = None,
    start_error: Exception | None = None,
    start_write_then_raise: bool = False,
    publisher_error: Exception | None = None,
    publisher_post_boundary_error: Exception | None = None,
    expire_error: Exception | None = None,
    cleanup_handle_error: Exception | None = None,
    cleanup_prefix_error: Exception | None = None,
    reread_error: Exception | None = None,
    receipt_error: Exception | None = None,
    receipt_real: bool = False,
    finalize_error: Exception | None = None,
    permalink_error: Exception | None = None,
    permalink_result: str | None = PERMALINK,
    finalize_real: bool = False,
) -> dict:
    release = build_release(tmp_path)
    install_verifier(monkeypatch, release, valid=verifier_valid)
    release_identity = _release_identity(release)
    history = _publication_history(
        release_identity, staged=staged, status=seed_status, receipt=seed_receipt
    )
    events: list = []
    install_memory_history(monkeypatch, history, events)

    handles: dict = {
        "release": release,
        "identity": release_identity,
        "history": history,
        "events": events,
        "verified_holder": {},
        "upload": None,
    }

    real_snapshot = reel_release.verified_reel_release_snapshot

    @contextmanager
    def recording_snapshot(release_arg, *, reels_repository, snapshot_root=None):
        events.append("verify_and_snapshot")
        with real_snapshot(
            release_arg,
            reels_repository=reels_repository,
            snapshot_root=snapshot_root,
        ) as verified:
            handles["verified_holder"]["verified"] = verified
            yield verified

    monkeypatch.setattr(
        reel_release, "verified_reel_release_snapshot", recording_snapshot
    )

    def fake_reserve(artwork_id, identity, publication_id=None):
        events.append("reserve_reel")
        if reserve_error is not None:
            raise reserve_error
        return PUBLICATION_ID

    handles["reserve_mock"] = Mock(side_effect=fake_reserve)
    monkeypatch.setattr(history_tracker, "reserve_reel", handles["reserve_mock"])

    def fake_stage(file_path, publication_id):
        events.append(f"stage_reel_mp4:{file_path}")
        if stage_error is not None:
            raise stage_error
        upload = r2_media.TempReelUpload(
            _object_key(publication_id), _public_url(publication_id), publication_id
        )
        handles["upload"] = upload
        return upload

    handles["stage_mock"] = Mock(side_effect=fake_stage)
    monkeypatch.setattr(r2_media, "stage_reel_mp4", handles["stage_mock"])

    def fake_record_staging(publication_id, identity, upload):
        events.append("record_reel_staging")
        if staging_error is not None:
            raise staging_error

    handles["record_staging_mock"] = Mock(side_effect=fake_record_staging)
    monkeypatch.setattr(
        history_tracker, "record_reel_staging", handles["record_staging_mock"]
    )

    if start_error is not None:
        handles["start_mock"] = Mock(side_effect=start_error)
        monkeypatch.setattr(
            history_tracker,
            "start_reel_publication_attempt",
            handles["start_mock"],
        )
    elif start_write_then_raise:
        real_start = history_tracker.start_reel_publication_attempt

        def start_then_raise(publication_id, identity, container_id):
            real_start(publication_id, identity, container_id)
            raise RuntimeError("boundary confirmation lost")

        monkeypatch.setattr(
            history_tracker, "start_reel_publication_attempt", start_then_raise
        )

    def fake_publisher(**kwargs):
        events.append("post_to_instagram_graph_api")
        if publisher_error is not None:
            raise publisher_error
        events.append(f"before_publish:{CONTAINER_ID}:{()}")
        try:
            kwargs["before_publish"](CONTAINER_ID, ())
        except Exception as error:
            raise instagram_poster.InstagramPrePublishBoundaryError(
                "The durable pre-publish callback failed before media_publish."
            ) from error
        if publisher_post_boundary_error is not None:
            raise publisher_post_boundary_error
        events.append("media_publish")
        return MEDIA_ID

    handles["publisher_mock"] = Mock(side_effect=fake_publisher)
    monkeypatch.setattr(
        instagram_poster, "post_to_instagram_graph_api", handles["publisher_mock"]
    )

    if expire_error is not None:
        handles["expire_mock"] = Mock(side_effect=expire_error)
        monkeypatch.setattr(
            history_tracker,
            "expire_reel_before_media_publish",
            handles["expire_mock"],
        )
    else:
        handles["expire_mock"] = None

    def fake_cleanup_handle(upload, *, reason):
        events.append("cleanup_temp_reel_upload")
        if cleanup_handle_error is not None:
            raise cleanup_handle_error
        return True

    handles["cleanup_handle_mock"] = Mock(side_effect=fake_cleanup_handle)
    monkeypatch.setattr(
        r2_media, "cleanup_temp_reel_upload", handles["cleanup_handle_mock"]
    )

    def fake_cleanup_prefix(publication_id, *, reason):
        events.append("cleanup_publication_reels")
        if cleanup_prefix_error is not None:
            raise cleanup_prefix_error
        return r2_media.MediaCleanupSummary(
            publication_id=publication_id,
            discovered=1,
            deleted=1,
            failures=0,
            complete=True,
            reason=reason,
        )

    handles["cleanup_prefix_mock"] = Mock(side_effect=fake_cleanup_prefix)
    monkeypatch.setattr(
        r2_media, "cleanup_publication_reels", handles["cleanup_prefix_mock"]
    )

    if reread_error is not None:
        handles["reread_mock"] = Mock(side_effect=reread_error)
        monkeypatch.setattr(
            history_tracker, "get_reel_reservation", handles["reread_mock"]
        )
    else:
        handles["reread_mock"] = None

    def fake_record_receipt(publication_id, identity, media_id):
        events.append(f"record_reel_publish_response:{media_id}")
        if receipt_error is not None:
            raise receipt_error

    if receipt_real:
        handles["receipt_mock"] = None
    else:
        handles["receipt_mock"] = Mock(side_effect=fake_record_receipt)
        monkeypatch.setattr(
            history_tracker,
            "record_reel_publish_response",
            handles["receipt_mock"],
        )

    def fake_get_permalink(media_id, access_token):
        events.append(f"get_permalink:{media_id}")
        if permalink_error is not None:
            raise permalink_error
        return permalink_result

    handles["permalink_mock"] = Mock(side_effect=fake_get_permalink)
    monkeypatch.setattr(
        instagram_poster, "get_instagram_permalink", handles["permalink_mock"]
    )

    publication_record = _publication_record(release_identity)

    def fake_finalize(publication_id, identity, media_id, *, permalink=None, **_kwargs):
        events.append(f"finalize_reel_publication:{media_id}")
        if finalize_error is not None:
            raise finalize_error
        return publication_record

    if finalize_real:
        handles["finalize_mock"] = None
    else:
        handles["finalize_mock"] = Mock(side_effect=fake_finalize)
        monkeypatch.setattr(
            history_tracker, "finalize_reel_publication", handles["finalize_mock"]
        )

    return handles


def _run_publish(handles: dict, tmp_path: Path):
    return reel_publication.publish_verified_reel(
        release=handles["release"],
        reels_repository=tmp_path / "artfolio-reels",
        account_id=ACCOUNT_ID,
        access_token=ACCESS_TOKEN,
    )


def history_queue(handles: dict) -> list:
    return handles["history"]["reel_staging_cleanup_queue"]


def test_invalid_intake_performs_no_reservation_staging_or_instagram_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch, tmp_path, verifier_valid=False
    )

    with pytest.raises(reel_release.ReleaseIntakeError):
        _run_publish(handles, tmp_path)

    assert handles["events"] == ["verify_and_snapshot"]
    handles["reserve_mock"].assert_not_called()
    handles["stage_mock"].assert_not_called()
    handles["publisher_mock"].assert_not_called()
    handles["permalink_mock"].assert_not_called()
    handles["finalize_mock"].assert_not_called()


def test_reserve_failure_performs_no_staging_or_instagram_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        reserve_error=RuntimeError("artwork is already protected"),
    )

    with pytest.raises(RuntimeError, match="already protected"):
        _run_publish(handles, tmp_path)

    assert handles["events"] == ["verify_and_snapshot", "reserve_reel"]
    handles["stage_mock"].assert_not_called()
    handles["record_staging_mock"].assert_not_called()
    handles["publisher_mock"].assert_not_called()
    handles["permalink_mock"].assert_not_called()
    handles["finalize_mock"].assert_not_called()


def test_stage_failure_expires_and_queues_reel_cleanup_without_instagram_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        stage_error=ValueError("Reel staging failed its public health check"),
    )

    with pytest.raises(ValueError, match="public health check"):
        _run_publish(handles, tmp_path)

    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "history_put:EXPIRED:",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "EXPIRED"
    assert reservation["expiration_reason"] == "reel_staging_failed"
    assert reservation["expired_at"]
    assert [entry["publication_id"] for entry in history_queue(handles)] == [
        PUBLICATION_ID
    ]
    handles["record_staging_mock"].assert_not_called()
    handles["publisher_mock"].assert_not_called()
    handles["permalink_mock"].assert_not_called()
    handles["finalize_mock"].assert_not_called()
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()


def test_staging_persist_failure_expires_then_cleans_handle_and_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staging_error=RuntimeError("staging handle could not be persisted"),
    )

    with pytest.raises(RuntimeError, match="staging handle"):
        _run_publish(handles, tmp_path)

    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "history_put:EXPIRED:",
        "cleanup_temp_reel_upload",
        "cleanup_publication_reels",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "EXPIRED"
    assert reservation["expiration_reason"] == "reel_staging_handle_persistence_failed"
    assert [entry["publication_id"] for entry in history_queue(handles)] == [
        PUBLICATION_ID
    ]
    handles["cleanup_handle_mock"].assert_called_once_with(
        handles["upload"], reason="reel_staging_handle_persistence_failed"
    )
    handles["cleanup_prefix_mock"].assert_called_once_with(
        PUBLICATION_ID, reason="reel_staging_handle_persistence_failed"
    )
    handles["publisher_mock"].assert_not_called()
    handles["permalink_mock"].assert_not_called()
    handles["finalize_mock"].assert_not_called()


def test_staging_persist_failure_without_history_still_rolls_back_exact_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staging_error=RuntimeError("staging handle could not be persisted"),
        expire_error=history_tracker.ConcurrentWriteError(
            "Conditional R2 history write failed"
        ),
    )

    with pytest.raises(RuntimeError, match="staging handle"):
        _run_publish(handles, tmp_path)

    handles["expire_mock"].assert_called_once()
    expire_args, expire_kwargs = handles["expire_mock"].call_args
    assert expire_args == (PUBLICATION_ID, handles["identity"])
    assert expire_kwargs["reason"] == "reel_staging_handle_persistence_failed"
    assert expire_kwargs["expected_status"] is ReelPublicationStatus.PENDING
    handles["cleanup_handle_mock"].assert_called_once_with(
        handles["upload"], reason="reel_staging_handle_persistence_failed"
    )
    handles["cleanup_prefix_mock"].assert_not_called()
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "PENDING"
    assert history_queue(handles) == []
    handles["publisher_mock"].assert_not_called()


def test_staging_persist_failure_cleanup_errors_do_not_mask_the_original_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staging_error=RuntimeError("staging handle could not be persisted"),
        cleanup_handle_error=RuntimeError("r2 delete failed"),
        cleanup_prefix_error=RuntimeError("r2 list failed"),
    )

    with pytest.raises(RuntimeError, match="staging handle"):
        _run_publish(handles, tmp_path)

    handles["cleanup_handle_mock"].assert_called_once()
    handles["cleanup_prefix_mock"].assert_called_once()
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "EXPIRED"
    assert [entry["publication_id"] for entry in history_queue(handles)] == [
        PUBLICATION_ID
    ]


def test_container_failure_before_callback_expires_queues_and_cleans_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        publisher_error=instagram_poster.InstagramAPIError(
            "Reel container creation failed"
        ),
    )

    with pytest.raises(
        instagram_poster.InstagramAPIError, match="container creation failed"
    ):
        _run_publish(handles, tmp_path)

    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        "history_put:EXPIRED:",
        "cleanup_publication_reels",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "EXPIRED"
    assert reservation["expiration_reason"] == "reel_container_failed_before_publish"
    assert [entry["publication_id"] for entry in history_queue(handles)] == [
        PUBLICATION_ID
    ]
    handles["cleanup_prefix_mock"].assert_called_once_with(
        PUBLICATION_ID, reason="reel_container_failed_before_publish"
    )
    handles["cleanup_handle_mock"].assert_not_called()
    assert "media_publish" not in handles["events"]
    handles["finalize_mock"].assert_not_called()


def test_boundary_error_expires_from_pending_reread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch, tmp_path, staged=True, start_error=RuntimeError("history write failed")
    )

    with pytest.raises(instagram_poster.InstagramPrePublishBoundaryError):
        _run_publish(handles, tmp_path)

    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:EXPIRED:",
        "cleanup_publication_reels",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "EXPIRED"
    assert reservation["expiration_reason"] == "reel_pre_publish_boundary_unconfirmed"
    assert [entry["publication_id"] for entry in history_queue(handles)] == [
        PUBLICATION_ID
    ]
    assert "media_publish" not in handles["events"]
    handles["cleanup_handle_mock"].assert_not_called()
    handles["finalize_mock"].assert_not_called()


def test_boundary_error_expires_from_container_backed_publishing_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch, tmp_path, staged=True, start_write_then_raise=True
    )

    with pytest.raises(instagram_poster.InstagramPrePublishBoundaryError):
        _run_publish(handles, tmp_path)

    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:PUBLISHING:container-1",
        "history_put:EXPIRED:container-1",
        "cleanup_publication_reels",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "EXPIRED"
    assert reservation["expiration_reason"] == "reel_pre_publish_boundary_unconfirmed"
    assert [entry["publication_id"] for entry in history_queue(handles)] == [
        PUBLICATION_ID
    ]
    assert "media_publish" not in handles["events"]


@pytest.mark.parametrize(
    "seed_status,seed_receipt",
    [("PUBLISHING", "media-accepted-elsewhere"), ("AMBIGUOUS", None)],
)
def test_boundary_error_fails_closed_when_state_is_not_provably_pre_meta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seed_status: str,
    seed_receipt: str | None,
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        seed_status=seed_status,
        seed_receipt=seed_receipt,
        start_error=RuntimeError("history write failed"),
    )

    with pytest.raises(instagram_poster.InstagramPrePublishBoundaryError):
        _run_publish(handles, tmp_path)

    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == seed_status
    assert history_queue(handles) == []
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()
    assert "media_publish" not in handles["events"]


def test_boundary_error_fails_closed_when_reread_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        start_error=RuntimeError("history write failed"),
        reread_error=RuntimeError("history unavailable"),
    )

    with pytest.raises(instagram_poster.InstagramPrePublishBoundaryError):
        _run_publish(handles, tmp_path)

    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "PENDING"
    assert history_queue(handles) == []
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()
    assert "media_publish" not in handles["events"]


POST_BOUNDARY_ERRORS = [
    pytest.param(
        instagram_poster.InstagramAPIError(
            "Media was rejected by Instagram", status_code=400
        ),
        id="parsed-4xx",
    ),
    pytest.param(
        instagram_poster.InstagramAPIError("Instagram server error", status_code=502),
        id="5xx",
    ),
    pytest.param(
        instagram_poster.InstagramPublishAmbiguousError(
            "Publish outcome is unknown after a network failure"
        ),
        id="ambiguous-network",
    ),
    pytest.param(
        instagram_poster.InstagramPublishAmbiguousError(
            "Instagram publish outcome is unknown because the response was malformed"
        ),
        id="ambiguous-malformed",
    ),
    pytest.param(
        instagram_poster.InstagramPublishAmbiguousError(
            "Instagram publish outcome is unknown because no media id was returned"
        ),
        id="ambiguous-missing-id",
    ),
    pytest.param(
        requests.exceptions.Timeout("Instagram publish timed out"),
        id="timeout",
    ),
    pytest.param(
        requests.exceptions.ConnectionError("connection reset by peer"),
        id="connection-reset",
    ),
    pytest.param(
        RuntimeError("totally unexpected publisher failure"),
        id="unexpected",
    ),
]


@pytest.mark.parametrize("publisher_failure", POST_BOUNDARY_ERRORS)
def test_post_boundary_publisher_failure_is_conservatively_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_failure: Exception
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        publisher_post_boundary_error=publisher_failure,
    )
    feed_before = {
        key: copy.deepcopy(handles["history"][key]) for key in FEED_KEYS
    }

    with pytest.raises(type(publisher_failure)) as excinfo:
        _run_publish(handles, tmp_path)

    assert excinfo.value is publisher_failure
    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:PUBLISHING:container-1",
        "history_put:AMBIGUOUS:container-1",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "AMBIGUOUS"
    assert reservation["ambiguity_reason"] == "reel_publish_outcome_unverified"
    assert reservation["ambiguous_at"]
    assert reservation["staging"] is not None
    assert reservation["container_id"] == CONTAINER_ID
    assert history_queue(handles) == []
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()
    handles["publisher_mock"].assert_called_once()
    handles["permalink_mock"].assert_not_called()
    handles["finalize_mock"].assert_not_called()
    assert {key: handles["history"][key] for key in FEED_KEYS} == feed_before
    assert REEL_ID in history_tracker.globally_protected_artwork_ids(
        handles["history"], now=datetime.now(timezone.utc)
    )


def test_receipt_failure_marks_ambiguous_and_raises_persistence_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        receipt_error=history_tracker.ConcurrentWriteError(
            "Conditional R2 history write failed"
        ),
    )

    with pytest.raises(
        reel_publication.ReelPublicationPersistenceError
    ) as excinfo:
        _run_publish(handles, tmp_path)

    message = str(excinfo.value)
    assert PUBLICATION_ID in message
    assert ACCESS_TOKEN not in message
    assert ACCOUNT_ID not in message
    assert "A verified caption." not in message
    assert "media.example" not in message
    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:PUBLISHING:container-1",
        "media_publish",
        f"record_reel_publish_response:{MEDIA_ID}",
        "history_put:AMBIGUOUS:container-1",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "AMBIGUOUS"
    assert reservation["ambiguity_reason"] == "reel_receipt_not_durable"
    assert reservation.get("publish_response_media_id") is None
    assert reservation["staging"] is not None
    assert history_queue(handles) == []
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()
    handles["publisher_mock"].assert_called_once()
    assert handles["events"].count("media_publish") == 1
    handles["finalize_mock"].assert_not_called()


def test_finalize_failure_preserves_receipt_marks_ambiguous_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        receipt_real=True,
        finalize_error=RuntimeError("finalization lost a CAS race"),
    )

    with pytest.raises(
        reel_publication.ReelPublicationPersistenceError
    ) as excinfo:
        _run_publish(handles, tmp_path)

    message = str(excinfo.value)
    assert PUBLICATION_ID in message
    assert ACCESS_TOKEN not in message
    assert ACCOUNT_ID not in message
    assert "A verified caption." not in message
    assert "media.example" not in message
    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:PUBLISHING:container-1",
        "media_publish",
        "history_put:PUBLISHING:container-1",
        "get_permalink:media-1",
        f"finalize_reel_publication:{MEDIA_ID}",
        "history_put:AMBIGUOUS:container-1",
    ]
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "AMBIGUOUS"
    assert reservation["publish_response_media_id"] == MEDIA_ID
    assert reservation.get("media_id") is None
    assert reservation["ambiguity_reason"] == "reel_finalization_not_durable"
    assert reservation["staging"] is not None
    assert history_queue(handles) == []
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()
    handles["publisher_mock"].assert_called_once()
    assert handles["events"].count("media_publish") == 1
    assert REEL_ID in history_tracker.globally_protected_artwork_ids(
        handles["history"], now=datetime.now(timezone.utc)
    )


def _assert_published_without_permalink(handles: dict) -> None:
    result = handles["result"]
    assert isinstance(result, ReelPublicationRecord)
    assert result.id == PUBLICATION_ID
    assert result.media_id == MEDIA_ID
    assert result.permalink is None
    reservation = handles["history"]["reel_reservations"][0]
    assert reservation["status"] == "PUBLISHED"
    assert reservation["publish_response_media_id"] == MEDIA_ID
    assert reservation["media_id"] == MEDIA_ID
    assert reservation.get("permalink") is None
    publications = handles["history"]["reel_publications"]
    assert handles["history"]["reel_publication_count"] == 1
    assert len(publications) == 1
    assert publications[0]["id"] == PUBLICATION_ID
    assert publications[0]["media_id"] == MEDIA_ID
    assert publications[0]["artwork_id"] == REEL_ID
    assert history_queue(handles) == []
    handles["cleanup_handle_mock"].assert_not_called()
    handles["cleanup_prefix_mock"].assert_not_called()
    handles["publisher_mock"].assert_called_once()
    assert handles["events"].count("media_publish") == 1


def test_permalink_lookup_failure_still_finalizes_published_reel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        receipt_real=True,
        finalize_real=True,
        permalink_error=RuntimeError("permalink lookup exploded"),
    )
    feed_before = {
        key: copy.deepcopy(handles["history"][key]) for key in FEED_KEYS
    }

    handles["result"] = _run_publish(handles, tmp_path)

    verified = handles["verified_holder"]["verified"]
    assert handles["events"] == [
        "verify_and_snapshot",
        "reserve_reel",
        f"stage_reel_mp4:{verified.video_path}",
        "record_reel_staging",
        "post_to_instagram_graph_api",
        f"before_publish:{CONTAINER_ID}:{()}",
        "history_put:PUBLISHING:container-1",
        "media_publish",
        "history_put:PUBLISHING:container-1",
        "get_permalink:media-1",
        "history_put:PUBLISHED:container-1",
    ]
    _assert_published_without_permalink(handles)
    assert {key: handles["history"][key] for key in FEED_KEYS} == feed_before


@pytest.mark.parametrize(
    "invalid_permalink",
    [None, "not-a-permalink", "http://insecure.example/reel/"],
)
def test_invalid_permalink_still_finalizes_published_reel_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid_permalink: str | None
):
    handles = install_publication_harness(
        monkeypatch,
        tmp_path,
        staged=True,
        receipt_real=True,
        finalize_real=True,
        permalink_result=invalid_permalink,
    )

    handles["result"] = _run_publish(handles, tmp_path)

    assert "history_put:PUBLISHED:container-1" in handles["events"]
    assert handles["events"].count("history_put:PUBLISHED:container-1") == 1
    _assert_published_without_permalink(handles)


def test_reel_parsed_4xx_mapping_keeps_feed_error_semantics_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    error = instagram_poster.InstagramAPIError(
        "Media was rejected by Instagram", status_code=400
    )
    handles = install_publication_harness(
        monkeypatch, tmp_path, staged=True, publisher_post_boundary_error=error
    )
    feed_before = {
        key: copy.deepcopy(handles["history"][key]) for key in FEED_KEYS
    }

    with pytest.raises(instagram_poster.InstagramAPIError) as excinfo:
        _run_publish(handles, tmp_path)

    assert excinfo.value is error
    assert not isinstance(
        excinfo.value, instagram_poster.InstagramPublishAmbiguousError
    )
    assert handles["history"]["reel_reservations"][0]["status"] == "AMBIGUOUS"
    assert {key: handles["history"][key] for key in FEED_KEYS} == feed_before


CLI_IDENTITY = ReelReleaseIdentity.model_validate(
    {
        "version": "artfolio-release-v1",
        "reel_id": REEL_ID,
        "created_at": "2026-09-11T11:55:00Z",
        "manifest_sha256": "a" * 64,
        "files_sha256": {
            "reel.mp4": "a" * 64,
            "caption.txt": "a" * 64,
            "metadata.json": "a" * 64,
            "qc/contact-sheet.png": "a" * 64,
        },
    }
)


def _set_cli_credentials(monkeypatch, *, account=ACCOUNT_ID, token=ACCESS_TOKEN):
    if account is None:
        monkeypatch.delenv("INSTAGRAM_ACCOUNT_ID", raising=False)
    else:
        monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", account)
    if token is None:
        monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", token)


def _install_cli_orchestrator(monkeypatch, *, record=None, error=None):
    if error is not None:
        orchestrator = Mock(side_effect=error)
    else:
        orchestrator = Mock(return_value=record)
    monkeypatch.setattr(publish_reel, "publish_verified_reel", orchestrator)
    return orchestrator


def test_cli_publishes_single_release_with_env_credentials(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_cli_credentials(monkeypatch)
    monkeypatch.delenv("ARTFOLIO_REELS_ROOT", raising=False)
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    assert publish_reel.main(["release-id"]) == 0

    orchestrator.assert_called_once()
    call_kwargs = orchestrator.call_args.kwargs
    assert call_kwargs["release"] == "release-id"
    assert call_kwargs["account_id"] == ACCOUNT_ID
    assert call_kwargs["access_token"] == ACCESS_TOKEN
    assert PUBLICATION_ID in caplog.text
    assert MEDIA_ID in caplog.text
    assert ACCESS_TOKEN not in caplog.text


def test_cli_uses_nonblank_artfolio_reels_root_env(monkeypatch, tmp_path):
    _set_cli_credentials(monkeypatch)
    monkeypatch.setenv("ARTFOLIO_REELS_ROOT", str(tmp_path / "reels-from-env"))
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    assert publish_reel.main(["release-id"]) == 0

    assert orchestrator.call_args.kwargs["reels_repository"] == (
        tmp_path / "reels-from-env"
    )


def test_cli_blank_artfolio_reels_root_falls_back_to_collector_convention(
    monkeypatch, tmp_path
):
    from scripts import collect_insights

    _set_cli_credentials(monkeypatch)
    monkeypatch.setenv("ARTFOLIO_REELS_ROOT", "   ")
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    assert publish_reel.main(["release-id"]) == 0

    assert orchestrator.call_args.kwargs["reels_repository"] == (
        collect_insights._default_reels_root()
    )


def test_cli_explicit_artfolio_reels_root_overrides_env(monkeypatch, tmp_path):
    _set_cli_credentials(monkeypatch)
    monkeypatch.setenv("ARTFOLIO_REELS_ROOT", str(tmp_path / "from-env"))
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    code = publish_reel.main(
        ["release-id", "--artfolio-reels-root", str(tmp_path / "explicit")]
    )

    assert code == 0
    assert orchestrator.call_args.kwargs["reels_repository"] == tmp_path / "explicit"


def test_cli_accepts_release_directory_positional(monkeypatch, tmp_path):
    _set_cli_credentials(monkeypatch)
    release_directory = tmp_path / "output" / "releases" / REEL_ID
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    assert publish_reel.main([str(release_directory)]) == 0

    assert orchestrator.call_args.kwargs["release"] == str(release_directory)


def test_cli_missing_account_id_returns_nonzero_without_publishing(monkeypatch, caplog):
    _set_cli_credentials(monkeypatch, account=None)
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    assert publish_reel.main(["release-id"]) == 1

    orchestrator.assert_not_called()
    assert "INSTAGRAM_ACCOUNT_ID" in caplog.text
    assert ACCESS_TOKEN not in caplog.text


def test_cli_missing_access_token_returns_nonzero_without_publishing(monkeypatch, caplog):
    _set_cli_credentials(monkeypatch, token=None)
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    assert publish_reel.main(["release-id"]) == 1

    orchestrator.assert_not_called()
    assert "INSTAGRAM_ACCESS_TOKEN" in caplog.text
    assert ACCOUNT_ID not in caplog.text


def test_cli_failure_returns_nonzero_and_sanitizes_output(monkeypatch, caplog):
    _set_cli_credentials(monkeypatch)
    orchestrator = _install_cli_orchestrator(
        monkeypatch,
        error=RuntimeError(
            "unexpected failure access_token=SUPERSECRET caption=A verified caption."
        ),
    )

    assert publish_reel.main(["release-id"]) == 1

    orchestrator.assert_called_once()
    assert "RuntimeError" in caplog.text
    assert "SUPERSECRET" not in caplog.text
    assert "A verified caption." not in caplog.text
    assert ACCESS_TOKEN not in caplog.text


def test_cli_intake_failure_reports_sanitized_message(monkeypatch, caplog):
    _set_cli_credentials(monkeypatch)
    _install_cli_orchestrator(
        monkeypatch,
        error=reel_release.ReleaseIntakeError(
            "release directory must contain exactly the required release files"
        ),
    )

    assert publish_reel.main(["release-id"]) == 1

    assert "ReleaseIntakeError" in caplog.text
    assert "release directory must contain exactly" in caplog.text
    assert ACCESS_TOKEN not in caplog.text


@pytest.mark.parametrize(
    "argv",
    [
        ["--batch", "3", "release-id"],
        ["--schedule", "daily", "release-id"],
        ["--interval", "300", "release-id"],
        ["--count", "2", "release-id"],
        ["--discover", "release-id"],
        ["--dry-run", "release-id"],
    ],
)
def test_cli_rejects_batch_schedule_and_discovery_options(monkeypatch, argv):
    _set_cli_credentials(monkeypatch)
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    with pytest.raises(SystemExit) as excinfo:
        publish_reel.main(argv)

    assert excinfo.value.code != 0
    orchestrator.assert_not_called()


def test_cli_rejects_multiple_release_arguments(monkeypatch):
    _set_cli_credentials(monkeypatch)
    orchestrator = _install_cli_orchestrator(
        monkeypatch, record=_publication_record(CLI_IDENTITY)
    )

    with pytest.raises(SystemExit) as excinfo:
        publish_reel.main(["release-a", "release-b"])

    assert excinfo.value.code != 0
    orchestrator.assert_not_called()
