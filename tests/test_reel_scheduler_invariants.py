"""Scheduler safety invariants pinned across orchestration, workflow, and reconciliation."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src import (
    history_tracker,
    instagram_poster,
    r2_media,
    reel_batch_candidates,
    reel_candidate_acquisition,
    reel_production,
    reel_publication,
    reel_reconciliation,
)
from src.reel_batch_candidates import (
    BatchCandidateQueue,
    BatchCandidateStageCounts,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _install_full_run(
    monkeypatch, tmp_path, *, canonical_ids=("met_123",), verify=None, publish_error=None
):
    """Install the full one-run pipeline double.

    Returns (reels_root, events, commands, publish_calls, runner). ``verify`` may be
    None (valid payload), raw stdout text, or a callable ``(reel_id, release_directory)
    -> stdout``.
    """
    reels_root = tmp_path / "artfolio-reels"
    (reels_root / "scripts").mkdir(parents=True)
    (reels_root / "package.json").write_text("{}", encoding="utf-8")
    for script in ("reel.ts", "package-release.ts", "verify-release.ts"):
        (reels_root / "scripts" / script).write_text("//\n", encoding="utf-8")

    events = []
    entries = []
    for canonical_id in canonical_ids:
        handoff = tmp_path / "handoffs" / f"{canonical_id}.json"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(
            json.dumps({"canonicalId": canonical_id}), encoding="utf-8"
        )
        entries.append(
            {
                "canonicalId": canonical_id,
                "artist": "Artist",
                "museum": "Museum",
                "handoffPath": str(handoff),
                "baseScore": 80.0,
                "portfolioPriorityScore": 80.0,
            }
        )

    def fake_acquire(**kwargs):
        events.append("acquire")
        return reel_candidate_acquisition.AcquisitionResult(manifest={})

    def fake_queue(**kwargs):
        events.append("queue")
        return _queue(entries)

    monkeypatch.setattr(
        reel_candidate_acquisition, "acquire_reel_candidate_pool", fake_acquire
    )
    monkeypatch.setattr(
        reel_batch_candidates, "build_batch_candidate_queue", fake_queue
    )

    commands = []
    publish_calls = []

    def fake_runner(command, **kwargs):
        events.append(f"command:{command[2]}")
        commands.append(list(command))
        if command[2] != "reels:verify-release":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        reel_id = command[4]
        release_directory = reels_root / "output" / "releases" / reel_id
        if verify is None or callable(verify):
            release_directory.mkdir(parents=True, exist_ok=True)
        if verify is None:
            payload = {
                "valid": True,
                "errors": [],
                "reelId": reel_id,
                "directory": str(release_directory),
            }
            stdout = "> banner\n\n" + json.dumps(payload) + "\n"
        elif callable(verify):
            stdout = verify(reel_id, release_directory)
        else:
            stdout = verify
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def fake_publish(**kwargs):
        events.append("publish")
        publish_calls.append(kwargs)
        if publish_error is not None:
            raise publish_error
        return _publication_record()

    monkeypatch.setattr(reel_publication, "publish_verified_reel", fake_publish)
    return reels_root, events, commands, publish_calls, fake_runner


def _run_kwargs(tmp_path, reels_root, runner, **overrides):
    kwargs = {
        "reels_repository": reels_root,
        "account_id": "account",
        "access_token": "token",
        "handoff_directory": tmp_path / "handoffs",
        "manifest_path": None,
        "work_directory": tmp_path / "work",
        "batch_output_directory": tmp_path / "batches",
        "selection_target": 1,
        "environment": {},
        "command_runner": runner,
    }
    kwargs.update(overrides)
    return kwargs


def test_orchestration_module_never_references_publish_infrastructure():
    source = (REPO_ROOT / "src" / "reel_production.py").read_text(encoding="utf-8")
    assert "instagram_poster" not in source
    assert "history_tracker" not in source
    assert "r2_media" not in source
    assert source.count("publish_verified_reel(") == 1


def test_reconcile_wrapper_never_references_publish_capabilities():
    source = (REPO_ROOT / "scripts" / "reconcile_reels.py").read_text(encoding="utf-8")
    assert "instagram_poster" not in source
    assert "publish_verified_reel" not in source
    assert "src.reel_publication" not in source


def test_reconciliation_module_only_reads_instagram():
    source = (REPO_ROOT / "src" / "reel_reconciliation.py").read_text(encoding="utf-8")
    for read_name in (
        "get_container_status",
        "get_instagram_permalink",
        "get_instagram_media_id",
    ):
        assert read_name in source
    for forbidden in (
        "_create_container",
        "_publish_container",
        "post_to_instagram_graph_api",
        "post_story_to_instagram_graph_api",
        "post_carousel_to_instagram_graph_api",
    ):
        assert forbidden not in source


def test_workflow_concurrency_block_matches_carousel_exactly():
    reels = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "instagram_reels.yml").read_text(
            encoding="utf-8"
        )
    )
    carousel = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "instagram_bot.yml").read_text(
            encoding="utf-8"
        )
    )
    assert reels["concurrency"] == carousel["concurrency"]
    assert reels["concurrency"]["group"] == "instagram-bot"
    assert reels["concurrency"]["cancel-in-progress"] is False


def test_workflow_has_single_cron_gated_by_repository_variable():
    reels, _ = _workflow_documents()
    schedule = reels["on"]["schedule"]
    assert len(schedule) == 1
    assert schedule[0]["cron"] == "0 7,12,17,22 * * *"
    job = reels["jobs"]["produce-one-reel"]
    assert "vars.ARTFOLIO_REEL_SCHEDULE_ENABLED == 'true'" in job["if"]
    assert "PUBLISH_REEL_TO_INSTAGRAM" in job["if"]


def test_workflow_single_job_single_production_step_last_with_allowed_actions():
    reels, _ = _workflow_documents()
    assert list(reels["jobs"]) == ["produce-one-reel"]
    job = reels["jobs"]["produce-one-reel"]
    assert "strategy" not in job
    allowed_actions = {
        "actions/checkout@v5",
        "actions/setup-python@v6",
        "actions/setup-node@v4",
    }
    run_steps = []
    for step in job["steps"]:
        if "uses" in step:
            assert step["uses"] in allowed_actions
        else:
            assert "continue-on-error" not in step
            if "run" in step:
                run_steps.append(step["run"])
    production_runs = [run for run in run_steps if "scripts/produce_reel.py" in run]
    reconcile_runs = [run for run in run_steps if "scripts/reconcile_reels.py" in run]
    assert len(production_runs) == 1
    assert len(reconcile_runs) == 1
    assert run_steps.index(reconcile_runs[0]) < run_steps.index(production_runs[0])
    assert run_steps[-1] == production_runs[0]
    assert (
        "reel.mp4"
        not in (
            REPO_ROOT / ".github" / "workflows" / "instagram_reels.yml"
        ).read_text(encoding="utf-8")
    )


def _workflow_documents():
    reels = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "instagram_reels.yml").read_text(
            encoding="utf-8"
        )
    )
    carousel = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "instagram_bot.yml").read_text(
            encoding="utf-8"
        )
    )
    return reels, carousel


def test_full_run_uses_injected_runner_and_never_global_subprocess(
    monkeypatch, tmp_path
):
    reels_root, events, commands, publish_calls, runner = _install_full_run(
        monkeypatch, tmp_path
    )
    global_subprocess = Mock()
    monkeypatch.setattr(subprocess, "run", global_subprocess)

    outcome = reel_production.produce_and_publish_reel(
        **_run_kwargs(tmp_path, reels_root, runner)
    )

    global_subprocess.assert_not_called()
    assert events == [
        "acquire",
        "queue",
        "command:reel",
        "command:package",
        "command:reels:verify-release",
        "publish",
    ]
    assert [command[2] for command in commands] == [
        "reel",
        "package",
        "reels:verify-release",
    ]
    assert len(publish_calls) == 1
    assert outcome.canonical_id == "met_123"
    assert outcome.publication.media_id == "media-1"


def test_multi_candidate_queue_processes_exactly_the_first_candidate(
    monkeypatch, tmp_path
):
    reels_root, events, commands, publish_calls, runner = _install_full_run(
        monkeypatch, tmp_path, canonical_ids=("met_123", "aic_2", "met_3")
    )

    outcome = reel_production.produce_and_publish_reel(
        **_run_kwargs(tmp_path, reels_root, runner)
    )

    assert outcome.canonical_id == "met_123"
    assert [command[2] for command in commands] == [
        "reel",
        "package",
        "reels:verify-release",
    ]
    assert all("met_123" in " ".join(command) for command in commands)
    assert not any("aic_2" in " ".join(command) for command in commands)
    assert not any("met_3" in " ".join(command) for command in commands)
    assert publish_calls[0]["release"] == (
        reels_root / "output" / "releases" / "met_123"
    )
    assert not (reels_root / "handoffs" / "aic_2.json").exists()
    assert not (reels_root / "handoffs" / "met_3.json").exists()


def test_publish_failure_leaves_release_pipeline_untouched(monkeypatch, tmp_path):
    reels_root, events, commands, publish_calls, runner = _install_full_run(
        monkeypatch, tmp_path, publish_error=RuntimeError("publish failed")
    )

    with pytest.raises(RuntimeError, match="publish failed"):
        reel_production.produce_and_publish_reel(
            **_run_kwargs(tmp_path, reels_root, runner)
        )

    assert [command[2] for command in commands] == [
        "reel",
        "package",
        "reels:verify-release",
    ]
    assert len(publish_calls) == 1
    assert events.count("publish") == 1


def test_empty_selection_runs_no_commands_and_never_publishes(monkeypatch, tmp_path):
    reels_root, events, commands, publish_calls, runner = _install_full_run(
        monkeypatch, tmp_path, canonical_ids=()
    )

    with pytest.raises(
        reel_production.ReelProductionSelectionError, match="No eligible Reel candidate"
    ):
        reel_production.produce_and_publish_reel(
            **_run_kwargs(tmp_path, reels_root, runner)
        )

    assert commands == []
    assert publish_calls == []
    assert events == ["acquire", "queue"]


def test_excluded_canonical_ids_forward_to_acquisition_only(monkeypatch, tmp_path):
    reels_root, events, commands, publish_calls, runner = _install_full_run(
        monkeypatch, tmp_path
    )
    excluded_recorder = []
    installed_acquire = reel_candidate_acquisition.acquire_reel_candidate_pool

    def recording_acquire(**kwargs):
        excluded_recorder.append(kwargs.get("excluded_canonical_ids"))
        return installed_acquire(**kwargs)

    monkeypatch.setattr(
        reel_candidate_acquisition, "acquire_reel_candidate_pool", recording_acquire
    )

    reel_production.produce_and_publish_reel(
        **_run_kwargs(
            tmp_path,
            reels_root,
            runner,
            excluded_canonical_ids=("met_9", "aic_10"),
        )
    )

    assert excluded_recorder == [("met_9", "aic_10")]


def test_verifier_failures_never_publish(monkeypatch, tmp_path):
    nonexistent = tmp_path / "does-not-exist"
    cases = [
        ("not json at all\n", "not valid JSON"),
        ("[1, 2]\n", "not valid JSON"),
        ("42\n", "not valid JSON"),
        (
            '{ "valid": true, "errors": [], "reelId": "met_123" }\ntrailing',
            "not valid JSON",
        ),
        (
            json.dumps({"valid": False, "errors": [], "reelId": "met_123"}),
            "did not report a valid release",
        ),
        (
            json.dumps(
                {"valid": True, "errors": ["decode failed"], "reelId": "met_123"}
            ),
            "reported errors",
        ),
        (
            json.dumps({"valid": True, "errors": [], "reelId": "other_reel"}),
            "reel id does not match",
        ),
        (
            json.dumps({"valid": True, "errors": [], "reelId": "met_123"}),
            "no release directory",
        ),
        (
            json.dumps(
                {"valid": True, "errors": [], "reelId": "met_123", "directory": ""}
            ),
            "no release directory",
        ),
        (
            json.dumps(
                {
                    "valid": True,
                    "errors": [],
                    "reelId": "met_123",
                    "directory": str(nonexistent),
                }
            ),
            "directory does not exist",
        ),
    ]
    for index, (stdout, expected_message) in enumerate(cases):
        case_root = tmp_path / f"case-{index}"
        reels_root, events, commands, publish_calls, runner = _install_full_run(
            monkeypatch, case_root, verify=stdout
        )

        with pytest.raises(
            reel_production.ReelReleaseVerificationError, match=expected_message
        ):
            reel_production.produce_and_publish_reel(
                **_run_kwargs(case_root, reels_root, runner)
            )

        assert "publish" not in events
        assert publish_calls == []


def test_publisher_owns_every_lifecycle_and_instagram_boundary(monkeypatch, tmp_path):
    reels_root, events, commands, publish_calls, runner = _install_full_run(
        monkeypatch, tmp_path
    )
    forbidden = [
        (history_tracker, "reserve_reel"),
        (history_tracker, "record_reel_staging"),
        (history_tracker, "start_reel_publication_attempt"),
        (history_tracker, "record_reel_publish_response"),
        (history_tracker, "finalize_reel_publication"),
        (history_tracker, "mark_reel_ambiguous"),
        (history_tracker, "expire_reel_before_media_publish"),
        (history_tracker, "_upload_history"),
        (instagram_poster, "_create_container"),
        (instagram_poster, "_publish_container"),
        (instagram_poster, "post_to_instagram_graph_api"),
        (instagram_poster, "get_instagram_permalink"),
        (r2_media, "stage_reel_mp4"),
        (r2_media, "cleanup_temp_reel_upload"),
        (r2_media, "cleanup_publication_reels"),
        (reel_reconciliation, "reconcile_reel_publications"),
    ]
    for module, name in forbidden:
        monkeypatch.setattr(
            module,
            name,
            Mock(
                side_effect=AssertionError(f"{name} is not owned by the scheduler")
            ),
        )

    outcome = reel_production.produce_and_publish_reel(
        **_run_kwargs(tmp_path, reels_root, runner)
    )

    assert outcome.publication.media_id == "media-1"
