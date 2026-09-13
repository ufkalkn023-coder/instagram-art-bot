"""Focused tests for the single-Reel production orchestration entry point."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import (
    history_tracker,
    instagram_poster,
    r2_media,
    reel_batch_candidates,
    reel_candidate_acquisition,
    reel_production,
    reel_publication,
    reel_reconciliation,
    reel_release,
)
from src.reel_batch_candidates import (
    BatchCandidateQueue,
    BatchCandidateStageCounts,
)
from src.reel_candidate_acquisition import AcquisitionResult


def _stage_counts():
    return BatchCandidateStageCounts(
        source_handoffs=1,
        history_excluded_at_boundary=0,
        acquired_usable=1,
        preselector_eligible=1,
        portfolio_available=1,
        queued=1,
        preselector_rejection_counts=(),
    )


def _queue(entries):
    return BatchCandidateQueue(
        target=1,
        candidate_limit=1,
        candidate_count=len(entries),
        candidates=tuple(entries),
        stage_counts=_stage_counts(),
    )


def _entry(canonical_id, handoff_path):
    return {
        "canonicalId": canonical_id,
        "artist": "Artist",
        "museum": "Museum",
        "handoffPath": str(handoff_path),
        "baseScore": 80.0,
        "portfolioPriorityScore": 80.0,
    }


def _install_pipeline(monkeypatch, *, queue_entries):
    events = []
    acquisition = AcquisitionResult(manifest={"safeCandidateCount": 2})

    def fake_acquire(**kwargs):
        events.append(("acquire", kwargs))
        return acquisition

    def fake_queue(**kwargs):
        events.append(("queue", kwargs))
        return _queue(queue_entries)

    monkeypatch.setattr(
        reel_candidate_acquisition, "acquire_reel_candidate_pool", fake_acquire
    )
    monkeypatch.setattr(
        reel_batch_candidates, "build_batch_candidate_queue", fake_queue
    )
    return events


def test_produce_reel_handoff_composes_existing_pipeline_in_order(monkeypatch, tmp_path):
    events = _install_pipeline(
        monkeypatch, queue_entries=[_entry("met_1", tmp_path / "handoff.json")]
    )

    selection = reel_production.produce_reel_handoff(
        pool_size=6,
        attempt_limit=30,
        handoff_directory=tmp_path / "handoffs",
        manifest_path=tmp_path / "acquisition.json",
        work_directory=tmp_path / "work",
        batch_output_directory=tmp_path / "batches",
        selection_target=1,
        environment={"REEL_SELECTION_TARGET": "1"},
    )

    assert [name for name, _ in events] == ["acquire", "queue"]
    acquire_kwargs = events[0][1]
    assert acquire_kwargs["pool_size"] == 6
    assert acquire_kwargs["attempt_limit"] == 30
    assert acquire_kwargs["handoff_directory"] == tmp_path / "handoffs"
    assert acquire_kwargs["manifest_path"] == tmp_path / "acquisition.json"
    assert acquire_kwargs["work_directory"] == tmp_path / "work"
    assert acquire_kwargs["environment"] == {"REEL_SELECTION_TARGET": "1"}
    queue_kwargs = events[1][1]
    assert queue_kwargs["target"] == 1
    assert queue_kwargs["source_directory"] == tmp_path / "handoffs"
    assert queue_kwargs["output_directory"] == tmp_path / "batches"
    assert queue_kwargs["environment"] == {"REEL_SELECTION_TARGET": "1"}
    assert selection.canonical_id == "met_1"
    assert selection.handoff_path == Path(str(tmp_path / "handoff.json"))
    assert selection.acquisition.manifest == {"safeCandidateCount": 2}
    assert selection.queue.candidate_count == 1


def test_produce_reel_handoff_returns_exactly_one_selection(monkeypatch, tmp_path):
    entries = [
        _entry("met_1", tmp_path / "met_1.json"),
        _entry("aic_2", tmp_path / "aic_2.json"),
        _entry("met_3", tmp_path / "met_3.json"),
    ]
    _install_pipeline(monkeypatch, queue_entries=entries)

    selection = reel_production.produce_reel_handoff(
        handoff_directory=tmp_path / "handoffs",
        batch_output_directory=tmp_path / "batches",
        selection_target=1,
        environment={},
    )

    assert selection.canonical_id == "met_1"
    assert selection.handoff_path == Path(entries[0]["handoffPath"])


def test_produce_reel_handoff_fails_closed_without_eligible_candidates(
    monkeypatch, tmp_path
):
    events = _install_pipeline(monkeypatch, queue_entries=[])

    with pytest.raises(
        reel_production.ReelProductionSelectionError, match="No eligible Reel candidate"
    ):
        reel_production.produce_reel_handoff(
            handoff_directory=tmp_path / "handoffs",
            batch_output_directory=tmp_path / "batches",
            selection_target=1,
            environment={},
        )

    assert [name for name, _ in events] == ["acquire", "queue"]


def test_produce_reel_handoff_passes_through_resolution_defaults(monkeypatch, tmp_path):
    events = _install_pipeline(
        monkeypatch, queue_entries=[_entry("met_1", tmp_path / "h.json")]
    )

    reel_production.produce_reel_handoff(environment={"REEL_SELECTION_TARGET": "4"})

    acquire_kwargs = events[0][1]
    assert acquire_kwargs["pool_size"] is None
    assert acquire_kwargs["attempt_limit"] is None
    queue_kwargs = events[1][1]
    assert queue_kwargs["target"] is None


def test_produce_reel_handoff_never_crosses_publication_boundaries(monkeypatch, tmp_path):
    _install_pipeline(
        monkeypatch, queue_entries=[_entry("met_1", tmp_path / "h.json")]
    )
    forbidden = [
        (history_tracker, "reserve_reel"),
        (history_tracker, "record_reel_staging"),
        (history_tracker, "finalize_reel_publication"),
        (history_tracker, "_upload_history"),
        (r2_media, "stage_reel_mp4"),
        (r2_media, "cleanup_temp_reel_upload"),
        (r2_media, "cleanup_publication_reels"),
        (instagram_poster, "post_to_instagram_graph_api"),
        (instagram_poster, "get_instagram_permalink"),
        (instagram_poster, "_publish_container"),
        (reel_publication, "publish_verified_reel"),
        (reel_release, "verified_reel_release_snapshot"),
        (reel_reconciliation, "reconcile_reel_publications"),
    ]
    for module, name in forbidden:
        monkeypatch.setattr(
            module,
            name,
            Mock(side_effect=AssertionError(f"{name} must not run during selection")),
        )

    def forbidden_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not run during selection")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)

    selection = reel_production.produce_reel_handoff(
        handoff_directory=tmp_path / "handoffs",
        manifest_path=None,
        work_directory=tmp_path / "work",
        batch_output_directory=tmp_path / "batches",
        selection_target=1,
        environment={},
    )

    assert selection.canonical_id == "met_1"


def _reels_checkout(tmp_path):
    reels_root = tmp_path / "artfolio-reels"
    (reels_root / "scripts").mkdir(parents=True)
    (reels_root / "package.json").write_text("{}\n", encoding="utf-8")
    for script in ("reel.ts", "package-release.ts", "verify-release.ts"):
        (reels_root / "scripts" / script).write_text("// stub\n", encoding="utf-8")
    return reels_root


def _selection(tmp_path, canonical_id="met_123"):
    handoff_path = tmp_path / "handoffs" / f"{canonical_id}.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(
            {
                "canonicalId": canonical_id,
                "imagePath": str(tmp_path / "assets" / f"{canonical_id}.jpg"),
            }
        ),
        encoding="utf-8",
    )
    return reel_production.ReelProductionSelection(
        canonical_id=canonical_id,
        handoff_path=handoff_path,
        acquisition=AcquisitionResult(manifest={"safeCandidateCount": 1}),
        queue=_queue([_entry(canonical_id, handoff_path)]),
    )


def _install_command_runner(
    monkeypatch,
    reels_root,
    *,
    reel_id,
    failing_labels=(),
    verify_payload=None,
    verify_exit_code=0,
):
    commands = []

    def fake_runner(command, **kwargs):
        commands.append((list(command), kwargs))
        label = command[2]
        if label in failing_labels:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if label == "reels:verify-release":
            payload = verify_payload or {
                "valid": True,
                "errors": [],
                "reelId": reel_id,
                "directory": str(reels_root / "output" / "releases" / reel_id),
            }
            (reels_root / "output" / "releases" / reel_id).mkdir(
                parents=True, exist_ok=True
            )
            banner = "> artfolio-reels@1.0.0 verify\n\n"
            return SimpleNamespace(
                returncode=verify_exit_code,
                stdout=banner + json.dumps(payload) + "\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reel_production.subprocess, "run", fake_runner)
    return commands


def test_produce_verified_reel_release_runs_real_commands_in_order(
    monkeypatch, tmp_path
):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path)
    commands = _install_command_runner(monkeypatch, reels_root, reel_id="met_123")

    staged = reel_production.produce_verified_reel_release(
        selection, reels_repository=reels_root
    )

    assert [command[0][2] for command in commands] == [
        "reel",
        "package",
        "reels:verify-release",
    ]
    staged_handoff = reels_root / "handoffs" / "met_123.json"
    assert [command[0] for command in commands] == [
        ["npm", "run", "reel", "--", str(staged_handoff), "--render"],
        ["npm", "run", "package", "--", "met_123"],
        ["npm", "run", "reels:verify-release", "--", "met_123", "--deep", "--json"],
    ]
    assert all(kwargs["cwd"] == str(reels_root) for _, kwargs in commands)
    assert staged_handoff.read_text(encoding="utf-8") == (
        selection.handoff_path.read_text(encoding="utf-8")
    )
    assert staged.reel_id == "met_123"
    assert staged.handoff_path == staged_handoff
    assert staged.release_directory == reels_root / "output" / "releases" / "met_123"


def test_produce_verified_reel_release_requires_existing_checkout(tmp_path):
    selection = _selection(tmp_path)
    missing_root = tmp_path / "not-a-checkout"
    missing_root.mkdir()

    with pytest.raises(RuntimeError, match="checkout"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=missing_root
        )

    partial_root = tmp_path / "partial"
    partial_root.mkdir()
    (partial_root / "package.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checkout"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=partial_root
        )


def test_produce_verified_reel_release_rejects_unsafe_reel_id(monkeypatch, tmp_path):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path, canonical_id="met/../evil")
    commands = _install_command_runner(monkeypatch, reels_root, reel_id="unused")

    with pytest.raises(RuntimeError, match="safe reel id"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )

    assert commands == []


def test_produce_verified_reel_release_stops_on_first_command_failure(
    monkeypatch, tmp_path
):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path)

    commands = _install_command_runner(
        monkeypatch, reels_root, reel_id="met_123", failing_labels={"reel"}
    )
    with pytest.raises(RuntimeError, match="reel command failed"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )
    assert len(commands) == 1

    commands = _install_command_runner(
        monkeypatch, reels_root, reel_id="met_123", failing_labels={"package"}
    )
    with pytest.raises(RuntimeError, match="package command failed"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )
    assert [command[0][2] for command in commands] == ["reel", "package"]


def test_produce_verified_reel_release_fails_closed_on_invalid_verification(
    monkeypatch, tmp_path
):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path)

    commands = _install_command_runner(
        monkeypatch,
        reels_root,
        reel_id="met_123",
        verify_payload={"valid": False, "errors": ["decode failed"]},
    )
    with pytest.raises(RuntimeError, match="did not report a valid release"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )
    assert len(commands) == 3

    commands = _install_command_runner(
        monkeypatch,
        reels_root,
        reel_id="met_123",
        verify_payload={
            "valid": True,
            "errors": [],
            "reelId": "other_reel",
            "directory": str(reels_root / "output" / "releases" / "other_reel"),
        },
    )
    with pytest.raises(RuntimeError, match="reel id does not match"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )

    commands = _install_command_runner(
        monkeypatch, reels_root, reel_id="met_123", verify_exit_code=1
    )
    with pytest.raises(RuntimeError, match="verify-release command failed"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )


def test_produce_verified_reel_release_reuses_identical_staged_handoff(
    monkeypatch, tmp_path
):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path)
    staged = reels_root / "handoffs" / "met_123.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        selection.handoff_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    staged.chmod(0o444)
    _install_command_runner(monkeypatch, reels_root, reel_id="met_123")

    staged_release = reel_production.produce_verified_reel_release(
        selection, reels_repository=reels_root
    )

    assert staged_release.handoff_path == staged
    assert staged.read_text(encoding="utf-8") == (
        selection.handoff_path.read_text(encoding="utf-8")
    )


def test_produce_verified_reel_release_fails_closed_on_conflicting_staged_handoff(
    monkeypatch, tmp_path
):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path)
    staged = reels_root / "handoffs" / "met_123.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("{}\n", encoding="utf-8")
    commands = _install_command_runner(monkeypatch, reels_root, reel_id="met_123")

    with pytest.raises(RuntimeError, match="Conflicting staged handoff"):
        reel_production.produce_verified_reel_release(
            selection, reels_repository=reels_root
        )

    assert commands == []


def test_produce_verified_reel_release_never_publishes_or_mutates(
    monkeypatch, tmp_path
):
    reels_root = _reels_checkout(tmp_path)
    selection = _selection(tmp_path)
    _install_command_runner(monkeypatch, reels_root, reel_id="met_123")
    forbidden = [
        (history_tracker, "reserve_reel"),
        (history_tracker, "record_reel_staging"),
        (history_tracker, "finalize_reel_publication"),
        (history_tracker, "_upload_history"),
        (r2_media, "stage_reel_mp4"),
        (r2_media, "cleanup_temp_reel_upload"),
        (r2_media, "cleanup_publication_reels"),
        (instagram_poster, "post_to_instagram_graph_api"),
        (instagram_poster, "get_instagram_permalink"),
        (instagram_poster, "_publish_container"),
        (reel_publication, "publish_verified_reel"),
        (reel_release, "verified_reel_release_snapshot"),
        (reel_reconciliation, "reconcile_reel_publications"),
    ]
    for module, name in forbidden:
        monkeypatch.setattr(
            module,
            name,
            Mock(side_effect=AssertionError(f"{name} must not run before verification")),
        )

    staged = reel_production.produce_verified_reel_release(
        selection, reels_repository=reels_root
    )

    assert staged.reel_id == "met_123"
    assert staged.release_directory.is_dir()


def _publication_record():
    from src.models import ReelPublicationRecord

    return ReelPublicationRecord.model_validate(
        {
            "id": "12345678-1234-4234-8234-123456789abc",
            "artwork_id": "met_123",
            "media_id": "media-1",
            "posted_at": "2026-09-13T12:00:00Z",
            "permalink": None,
            "release_identity": {
                "version": "artfolio-release-v1",
                "reel_id": "met_123",
                "created_at": "2026-09-13T11:55:00Z",
                "manifest_sha256": "a" * 64,
                "files_sha256": {
                    "reel.mp4": "a" * 64,
                    "caption.txt": "a" * 64,
                    "metadata.json": "a" * 64,
                    "qc/contact-sheet.png": "a" * 64,
                },
            },
        }
    )


def _install_full_pipeline(monkeypatch, tmp_path, *, publish_error=None):
    reels_root = _reels_checkout(tmp_path)
    events = []
    handoff_path = tmp_path / "handoffs" / "met_123.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps({"canonicalId": "met_123", "imagePath": "https://x.example/a.jpg"}),
        encoding="utf-8",
    )

    def fake_acquire(**kwargs):
        events.append("acquire")
        return AcquisitionResult(manifest={"safeCandidateCount": 2})

    def fake_queue(**kwargs):
        events.append("queue")
        return _queue([_entry("met_123", handoff_path)])

    monkeypatch.setattr(
        reel_candidate_acquisition, "acquire_reel_candidate_pool", fake_acquire
    )
    monkeypatch.setattr(
        reel_batch_candidates, "build_batch_candidate_queue", fake_queue
    )

    def fake_runner(command, **kwargs):
        events.append(f"command:{command[2]}")
        if command[2] == "reels:verify-release":
            release_directory = reels_root / "output" / "releases" / "met_123"
            release_directory.mkdir(parents=True, exist_ok=True)
            payload = {
                "valid": True,
                "errors": [],
                "reelId": "met_123",
                "directory": str(release_directory),
            }
            return SimpleNamespace(
                returncode=0,
                stdout="> banner\n\n" + json.dumps(payload) + "\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reel_production.subprocess, "run", fake_runner)

    publish_calls = []

    def fake_publish(**kwargs):
        events.append("publish")
        publish_calls.append(kwargs)
        if publish_error is not None:
            raise publish_error
        return _publication_record()

    monkeypatch.setattr(reel_publication, "publish_verified_reel", fake_publish)
    return reels_root, events, publish_calls


def test_produce_and_publish_reel_publishes_once_after_verification(
    monkeypatch, tmp_path
):
    reels_root, events, publish_calls = _install_full_pipeline(monkeypatch, tmp_path)

    outcome = reel_production.produce_and_publish_reel(
        reels_repository=reels_root,
        account_id="account",
        access_token="token",
        handoff_directory=tmp_path / "handoffs",
        manifest_path=None,
        work_directory=tmp_path / "work",
        batch_output_directory=tmp_path / "batches",
        selection_target=1,
        environment={},
    )

    assert events == [
        "acquire",
        "queue",
        "command:reel",
        "command:package",
        "command:reels:verify-release",
        "publish",
    ]
    assert len(publish_calls) == 1
    assert publish_calls[0] == {
        "release": reels_root / "output" / "releases" / "met_123",
        "reels_repository": reels_root,
        "account_id": "account",
        "access_token": "token",
    }
    assert outcome.canonical_id == "met_123"
    assert outcome.release_directory == reels_root / "output" / "releases" / "met_123"
    assert outcome.publication.media_id == "media-1"
    assert outcome.publication.artwork_id == "met_123"


def test_produce_and_publish_reel_never_publishes_when_production_fails(
    monkeypatch, tmp_path
):
    reels_root, events, publish_calls = _install_full_pipeline(monkeypatch, tmp_path)

    def failing_runner(command, **kwargs):
        events.append(f"command:{command[2]}")
        if command[2] == "reel":
            return SimpleNamespace(returncode=1, stdout="", stderr="render boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reel_production.subprocess, "run", failing_runner)

    with pytest.raises(RuntimeError, match="reel command failed"):
        reel_production.produce_and_publish_reel(
            reels_repository=reels_root,
            account_id="account",
            access_token="token",
            handoff_directory=tmp_path / "handoffs",
            manifest_path=None,
            work_directory=tmp_path / "work",
            batch_output_directory=tmp_path / "batches",
            selection_target=1,
            environment={},
        )

    assert "publish" not in events
    assert publish_calls == []

    def invalid_verification_runner(command, **kwargs):
        events.append(f"command:{command[2]}")
        if command[2] == "reels:verify-release":
            release_directory = reels_root / "output" / "releases" / "met_123"
            release_directory.mkdir(parents=True, exist_ok=True)
            payload = {
                "valid": False,
                "errors": ["decode failed"],
                "reelId": "met_123",
                "directory": str(release_directory),
            }
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(payload) + "\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reel_production.subprocess, "run", invalid_verification_runner)
    events.clear()

    with pytest.raises(RuntimeError, match="did not report a valid release"):
        reel_production.produce_and_publish_reel(
            reels_repository=reels_root,
            account_id="account",
            access_token="token",
            handoff_directory=tmp_path / "handoffs",
            manifest_path=None,
            work_directory=tmp_path / "work",
            batch_output_directory=tmp_path / "batches",
            selection_target=1,
            environment={},
        )

    assert "publish" not in events
    assert publish_calls == []


def test_produce_and_publish_reel_never_retries_publish(monkeypatch, tmp_path):
    reels_root, events, publish_calls = _install_full_pipeline(
        monkeypatch, tmp_path, publish_error=RuntimeError("publish failed")
    )

    with pytest.raises(RuntimeError, match="publish failed"):
        reel_production.produce_and_publish_reel(
            reels_repository=reels_root,
            account_id="account",
            access_token="token",
            handoff_directory=tmp_path / "handoffs",
            manifest_path=None,
            work_directory=tmp_path / "work",
            batch_output_directory=tmp_path / "batches",
            selection_target=1,
            environment={},
        )

    assert events.count("publish") == 1
    assert len(publish_calls) == 1


def test_produce_and_publish_reel_delegates_all_instagram_and_lifecycle_work(
    monkeypatch, tmp_path
):
    reels_root, events, publish_calls = _install_full_pipeline(monkeypatch, tmp_path)
    forbidden = [
        (instagram_poster, "_publish_container"),
        (instagram_poster, "_create_container"),
        (instagram_poster, "get_instagram_permalink"),
        (instagram_poster, "post_to_instagram_graph_api"),
        (history_tracker, "reserve_reel"),
        (history_tracker, "record_reel_staging"),
        (history_tracker, "start_reel_publication_attempt"),
        (history_tracker, "record_reel_publish_response"),
        (history_tracker, "finalize_reel_publication"),
        (history_tracker, "mark_reel_ambiguous"),
        (history_tracker, "expire_reel_before_media_publish"),
        (history_tracker, "_upload_history"),
    ]
    for module, name in forbidden:
        monkeypatch.setattr(
            module,
            name,
            Mock(side_effect=AssertionError(f"{name} is owned by publish_verified_reel")),
        )

    outcome = reel_production.produce_and_publish_reel(
        reels_repository=reels_root,
        account_id="account",
        access_token="token",
        handoff_directory=tmp_path / "handoffs",
        manifest_path=None,
        work_directory=tmp_path / "work",
        batch_output_directory=tmp_path / "batches",
        selection_target=1,
        environment={},
    )

    assert outcome.publication.media_id == "media-1"
    assert events[-1] == "publish"
