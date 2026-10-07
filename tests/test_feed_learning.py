from datetime import datetime, timedelta, timezone

import pytest

from src.engagement_features import EngagementFeatureVector
from src.engagement_learning import analyze_engagement_learning

NOW = datetime(2026, 10, 7, 19, tzinfo=timezone.utc)


def dataset():
    publications, artworks, snapshots = [], [], []
    for index, format_name in enumerate(("carousel", "carousel", "single")):
        publication_id, media_id = f"pub-{index}", f"media-{index}"
        ids = [f"aic_{index}-{n}" for n in range(2 if format_name == "carousel" else 1)]
        publications.append({"id": publication_id, "media_id": media_id,
                             "type": format_name, "artwork_ids": ids,
                             "posted_at": (NOW - timedelta(days=8-index)).isoformat()})
        artworks.extend({"id": art_id, "publication_id": publication_id,
                         "artist": f"Artist {index}", "museum": "Museum"} for art_id in ids)
        snapshots.append({"publication_id": publication_id, "media_id": media_id,
                          "target_age_hours": 72, "captured_at": NOW.isoformat(),
                          "metrics": {"reach": 1000, "saved": 10 + index * 10}})
    return {"publications": publications, "posted_artworks": artworks}, snapshots


def test_single_observation_has_its_own_model_and_authoritative_format_feature():
    history, snapshots = dataset()
    audit = analyze_engagement_learning(history, snapshots, now=NOW)
    assert audit.eligible_learning_observations == 3
    assert audit.model.useful_feed_observations == 3
    assert audit.model.useful_carousel_observations == 2
    single = audit.model.for_format("single")
    assert single.useful_publications == 1
    assert 0 < single.confidence < 0.1
    assert "publication_format:single" in single.feature_estimates
    assert "featured_count:1" in single.feature_estimates
    assert "publication_format:carousel" not in single.feature_estimates
    assert "artist:artist 2" in single.feature_estimates


def test_single_outcome_does_not_change_carousel_model():
    history, snapshots = dataset()
    first = analyze_engagement_learning(history, snapshots, now=NOW).model.for_format("carousel")
    snapshots[-1]["metrics"] = {"reach": 100000, "saved": 90000}
    second = analyze_engagement_learning(history, snapshots, now=NOW).model.for_format("carousel")
    assert first == second


@pytest.mark.parametrize("metrics,expected", [({"reach": 1000}, 0), ({"reach": 0, "saved": 2}, 0),
                                             ({"reach": 1000, "saved": 0}, 1)])
def test_single_uses_existing_missing_and_zero_metric_rules(metrics, expected):
    history, snapshots = dataset()
    snapshots[-1]["metrics"] = metrics
    model = analyze_engagement_learning(history, snapshots, now=NOW).model
    assert model.for_format("single").useful_publications == expected
    if not expected:
        assert model.for_format("single").confidence == 0


def test_single_identity_mismatch_cannot_train():
    history, snapshots = dataset()
    snapshots[-1]["media_id"] = "another-media"
    audit = analyze_engagement_learning(history, snapshots, now=NOW)
    assert audit.model.for_format("single").confidence == 0
    assert audit.excluded_by_reason["snapshot_identity_mismatch"] == 1


def test_publication_format_is_separate_from_editorial_format_and_round_trips():
    vector = EngagementFeatureVector.from_context({"publication_format": "single", "featured_count": 1})
    assert "publication_format:single" in vector.context_feature_keys()
    restored = EngagementFeatureVector.from_context({"engagement_features": vector.model_dump(exclude_none=True)})
    assert restored == vector
    carousel = EngagementFeatureVector.from_context({"publication_format": "carousel", "carousel_format": "LIGHT_STUDY"})
    assert set(carousel.context_feature_keys()) >= {"publication_format:carousel", "format:light_study"}


def test_single_candidate_ranking_uses_only_its_supplied_format_model(monkeypatch, tmp_path):
    from PIL import Image
    from src import art_fetcher
    from src.engagement_learning import EngagementModel, FeatureEstimate, LearningConfig
    from src.quality_filter import ImageValidationResult
    from tests.test_single_post_diversity import _candidate

    candidates = [_candidate("low", artist="Low"), _candidate("high", artist="High")]
    class Adapter:
        source_id = "aic"
        def fetch_candidates(self, **kwargs):
            return candidates
    def download(url, path):
        Image.new("RGB", (1600, 1000), "navy").save(path)
        return ImageValidationResult(True, 1600, 1000, "JPEG", "ok")
    monkeypatch.setattr(art_fetcher, "get_museum_adapters", lambda: [Adapter()])
    monkeypatch.setattr(art_fetcher.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(art_fetcher.config, "OUTPUT_RAW_IMAGE_PATH", str(tmp_path / "raw.jpg"))
    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)
    model = EngagementModel(confidence=0.8, config=LearningConfig(exploration_rate=0),
                            feature_estimates={"artist:low": FeatureEstimate(5, 5, .8, 10, 8),
                                               "artist:high": FeatureEstimate(95, 95, .8, 10, 8)})
    selected = list(art_fetcher.iter_single_post_candidates(
        set(), max_candidates=2, engagement_model=model,
        engagement_context={"publication_format": "single", "featured_count": 1}))
    assert selected[0]["id"] == "aic_high"
    assert selected[0]["engagement_applied"] is True
    assert selected[0]["engagement_confidence"] > 0
