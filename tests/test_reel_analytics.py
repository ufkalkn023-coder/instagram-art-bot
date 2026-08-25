import json
from datetime import datetime, timedelta, timezone

import pytest

from src.insights_collector import merge_associations
from src.insights_storage import InsightsStorageError
from src.instagram_insights import InstagramMedia
from src.reel_analytics import (
    LocalReel,
    derive_engagement_rates,
    load_local_reels,
    match_recent_media,
    safe_rate,
)


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def _reel(identifier="met_1", caption="Exact caption", title="The Artwork", artist="The Artist", age_hours=24):
    return LocalReel(identifier, identifier, title, artist, NOW - timedelta(hours=age_hours), caption, f"/renders/{identifier}.mp4")


def _media(identifier="ig_1", caption="Exact caption", timestamp=None):
    return InstagramMedia(identifier, "VIDEO", "REELS", caption, f"https://instagram.com/reel/{identifier}/", timestamp or NOW.isoformat())


def test_exact_and_normalized_caption_matching_are_distinct():
    exact = match_recent_media((_reel(),), (_media(),), [], NOW)
    assert exact.associations[0]["match_method"] == "caption_exact"

    normalized = match_recent_media(
        (_reel(caption="A  caption\nwith SPACE"),),
        (_media(caption="a caption with   space"),),
        [],
        NOW,
    )
    assert normalized.associations[0]["match_method"] == "caption_normalized"


def test_title_artist_timestamp_is_secondary_evidence():
    reel = _reel(caption=None, title="Madonna and Child", artist="Filippino Lippi", age_hours=48)
    media = _media(caption="Madonna and Child — Filippino Lippi, ca. 1483")
    result = match_recent_media((reel,), (media,), [], NOW)
    assert result.associations[0]["match_method"] == "title_artist_timestamp"

    too_late = InstagramMedia(media.id, media.media_type, media.media_product_type, media.caption, media.permalink, (NOW + timedelta(days=31)).isoformat())
    assert match_recent_media((reel,), (too_late,), [], NOW).unmatched_media_ids == ("ig_1",)

    too_early = InstagramMedia(media.id, media.media_type, media.media_product_type, "Exact caption", media.permalink, (reel.produced_at - timedelta(hours=7)).isoformat())
    assert match_recent_media((_reel(age_hours=48),), (too_early,), [], NOW).unmatched_media_ids == ("ig_1",)


def test_media_published_before_current_production_ledger_stays_unmatched_even_with_identical_caption():
    first_production = datetime(2026, 8, 22, 17, 44, 44, tzinfo=timezone.utc)
    older_media = datetime(2026, 8, 9, 9, 36, 11, tzinfo=timezone.utc)
    reel = LocalReel(
        "met_853157",
        "met_853157",
        "Portrait of a Sri Lankan Tamil",
        "Samuel Daniell",
        first_production,
        "identical caption",
        "/renders/met_853157.mp4",
    )
    media = _media("ig-before-ledger", caption="identical caption", timestamp=older_media.isoformat())

    result = match_recent_media((reel,), (media,), [], NOW)

    assert result.associations == ()
    assert result.ambiguous_media_ids == ()
    assert result.unmatched_media_ids == ("ig-before-ledger",)


def test_ambiguous_or_competing_candidates_are_never_auto_linked():
    first = _reel("met_1")
    second = _reel("met_2")
    ambiguous = match_recent_media((first, second), (_media(),), [], NOW)
    assert ambiguous.associations == ()
    assert ambiguous.ambiguous_media_ids == ("ig_1",)

    competing = match_recent_media((first,), (_media("ig_1"), _media("ig_2")), [], NOW)
    assert competing.associations == ()
    assert competing.ambiguous_media_ids == ("ig_1", "ig_2")


def test_stronger_exact_match_wins_over_normalized_competitor():
    reel = _reel(caption="Mixed Case")
    exact = _media("ig-exact", caption="Mixed Case")
    normalized = _media("ig-normalized", caption="mixed   case")
    result = match_recent_media((reel,), (normalized, exact), [], NOW)
    assert [item["instagram_media_id"] for item in result.associations] == ["ig-exact"]
    assert result.associations[0]["match_method"] == "caption_exact"
    assert result.ambiguous_media_ids == ("ig-normalized",)


def test_existing_manual_mapping_takes_precedence_and_duplicates_fail():
    existing = [{
        "canonical_artwork_id": "met_1", "reel_id": "met_1", "instagram_media_id": "manual-media",
        "published_at": NOW.isoformat(), "matched_at": NOW.isoformat(), "match_method": "manual",
    }]
    result = match_recent_media((_reel("met_1"),), (_media("new-media"),), existing, NOW)
    assert result.associations == ()
    assert result.unmatched_media_ids == ("new-media",)
    with pytest.raises(InsightsStorageError, match="Duplicate"):
        merge_associations(existing, ({**existing[0], "instagram_media_id": "other"},))


def test_rate_helpers_require_real_positive_reach():
    assert derive_engagement_rates({"reach": 100, "saved": 4, "shares": 2, "likes": 10, "comments": 1}) == {
        "save_rate": 0.04, "share_rate": 0.02, "like_rate": 0.1, "comment_rate": 0.01,
    }
    assert derive_engagement_rates({"reach": 0, "saved": 4}) == {}
    assert safe_rate(None, 10) is None
    assert safe_rate(1, 0) is None


def test_local_catalog_reads_rendered_history_plan_and_social_copy(tmp_path):
    (tmp_path / "data" / "reels").mkdir(parents=True)
    (tmp_path / "output" / "social").mkdir(parents=True)
    history = {
        "version": "reel-production-history-v1",
        "entries": [{
            "canonicalId": "met_1", "status": "RENDERED", "renderedAt": "2026-08-24T12:00:00Z",
            "renderPath": "/renders/met_1.mp4",
        }],
    }
    plan = {"id": "met_1", "artworks": [{"id": "met_1", "title": "Work", "artist": "Artist"}]}
    (tmp_path / "data" / "reel-production-history.json").write_text(json.dumps(history))
    (tmp_path / "data" / "reels" / "met_1.json").write_text(json.dumps(plan))
    (tmp_path / "output" / "social" / "met_1-work.txt").write_text("caption\n")
    reels = load_local_reels(tmp_path)
    assert reels[0].caption == "caption\n"
    assert reels[0].title == "Work"
