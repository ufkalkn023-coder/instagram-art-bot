from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from src.engagement_learning import (
    EngagementModel,
    LearningConfig,
    build_engagement_model,
    candidate_feature_keys,
    select_mature_snapshot,
)
from src.editorial_experiments import canonical_publish_slot


NOW = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)


def _publication(
    index: int,
    *,
    artist: str = "Artist",
    theme: str = "winter_light",
    slot: str = "slot_1",
    posted_at: datetime | None = None,
    include_experiment: bool = True,
) -> tuple[dict, list[dict]]:
    publication_id = f"publication-{index}"
    timestamp = posted_at or (NOW - timedelta(days=index + 5))
    publication = {
        "id": publication_id,
        "type": "carousel",
        "media_id": f"media-{index}",
        "artwork_ids": [f"cover-{index}", *[f"work-{index}-{item}" for item in range(5)]],
        "posted_at": timestamp.isoformat(),
    }
    if include_experiment:
        publication.update(
            {
                "carousel_theme": theme,
                "carousel_format": "LIGHT_STUDY",
                "featured_count": 5,
                "cover_variant": "editorial",
                "caption_hook_type": "curiosity",
                "publish_slot": slot,
            }
        )
    artworks = [
        {
            "id": f"cover-{index}",
            "publication_id": publication_id,
            "publication_role": "COVER",
            "artist": "Cover Artist",
            "museum": "Cover Museum",
            "source": "cover",
        }
    ]
    artworks.extend(
        {
            "id": f"work-{index}-{item}",
            "publication_id": publication_id,
            "publication_role": "FEATURED",
            "artist": artist,
            "museum": "Museum",
            "source": "aic",
            "region": "europe",
            "style_or_period": "baroque",
            "semantic_family": "portrait",
        }
        for item in range(5)
    )
    return publication, artworks


def _snapshot(
    index: int,
    *,
    target: int = 72,
    reach: float = 2_000,
    shares: float | None = 20,
    saved: float | None = 30,
    comments: float | None = 10,
    likes: float | None = 100,
) -> dict:
    metrics = {"reach": reach}
    for name, value in {
        "shares": shares,
        "saved": saved,
        "comments": comments,
        "likes": likes,
    }.items():
        if value is not None:
            metrics[name] = value
    return {
        "publication_id": f"publication-{index}",
        "media_id": f"media-{index}",
        "target_age_hours": target,
        "captured_at": (NOW - timedelta(days=1)).isoformat(),
        "metrics": metrics,
    }


def _dataset(specs: list[dict]) -> tuple[dict, list[dict]]:
    publications = []
    artworks = []
    snapshots = []
    for index, spec in enumerate(specs):
        publication, publication_artworks = _publication(
            index,
            artist=spec.get("artist", "Artist"),
            theme=spec.get("theme", "winter_light"),
            slot=spec.get("slot", "slot_1"),
            posted_at=spec.get("posted_at"),
            include_experiment=spec.get("include_experiment", True),
        )
        publications.append(publication)
        artworks.extend(publication_artworks)
        snapshots.append(
            _snapshot(
                index,
                target=spec.get("target", 72),
                reach=spec.get("reach", 2_000),
                shares=spec.get("shares", 20),
                saved=spec.get("saved", 30),
                comments=spec.get("comments", 10),
                likes=spec.get("likes", 100),
            )
        )
    return {"publications": publications, "posted_artworks": artworks}, snapshots


def test_no_insights_empty_history_and_legacy_publication_degrade_safely():
    assert build_engagement_model({}, []).confidence == 0
    assert build_engagement_model({"publications": [], "posted_artworks": []}, None).confidence == 0

    history, snapshots = _dataset([{"include_experiment": False}])
    model = build_engagement_model(history, snapshots, now=NOW)

    assert model.useful_publications == 1
    assert model.version == "engagement_rates_v1"
    assert not any(key.startswith("theme:") for key in model.feature_estimates)


@pytest.mark.parametrize("missing_metric", ["likes", "saved", "shares", "comments"])
def test_missing_metrics_and_partial_snapshots_reweight_without_crashing(missing_metric):
    spec = {missing_metric: None}
    history, snapshots = _dataset([spec, {"artist": "Comparison"}])
    snapshots.extend([{}, {"publication_id": 123, "metrics": "bad"}])

    model = build_engagement_model(history, snapshots, now=NOW)

    assert model.useful_publications == 2
    assert 0 <= model.global_score <= 100
    assert model.effective_observations > 0


def test_zero_reach_and_snapshot_with_no_outcome_metrics_are_ignored():
    history, snapshots = _dataset([{"reach": 0}, {"shares": None, "saved": None, "comments": None, "likes": None}])

    model = build_engagement_model(history, snapshots, now=NOW)

    assert model == EngagementModel.cold_start()


def test_snapshot_maturity_prefers_72h_then_168h_then_provisional_24h():
    snapshots = [
        _snapshot(0, target=1),
        _snapshot(0, target=24),
        _snapshot(0, target=168),
        _snapshot(0, target=72),
    ]
    assert select_mature_snapshot(snapshots)["target_age_hours"] == 72
    assert select_mature_snapshot([snapshots[1], snapshots[2]])["target_age_hours"] == 168
    assert select_mature_snapshot([snapshots[1]])["target_age_hours"] == 24
    assert select_mature_snapshot([snapshots[0]]) is None


def test_24h_signal_has_less_effective_confidence_than_72h_or_168h():
    provisional_history, provisional_snapshots = _dataset([{"target": 24} for _ in range(8)])
    mature_history, mature_snapshots = _dataset([{"target": 72} for _ in range(8)])
    final_history, final_snapshots = _dataset([{"target": 168} for _ in range(8)])

    provisional = build_engagement_model(provisional_history, provisional_snapshots, now=NOW)
    mature = build_engagement_model(mature_history, mature_snapshots, now=NOW)
    final = build_engagement_model(final_history, final_snapshots, now=NOW)

    assert provisional.effective_observations < mature.effective_observations
    assert mature.effective_observations == pytest.approx(final.effective_observations)


def test_multiple_snapshots_use_only_the_preferred_mature_slot():
    history, snapshots = _dataset([{}])
    snapshots.extend(
        [
            _snapshot(0, target=24, shares=0),
            _snapshot(0, target=168, shares=0),
        ]
    )

    model = build_engagement_model(history, snapshots, now=NOW)

    assert model.useful_publications == 1


def test_viral_outlier_is_bounded_and_does_not_permanently_dominate():
    ordinary = [
        {"artist": "Ordinary", "reach": 2_000, "shares": 20, "saved": 25, "comments": 8, "likes": 90}
        for _ in range(19)
    ]
    history, snapshots = _dataset(
        [*ordinary, {"artist": "Viral", "reach": 100, "shares": 10_000, "saved": 10_000, "comments": 5_000, "likes": 20_000}]
    )

    model = build_engagement_model(history, snapshots, now=NOW)

    viral = model.feature_estimates["artist:viral"]
    assert 0 <= viral.score <= 100
    assert viral.observations == 1
    assert viral.confidence < 0.1
    assert abs(viral.score - model.global_score) < 10


def test_feature_shrinkage_respects_low_and_high_sample_support():
    specs = [
        {"artist": "Low Sample", "shares": 80, "saved": 80},
        *[
            {"artist": "High Sample", "shares": 80, "saved": 80}
            for _ in range(10)
        ],
        *[
            {"artist": "Baseline", "shares": 2, "saved": 2}
            for _ in range(10)
        ],
    ]
    history, snapshots = _dataset(specs)

    model = build_engagement_model(history, snapshots, now=NOW)
    low = model.feature_estimates["artist:low sample"]
    high = model.feature_estimates["artist:high sample"]

    assert low.observations == 1
    assert high.observations == 10
    assert low.confidence < high.confidence
    assert abs(low.score - model.global_score) < abs(high.score - model.global_score)


def test_recent_results_receive_more_effective_weight_than_ancient_results():
    specs = [
        {
            "artist": "Recent",
            "posted_at": NOW - timedelta(days=5),
            "shares": 80,
            "saved": 80,
        },
        {
            "artist": "Ancient",
            "posted_at": NOW - timedelta(days=365),
            "shares": 80,
            "saved": 80,
        },
        {"artist": "Baseline", "shares": 2, "saved": 2},
    ]
    history, snapshots = _dataset(specs)

    model = build_engagement_model(history, snapshots, now=NOW)

    assert model.feature_estimates["artist:recent"].effective_observations > model.feature_estimates["artist:ancient"].effective_observations


def test_exploration_is_ten_percent_seeded_deterministic_and_never_global_random():
    model = EngagementModel.cold_start(LearningConfig(exploration_rate=0.10))
    first = [model.exploration_selected(f"seed-{index}") for index in range(1_000)]
    second = [model.exploration_selected(f"seed-{index}") for index in range(1_000)]

    assert first == second
    assert 70 <= sum(first) <= 130


def test_cold_start_blend_is_quality_led_and_learning_increases_gradually():
    cold = EngagementModel.cold_start()
    prediction = cold.score_candidate({"artist": "Unknown"}, {})
    components = cold.blend_candidate_score(
        quality_editorial_score=82,
        prediction=prediction,
        exploration_selected=False,
    )

    assert components.final_score == 82
    assert components.engagement_component == 0
    assert components.quality_component == 82


def test_malformed_history_unknown_features_and_multiple_artists_are_safe():
    assert build_engagement_model({"publications": "bad"}, [{}], now=NOW).confidence == 0
    assert candidate_feature_keys({"artist": "Unknown", "museum": None}) == ()

    history, snapshots = _dataset([{}, {}])
    for item in history["posted_artworks"]:
        if item.get("publication_role") == "FEATURED":
            item["artist"] = f"Artist {item['id'].rsplit('-', 1)[-1]}"
    model = build_engagement_model(history, snapshots, now=NOW)

    assert all(f"artist:artist {index}" in model.feature_estimates for index in range(5))


def test_publish_slot_is_learned_with_the_same_shrinkage_as_content_features():
    history, snapshots = _dataset(
        [
            *[{"slot": "slot_1", "shares": 80, "saved": 80} for _ in range(8)],
            *[{"slot": "slot_4", "shares": 2, "saved": 2} for _ in range(8)],
        ]
    )

    model = build_engagement_model(history, snapshots, now=NOW)

    assert model.feature_estimates["publish_slot:slot_1"].score > model.feature_estimates["publish_slot:slot_4"].score
    assert model.feature_estimates["publish_slot:slot_1"].confidence < 1


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(5, "slot_1"), (10, "slot_2"), (15, "slot_3"), (20, "slot_4")],
)
def test_four_canonical_utc_publish_slots(hour, expected):
    assert canonical_publish_slot(NOW.replace(hour=hour, minute=0)) == expected


def test_engagement_subsystem_unavailable_falls_back_to_cold_start(monkeypatch):
    monkeypatch.setattr(
        main.history_tracker,
        "load_history_with_etag",
        lambda: (_ for _ in ()).throw(OSError("R2 unavailable")),
    )

    model = main._load_engagement_model()

    assert model.confidence == 0
    assert model.useful_publications == 0
