from datetime import datetime, timezone

import pytest

from src import history_tracker
from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    ContrastBucket,
    DominantColorFamily,
    LuminanceBucket,
)
from src.carousel_themes import CarouselFormat, ThemeFamily
from src.engagement_features import EngagementFeatureVector
from src.engagement_learning import candidate_feature_keys
from src.models import PublicationRecord


def _artwork(identifier):
    return {
        "id": identifier,
        "title": f"Title {identifier}",
        "artist": f"Artist {identifier}",
        "museum": "Museum",
        "medium": "Oil on canvas",
        "period": "modern",
        "region": "europe",
    }


def _publication():
    return _artwork("met_cover"), [_artwork(f"aic_{index}") for index in range(1, 9)]


def _experiment_metadata(featured_count=8):
    return {
        "selection_model_version": "carousel_learning_v1",
        "engagement_model_version": "engagement_rates_v2",
        "carousel_theme": "winter_light",
        "carousel_format": "LIGHT_STUDY",
        "featured_count": featured_count,
        "cover_variant": "editorial",
        "caption_hook_type": "curiosity",
        "publish_slot": "slot_2",
        "exploration_selected": False,
        "learned_score": 63.5,
        "engagement_confidence": 0.42,
        "quality_component": 38.0,
        "engagement_component": 24.0,
        "diversity_component": -1.0,
        "exploration_component": 0.0,
        "preceding_post_distance_minutes": 300.0,
        "previous_post_spacing_bucket": "3h_to_6h",
        "engagement_features": {
            "theme": "winter_light",
            "format": "LIGHT_STUDY",
            "featured_count": featured_count,
            "cover_variant": "editorial",
            "caption_hook": "curiosity",
            "publish_slot": "slot_2",
            "weekday": "monday",
            "previous_post_spacing_bucket": "3h_to_6h",
        },
    }


def _history_backend(monkeypatch, initial=None):
    history = initial or {"posted_artworks": []}
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda value, etag: uploads.append((value, etag)))
    return history, uploads


def test_carousel_reservation_atomically_records_cover_and_featured_roles(monkeypatch):
    history, uploads = _history_backend(monkeypatch)
    cover, featured = _publication()

    publication_id = history_tracker.reserve_carousel(cover, featured)

    records = history["posted_artworks"]
    assert len(uploads) == 1
    assert len(records) == 9
    assert [record["id"] for record in records] == [cover["id"], *[art["id"] for art in featured]]
    assert records[0]["publication_role"] == "COVER"
    assert [record["publication_role"] for record in records[1:]] == ["FEATURED"] * 8
    assert [record["featured_position"] for record in records[1:]] == list(range(1, 9))
    assert {record["publication_id"] for record in records} == {publication_id}
    assert {record["cover_artwork_id"] for record in records} == {cover["id"]}
    assert all(record["featured_artwork_ids"] == [art["id"] for art in featured] for record in records)
    assert history_tracker.get_posted_ids() == {cover["id"], *[art["id"] for art in featured]}


def test_candidate_features_round_trip_through_history_for_training(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()
    visual = ArtworkVisualFeatures(
        width=1200,
        height=800,
        aspect_ratio=1.5,
        orientation=ArtworkOrientation.LANDSCAPE,
        mean_luminance=122.0,
        luminance_bucket=LuminanceBucket.MID,
        mean_saturation=0.5,
        dominant_color_family=DominantColorFamily.BLUE,
        contrast_bucket=ContrastBucket.MEDIUM,
    )
    candidate = {
        **featured[0],
        "artist": "Claude Monet",
        "artist_group": "Impressionist circle",
        "source": "aic",
        "style_or_period": "Impressionism",
        "visual_category": "landscape",
        "visual_features": visual,
    }
    featured[0] = candidate

    history_tracker.reserve_carousel(cover, featured)

    record = history["posted_artworks"][1]
    serving = EngagementFeatureVector.from_candidate(candidate)
    training = EngagementFeatureVector.from_candidate(record)
    assert training == serving
    assert candidate_feature_keys(record) == candidate_feature_keys(candidate)
    assert record["period_or_style"] == "Impressionism"
    assert record["semantic_family"] == "landscape"
    assert record["dominant_color"] == "BLUE"
    assert record["luminance_bucket"] == "MID"
    assert record["orientation"] == "LANDSCAPE"


def test_legacy_publication_and_artwork_without_canonical_features_remain_valid():
    publication = PublicationRecord.model_validate(
        {
            "id": "legacy-publication",
            "type": "carousel",
            "media_id": "legacy-media",
            "artwork_ids": ["cover", "featured"],
            "posted_at": "2026-08-01T12:00:00Z",
        }
    )
    legacy = EngagementFeatureVector.from_candidate(
        {"id": "aic_legacy", "artist": "Unknown Artist", "period": "unknown"}
    )

    assert publication.engagement_features is None
    assert legacy.artist is None
    assert legacy.period_or_style is None
    assert legacy.candidate_feature_keys() == ("source:aic",)


@pytest.mark.parametrize("featured_count", [5, 6, 8])
def test_variable_length_carousel_history_transitions_atomically(
    monkeypatch, featured_count
):
    history, uploads = _history_backend(monkeypatch)
    cover = _artwork("met_cover")
    featured = [
        _artwork(f"aic_{index}") for index in range(1, featured_count + 1)
    ]

    publication_id = history_tracker.reserve_carousel(cover, featured)
    ids = [cover["id"], *[artwork["id"] for artwork in featured]]
    children = tuple(f"child-{index}" for index in range(featured_count + 1))

    assert history_tracker.start_publication_attempt(ids, "parent", children) == featured_count + 1
    assert history_tracker.confirm_carousel_publication(
        cover["id"], [artwork["id"] for artwork in featured], "media"
    ) == featured_count + 1
    records = history["posted_artworks"]
    assert {record["publication_id"] for record in records} == {publication_id}
    assert {record["status"] for record in records} == {"PUBLISHED"}
    assert {record["container_id"] for record in records} == {"parent"}
    assert [record["featured_position"] for record in records[1:]] == list(
        range(1, featured_count + 1)
    )
    assert len(uploads) == 3


def test_carousel_reservation_persists_theme_metadata_on_all_publication_records(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()

    history_tracker.reserve_carousel(
        cover,
        featured,
        theme_id="winter_light",
        theme_family="season",
        carousel_format="LIGHT_STUDY",
    )

    assert {record["theme_id"] for record in history["posted_artworks"]} == {"winter_light"}
    assert {record["theme_family"] for record in history["posted_artworks"]} == {"season"}
    assert {record["carousel_format"] for record in history["posted_artworks"]} == {"LIGHT_STUDY"}


def test_experiment_metadata_is_compact_reserved_and_finalized_backward_compatibly(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()
    metadata = _experiment_metadata()

    publication_id = history_tracker.reserve_carousel(
        cover,
        featured,
        theme_id="winter_light",
        theme_family="season",
        carousel_format="LIGHT_STUDY",
        publication_metadata=metadata,
    )

    records = history["posted_artworks"]
    assert records[0]["publication_metadata"] == metadata
    assert all("publication_metadata" not in record for record in records[1:])
    ids = [record["id"] for record in records]
    history_tracker.mark_artworks_publishing(ids)
    history_tracker.confirm_carousel_publication(
        cover["id"], [artwork["id"] for artwork in featured], "media-1"
    )

    published = history["publications"][0]
    assert published["id"] == publication_id
    for field, value in metadata.items():
        assert published[field] == value


def test_legacy_carousel_without_experiment_metadata_still_finalizes(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()
    history_tracker.reserve_carousel(cover, featured)
    ids = [cover["id"], *[artwork["id"] for artwork in featured]]
    history_tracker.mark_artworks_publishing(ids)

    history_tracker.confirm_carousel_publication(
        cover["id"], [artwork["id"] for artwork in featured], "legacy-media"
    )

    publication = history["publications"][0]
    assert publication["type"] == "carousel"
    assert "selection_model_version" not in publication


def test_theme_history_collapses_nine_artwork_rows_into_one_publication_slot(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()
    history_tracker.reserve_carousel(
        cover,
        featured,
        theme_id="winter_light",
        theme_family="season",
        carousel_format="LIGHT_STUDY",
    )
    for record in history["posted_artworks"]:
        record["status"] = "PUBLISHED"

    slots = history_tracker.get_recent_carousel_theme_history()

    assert len(slots) == 1
    assert slots[0].theme_id == "winter_light"
    assert slots[0].theme_family is ThemeFamily.SEASON
    assert slots[0].carousel_format is CarouselFormat.LIGHT_STUDY


def test_single_publications_do_not_contaminate_carousel_theme_fatigue(monkeypatch):
    history = {
        "posted_artworks": [
            {
                "id": "aic_single",
                "status": "PUBLISHED",
                "publication_type": "SINGLE",
                "content_type": "SINGLE_ARTWORK",
                "theme_id": "cats_in_art",
                "theme_family": "animals",
                "carousel_format": "THEMATIC_COLLECTION",
            },
            {
                "id": "aic_cover",
                "status": "PUBLISHED",
                "publication_type": "CAROUSEL",
                "publication_id": "carousel-1",
                "theme_id": "winter_light",
                "theme_family": "season",
                "carousel_format": "LIGHT_STUDY",
            },
        ]
    }
    _history_backend(monkeypatch, history)

    slots = history_tracker.get_recent_carousel_theme_history()

    assert [slot.theme_id for slot in slots] == ["winter_light"]


def test_legacy_theme_history_remains_loadable_without_invented_taxonomy(monkeypatch):
    history = {"posted_artworks": [{"theme": "winter", "status": "PUBLISHED"}]}
    _history_backend(monkeypatch, history)

    slots = history_tracker.get_recent_carousel_theme_history()

    assert len(slots) == 1
    assert slots[0].theme_id == "winter"
    assert slots[0].theme_family is None
    assert slots[0].carousel_format is None


def test_ambiguous_and_definite_failure_transitions_apply_to_all_nine(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()
    ids = [cover["id"], *[art["id"] for art in featured]]
    history_tracker.reserve_carousel(cover, featured)

    assert history_tracker.mark_artworks_publishing(ids) == 9
    assert {record["status"] for record in history["posted_artworks"]} == {"PUBLISHING"}
    assert history_tracker.mark_artworks_pending(ids) == 9
    assert {record["status"] for record in history["posted_artworks"]} == {"PENDING"}

    history_tracker.mark_artworks_publishing(ids)
    assert history_tracker.mark_artworks_ambiguous(ids) == 9
    assert {record["status"] for record in history["posted_artworks"]} == {"AMBIGUOUS"}
    assert history_tracker.get_posted_ids() == set(ids)
    assert history_tracker.recover_stale_reservations(datetime(2030, 1, 1, tzinfo=timezone.utc)) == 0


def test_successful_finalization_preserves_cover_and_featured_roles(monkeypatch):
    history, _ = _history_backend(monkeypatch)
    cover, featured = _publication()
    ids = [cover["id"], *[art["id"] for art in featured]]
    history_tracker.reserve_carousel(cover, featured)
    history_tracker.mark_artworks_publishing(ids)

    assert history_tracker.confirm_carousel_publication(
        cover["id"],
        [art["id"] for art in featured],
        "instagram-media-1",
    ) == 9

    records = history["posted_artworks"]
    assert {record["status"] for record in records} == {"PUBLISHED"}
    assert {record["media_id"] for record in records} == {"instagram-media-1"}
    assert records[0]["publication_role"] == "COVER"
    assert [record["featured_position"] for record in records[1:]] == list(range(1, 9))


def test_legacy_history_records_continue_loading_without_role_reinterpretation(monkeypatch):
    legacy = {
        "posted_artworks": [
            {"id": "artic_84774", "title": "Legacy", "status": "PUBLISHED"},
            {"id": "met_legacy", "title": "Older schema"},
        ]
    }
    _history_backend(monkeypatch, legacy)

    assert history_tracker.get_posted_ids() == {"aic_84774", "met_legacy"}
    assert history_tracker.get_recent_history() == legacy["posted_artworks"]
    assert all("publication_role" not in record for record in legacy["posted_artworks"])


def test_carousel_reservation_rejects_cover_featured_identity_collision_before_write(monkeypatch):
    _, uploads = _history_backend(monkeypatch)
    cover, featured = _publication()
    featured[0]["id"] = cover["id"]

    with pytest.raises(ValueError, match="all be distinct"):
        history_tracker.reserve_carousel(cover, featured)

    assert uploads == []
