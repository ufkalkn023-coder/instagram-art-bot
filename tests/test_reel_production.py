"""Focused tests for the single-Reel production orchestration entry point."""

import subprocess
from pathlib import Path
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
