from datetime import datetime, timedelta, timezone

from src.engagement_features import EngagementFeatureVector
from src.feed_analytics import build_feed_analytics_report

NOW = datetime(2026, 10, 7, 19, tzinfo=timezone.utc)


def story_delivery(
    index=0,
    narrative="single_study",
    cover="artwork_first",
    headline="factual_collection",
):
    source_ids = (
        [f"aic_{index}"]
        if narrative == "single_study"
        else [f"aic_{index}", f"met_{index}"]
    )
    roles = [("cover", (source_ids[0],)), ("artwork", (source_ids[0],))]
    if len(source_ids) == 2:
        roles.extend([("artwork", (source_ids[1],)), ("comparison", tuple(source_ids))])
    else:
        roles.append(("detail", (source_ids[0],)))
    roles.append(("closing", (source_ids[0],)))
    return {
        "schema_version": "artfolio-story-delivery-v1",
        "reviewed_revision": 1,
        "reviewed_digest": "b" * 64,
        "source_ids": source_ids,
        "source_sha256": {source_id: "c" * 64 for source_id in source_ids},
        "artwork_sha256": {source_id: "d" * 64 for source_id in source_ids},
        "caption_sha256": "e" * 64,
        "narrative": narrative,
        "cover_variant": cover,
        "headline_kind": headline,
        "public_title": "A study in blue",
        "theme_id": "blue-study",
        "theme_title": "A study in blue",
        "pages": [
            {
                "id": f"page-{page}",
                "role": role,
                "artwork_ids": artwork_ids,
                "sha256": "a" * 64,
            }
            for page, (role, artwork_ids) in enumerate(roles)
        ],
    }


def publication(index=0, delivery=None):
    artwork_ids = (
        delivery["source_ids"] if delivery else [f"aic_{index}", f"met_{index}"]
    )
    return {
        "id": f"p-{index}",
        "media_id": f"m-{index}",
        "type": "carousel",
        "artwork_ids": artwork_ids,
        "posted_at": (NOW - timedelta(hours=80)).isoformat(),
        **({"story_delivery": delivery} if delivery else {}),
    }


def snapshot(pub):
    return {
        "publication_id": pub["id"],
        "media_id": pub["media_id"],
        "target_age_hours": 72,
        "captured_at": (
            datetime.fromisoformat(pub["posted_at"]) + timedelta(hours=72)
        ).isoformat(),
        "metrics": {"reach": 100, "saved": 10},
    }


def test_context_vector_derives_story_fields_without_inventing_featured_count():
    delivery = story_delivery(narrative="comparison")
    features = EngagementFeatureVector.from_context(
        {"story_delivery": delivery, "publication_format": "carousel"}
    )
    assert features.narrative == "comparison"
    assert features.headline_kind == "factual_collection"
    assert features.page_count == 5
    assert features.cover_variant == "artwork_first"
    assert features.featured_count is None
    assert "narrative:comparison" in features.context_feature_keys()
    assert "headline_kind:factual_collection" in features.context_feature_keys()
    assert "page_count:5" in features.context_feature_keys()


def test_story_cohorts_are_publication_level_and_keep_equal_age_rate_semantics():
    pubs = [
        publication(
            i, story_delivery(i, narrative="comparison" if i < 2 else "single_study")
        )
        for i in range(4)
    ]
    result = build_feed_analytics_report(
        {"publications": pubs},
        [snapshot(pub) for pub in pubs],
        now=NOW,
        minimum_cohort_size=2,
    )
    assert all("story_delivery" in row for row in result["windows"])
    cohort = next(
        row
        for row in result["story_cohorts"]
        if row["dimension"] == "narrative"
        and row["value"] == "comparison"
        and row["target_age_hours"] == 72
    )
    assert cohort["eligible_publications"] == 2
    assert cohort["usable_publications"] == 2
    assert cohort["rates"]["save_rate"]["observations"] == 2
    assert cohort["rates"]["save_rate"]["weighted_rate"] == 0.1
    assert cohort["minimum_cohort_met"] is True
    assert cohort["winner"] is None
    assert result["summary"]["feed_publications"] == 4


def test_genuine_single_source_story_publication_is_validated_and_reported():
    pub = publication(0, story_delivery(0, narrative="single_study"))
    result = build_feed_analytics_report(
        {"publications": [pub]}, [snapshot(pub)], now=NOW
    )
    assert result["summary"]["feed_publications"] == 1
    row = next(row for row in result["windows"] if row["target_age_hours"] == 72)
    assert row["story_delivery"]["narrative"] == "single_study"
    assert row["story_delivery"]["page_count"] == 4


def test_invalid_story_source_mapping_excludes_publication():
    delivery = story_delivery(0, narrative="comparison")
    delivery["source_sha256"] = {"met_wrong": "c" * 64}
    pub = publication(0, delivery)
    result = build_feed_analytics_report({"publications": [pub]}, [], now=NOW)
    assert result["summary"]["feed_publications"] == 0
    assert result["diagnostics"]["invalid_publications"] == 1


def test_nonstory_publications_are_excluded_from_story_cohorts_and_pages_do_not_duplicate_samples():
    story = publication(0, story_delivery(narrative="comparison"))
    plain = publication(1)
    result = build_feed_analytics_report(
        {"publications": [story, plain]}, [snapshot(story), snapshot(plain)], now=NOW
    )
    cohort = next(
        row
        for row in result["story_cohorts"]
        if row["dimension"] == "cover_variant"
        and row["value"] == "artwork_first"
        and row["target_age_hours"] == 72
    )
    assert cohort["eligible_publications"] == 1
    assert cohort["usable_publications"] == 1
    assert (
        len([row for row in result["windows"] if row["publication_id"] == "p-0"]) == 5
    )
