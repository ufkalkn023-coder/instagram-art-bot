from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.backtest_engagement_learning as backtest_script
from scripts.backtest_engagement_learning import run
from src.engagement_backtest import run_engagement_backtest


START = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _dataset(
    count: int = 14,
    *,
    spacing_days: int = 4,
) -> tuple[dict, list[dict], datetime]:
    publications = []
    artworks = []
    snapshots = []
    for index in range(count):
        posted_at = START + timedelta(days=index * spacing_days)
        publication_id = f"publication-{index}"
        theme = "strong" if index % 2 else "quiet"
        publications.append(
            {
                "id": publication_id,
                "type": "carousel",
                "media_id": f"media-{index}",
                "posted_at": posted_at.isoformat(),
                "carousel_theme": theme,
                "carousel_format": "LIGHT_STUDY",
                "featured_count": 5,
                "publish_slot": "slot_1",
            }
        )
        artworks.extend(
            {
                "id": f"work-{index}-{position}",
                "publication_id": publication_id,
                "publication_role": "FEATURED",
                "artist": theme,
                "museum": f"Museum {position % 2}",
                "source": "aic",
                "region": "europe",
                "style_or_period": "modern" if index % 2 else "baroque",
                "dominant_color": "blue" if index % 2 else "red",
            }
            for position in range(5)
        )
        snapshots.append(
            {
                "publication_id": publication_id,
                "media_id": f"media-{index}",
                "target_age_hours": 72,
                "captured_at": (posted_at + timedelta(hours=72)).isoformat(),
                "metrics": {
                    "reach": 8 + index % 6,
                    "shares": 4 if index % 2 else 0,
                    "saved": 5 if index % 2 else 1,
                    "comments": 2 if index % 2 else 0,
                    "likes": 8 if index % 2 else 2,
                },
            }
        )
    now = START + timedelta(days=count * spacing_days + 7)
    return {"publications": publications, "posted_artworks": artworks}, snapshots, now


def test_backtest_is_deterministic_and_smaller_prior_only_changes_reliability():
    history, snapshots, now = _dataset()

    first = run_engagement_backtest(
        history,
        snapshots,
        reach_priors=(750, 100),
        minimum_training_observations=4,
        bootstrap_samples=30,
        seed=42,
        now=now,
    )
    second = run_engagement_backtest(
        history,
        snapshots,
        reach_priors=(750, 100),
        minimum_training_observations=4,
        bootstrap_samples=30,
        seed=42,
        now=now,
    )

    assert first == second
    baseline, smaller = first.datasets[0].evaluations
    assert smaller.effective_observations > baseline.effective_observations
    assert smaller.confidence > baseline.confidence
    assert [fold.actual_score for fold in smaller.folds] == pytest.approx(
        [fold.actual_score for fold in baseline.folds]
    )


def test_temporal_folds_exclude_snapshots_not_captured_by_prediction_time():
    history, snapshots, now = _dataset(count=8, spacing_days=1)

    report = run_engagement_backtest(
        history,
        snapshots,
        reach_priors=(750,),
        minimum_training_observations=2,
        bootstrap_samples=0,
        now=now,
    )

    folds = report.datasets[0].evaluations[0].folds
    assert folds[0].test_posted_at == (START + timedelta(days=4)).isoformat()
    assert folds[0].training_observations == 2
    assert all(
        earlier.training_observations <= later.training_observations
        for earlier, later in zip(folds, folds[1:])
    )


def test_future_outcome_cannot_change_earlier_fold_predictions_or_labels():
    history, snapshots, now = _dataset(count=10)
    original = run_engagement_backtest(
        history,
        snapshots,
        reach_priors=(750,),
        minimum_training_observations=3,
        bootstrap_samples=0,
        now=now,
    )
    changed_snapshots = [dict(snapshot) for snapshot in snapshots]
    changed_snapshots[-1] = {
        **changed_snapshots[-1],
        "metrics": {
            "reach": 10,
            "shares": 10_000,
            "saved": 10_000,
            "comments": 10_000,
            "likes": 10_000,
        },
    }

    changed = run_engagement_backtest(
        history,
        changed_snapshots,
        reach_priors=(750,),
        minimum_training_observations=3,
        bootstrap_samples=0,
        now=now,
    )

    original_folds = original.datasets[0].evaluations[0].folds
    changed_folds = changed.datasets[0].evaluations[0].folds
    assert original_folds[:-1] == changed_folds[:-1]
    assert original_folds[-1].predicted_score == changed_folds[-1].predicted_score
    assert original_folds[-1].actual_score != changed_folds[-1].actual_score


def test_mature_only_evaluation_excludes_24_hour_labels():
    history, snapshots, now = _dataset(count=10)
    snapshots[0]["target_age_hours"] = 24
    snapshots[0]["captured_at"] = (START + timedelta(hours=24)).isoformat()

    report = run_engagement_backtest(
        history,
        snapshots,
        reach_priors=(750,),
        minimum_training_observations=3,
        bootstrap_samples=0,
        now=now,
    )

    assert report.usable_publications == 10
    assert report.mature_publications == 9
    assert report.provisional_publications == 1
    assert report.datasets[1].usable_observations == 9


def test_local_cli_is_read_only_and_does_not_load_keychain(
    tmp_path, monkeypatch, capsys
):
    history, snapshots, now = _dataset(count=8)
    history_path = tmp_path / "history.json"
    snapshots_path = tmp_path / "snapshots.json"
    history_path.write_text(json.dumps(history), encoding="utf-8")
    snapshots_path.write_text(json.dumps(snapshots), encoding="utf-8")
    history_before = history_path.read_bytes()
    snapshots_before = snapshots_path.read_bytes()
    monkeypatch.setattr(
        backtest_script,
        "load_keychain_credentials",
        lambda profile: (_ for _ in ()).throw(
            AssertionError("unexpected Keychain read")
        ),
    )

    result = run(
        [
            "--history",
            str(history_path),
            "--snapshots",
            str(snapshots_path),
            "--reach-priors",
            "750,100,none",
            "--minimum-train",
            "3",
            "--bootstrap-samples",
            "10",
            "--now",
            now.isoformat(),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "# Temporal OOS Results" in output
    assert "Production parameter changed: NO" in output
    assert history_path.read_bytes() == history_before
    assert snapshots_path.read_bytes() == snapshots_before


def test_production_path_exposes_only_read_operations():
    source = Path(backtest_script.__file__).read_text(encoding="utf-8")

    assert "load_history_with_etag()" in source
    assert "load_all_snapshots()" in source
    for mutation in (
        "put_object",
        "delete_object",
        "copy_object",
        "upload_file",
        "upload_fileobj",
        "_upload_history",
        "save_snapshots",
        "save_associations",
    ):
        assert mutation not in source
