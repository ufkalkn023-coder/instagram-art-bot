import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from src import content_diversity, history_tracker


POSTED_AT = "2026-08-24T10:00:00Z"


def _artwork(artwork_id: str, *, content_type=None, region="europe"):
    return {
        "id": artwork_id,
        "title": f"Artwork {artwork_id}",
        "artist": f"Artist {artwork_id}",
        "museum": "Museum",
        "region": region,
        "content_type": content_type,
    }


def _locked_artwork(
    artwork_id: str,
    publication_id: str,
    publication_type: str,
    *,
    status="PUBLISHING",
    content_type=None,
    region="europe",
):
    return {
        "id": artwork_id,
        "status": status,
        "media_id": None,
        "publication_id": publication_id,
        "publication_type": publication_type,
        "content_type": content_type,
        "region": region,
        "museum_name": "Museum",
        "artist_name": f"Artist {artwork_id}",
    }


def _publication(publication_id: str, media_id: str, artwork_ids, publication_type="single"):
    return {
        "id": publication_id,
        "type": publication_type,
        "media_id": media_id,
        "artwork_ids": list(artwork_ids),
        "posted_at": POSTED_AT,
    }


def _install_history(monkeypatch, history, uploads):
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag-1"))
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda value, etag: uploads.append((copy.deepcopy(value), etag)),
    )


def test_legacy_history_without_publications_loads_without_mutation(monkeypatch):
    legacy = {"posted_artworks": [{"id": "artic_84774", "title": "Legacy"}], "active_color_tone": "cool"}
    original = copy.deepcopy(legacy)
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (legacy, "etag"))

    assert history_tracker.get_recent_publications() == []
    assert history_tracker.get_recent_history() == legacy["posted_artworks"]
    assert history_tracker.get_recent_artworks_by_publication(1) == legacy["posted_artworks"]
    assert "aic_84774" in history_tracker.get_posted_ids()
    assert legacy == original


def test_batch_carousel_reservation_is_one_write_and_uses_neutral_child_content_type(monkeypatch):
    history = {"posted_artworks": []}
    uploads = []
    _install_history(monkeypatch, history, uploads)
    artworks = [_artwork(f"aic_{index}") for index in range(8)]

    publication_id = history_tracker.reserve_artworks(artworks, "carousel", "publication-carousel")

    assert publication_id == "publication-carousel"
    assert len(uploads) == 1
    assert uploads[0][1] == "etag-1"
    assert len(history["posted_artworks"]) == 8
    assert {item["publication_id"] for item in history["posted_artworks"]} == {publication_id}
    assert {item["publication_type"] for item in history["posted_artworks"]} == {"carousel"}
    assert {item["content_type"] for item in history["posted_artworks"]} == {None}
    assert "publications" not in history


def test_single_reservation_keeps_editorial_type_separate_from_publication_type(monkeypatch):
    history = {"posted_artworks": []}
    uploads = []
    _install_history(monkeypatch, history, uploads)

    publication_id = history_tracker.reserve_artwork(
        _artwork("aic_1", content_type="DETAIL_FOCUS"),
        publication_id="publication-single",
    )

    record = history["posted_artworks"][0]
    assert publication_id == "publication-single"
    assert record["publication_type"] == "single"
    assert record["content_type"] == "DETAIL_FOCUS"


def test_batch_reservation_failure_restores_legacy_history(monkeypatch):
    history = {"posted_artworks": [{"id": "met_old", "status": "PUBLISHED"}]}
    original = copy.deepcopy(history)
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda *args: (_ for _ in ()).throw(OSError("R2 unavailable")),
    )

    with pytest.raises(OSError, match="R2 unavailable"):
        history_tracker.reserve_artworks([_artwork("aic_1"), _artwork("aic_2")], "carousel")

    assert history == original


def test_single_finalization_writes_one_artwork_one_publication_and_preserves_tone(monkeypatch):
    history = {
        "posted_artworks": [
            _locked_artwork(
                "aic_1", "publication-single", "single", content_type="DETAIL_FOCUS"
            )
        ],
        "active_color_tone": "cool",
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)

    publication = history_tracker.confirm_artworks_and_record_publication(
        ["aic_1"],
        "media-single",
        "single",
        publication_id="publication-single",
        content_type="DETAIL_FOCUS",
    )

    assert publication == {
        "id": "publication-single",
        "type": "single",
        "media_id": "media-single",
        "artwork_ids": ["aic_1"],
        "posted_at": publication["posted_at"],
        "content_type": "DETAIL_FOCUS",
    }
    assert history["posted_artworks"][0]["status"] == "PUBLISHED"
    assert history["posted_artworks"][0]["media_id"] == "media-single"
    assert history["posted_artworks"][0]["posted_at"] == publication["posted_at"]
    assert history["publications"] == [publication]
    assert history["grid_publication_count"] == 1
    assert history["active_color_tone"] == "cool"
    assert len(uploads) == 1


def test_eight_artwork_carousel_finalizes_as_exactly_one_publication(monkeypatch):
    artwork_ids = [f"aic_{index}" for index in range(8)]
    history = {
        "posted_artworks": [
            _locked_artwork(artwork_id, "publication-carousel", "carousel")
            for artwork_id in artwork_ids
        ],
        "active_color_tone": "warm",
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)

    publication = history_tracker.confirm_artworks_and_record_publication(
        artwork_ids,
        "media-carousel",
        "carousel",
        publication_id="publication-carousel",
        theme="portrait",
    )

    assert len(history["posted_artworks"]) == 8
    assert {item["status"] for item in history["posted_artworks"]} == {"PUBLISHED"}
    assert {item["media_id"] for item in history["posted_artworks"]} == {"media-carousel"}
    assert len(history["publications"]) == 1
    assert publication["type"] == "carousel"
    assert publication["artwork_ids"] == artwork_ids
    assert publication["theme"] == "portrait"
    assert "content_type" not in publication
    assert history["grid_publication_count"] == 1
    assert len(uploads) == 1


def test_publication_finalization_is_idempotent_by_stable_id(monkeypatch):
    history = {
        "posted_artworks": [_locked_artwork("aic_1", "publication-1", "single")],
        "active_color_tone": "warm",
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)

    first = history_tracker.confirm_artworks_and_record_publication(
        ["aic_1"], "media-1", "single", publication_id="publication-1"
    )
    second = history_tracker.confirm_artworks_and_record_publication(
        ["aic_1"], "media-1", "single", publication_id="publication-1"
    )

    assert second == first
    assert len(history["publications"]) == 1
    assert history["grid_publication_count"] == 1
    assert len(uploads) == 1


def test_carousel_finalization_rejects_a_subset_of_the_reserved_group(monkeypatch):
    history = {
        "posted_artworks": [
            _locked_artwork(artwork_id, "publication-carousel", "carousel")
            for artwork_id in ["aic_1", "aic_2", "aic_3"]
        ]
    }
    original = copy.deepcopy(history)
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(RuntimeError, match="complete reservation group"):
        history_tracker.confirm_artworks_and_record_publication(
            ["aic_1", "aic_2"],
            "media-carousel",
            "carousel",
            publication_id="publication-carousel",
        )

    assert history == original
    assert uploads == []


def test_grid_counter_rotates_once_after_third_new_publication(monkeypatch):
    history = {
        "posted_artworks": [_locked_artwork("aic_3", "publication-3", "single")],
        "publications": [
            _publication("publication-1", "media-1", ["aic_1"]),
            _publication("publication-2", "media-2", ["aic_2"]),
        ],
        "grid_publication_count": 2,
        "active_color_tone": "warm",
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)
    monkeypatch.setattr(history_tracker.random, "choice", lambda tones: "blue")

    history_tracker.confirm_artworks_and_record_publication(
        ["aic_3"], "media-3", "single", publication_id="publication-3"
    )

    assert history["grid_publication_count"] == 3
    assert history["active_color_tone"] == "blue"
    assert history_tracker.get_grid_color_tone() == "blue"
    assert len(uploads) == 1


def test_recent_publication_window_gives_one_slot_to_a_carousel(monkeypatch):
    carousel_ids = [f"aic_carousel_{index}" for index in range(8)]
    history = {
        "posted_artworks": [
            {"id": "met_legacy_1"},
            {"id": "met_legacy_2"},
            *[
                {
                    "id": artwork_id,
                    "status": "PUBLISHED",
                    "publication_id": "publication-carousel",
                    "media_id": "media-carousel",
                }
                for artwork_id in carousel_ids
            ],
            {
                "id": "aic_single",
                "status": "PUBLISHED",
                "publication_id": "publication-single",
                "media_id": "media-single",
            },
        ],
        "publications": [
            _publication("publication-carousel", "media-carousel", carousel_ids, "carousel"),
            _publication("publication-single", "media-single", ["aic_single"]),
        ],
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    assert [item["id"] for item in history_tracker.get_recent_artworks_by_publication(1)] == ["aic_single"]
    assert [item["id"] for item in history_tracker.get_recent_artworks_by_publication(2)] == [
        *carousel_ids,
        "aic_single",
    ]


def test_recent_window_uses_publication_order_not_reservation_order(monkeypatch):
    history = {
        "posted_artworks": [
            {
                "id": "aic_a",
                "status": "PUBLISHED",
                "publication_id": "publication-a",
                "media_id": "media-a",
            },
            {
                "id": "aic_b",
                "status": "PUBLISHED",
                "publication_id": "publication-b",
                "media_id": "media-b",
            },
        ],
        # B crossed the Instagram boundary first even though A reserved first.
        "publications": [
            _publication("publication-b", "media-b", ["aic_b"]),
            _publication("publication-a", "media-a", ["aic_a"]),
        ],
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    assert [item["id"] for item in history_tracker.get_recent_artworks_by_publication(1)] == [
        "aic_a"
    ]


def test_diversity_window_flattens_every_artwork_in_one_carousel_slot():
    recent_history = [
        *[
            {
                "id": f"aic_{index}",
                "publication_id": "publication-carousel",
                "region": "europe",
            }
            for index in range(8)
        ],
        *[
            {
                "id": f"met_{index}",
                "publication_id": f"publication-single-{index}",
                "region": "east_asia",
            }
            for index in range(5)
        ],
    ]

    assert content_diversity.analyze_regional_diversity("europe", recent_history) == -30.0


def test_malformed_publications_fail_closed_without_upload(monkeypatch):
    history = {
        "posted_artworks": [_locked_artwork("aic_1", "publication-1", "single")],
        "publications": {"not": "a list"},
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="must be a list"):
        history_tracker.get_recent_publications()
    with pytest.raises(history_tracker.CorruptedHistoryError, match="must be a list"):
        history_tracker.confirm_artworks_and_record_publication(
            ["aic_1"], "media-1", "single", publication_id="publication-1"
        )

    assert history["posted_artworks"][0]["status"] == "PUBLISHING"
    assert uploads == []


def test_mismatched_grid_counter_fails_closed_before_finalization(monkeypatch):
    history = {
        "posted_artworks": [_locked_artwork("aic_2", "publication-2", "single")],
        "publications": [_publication("publication-1", "media-1", ["aic_1"])],
        "grid_publication_count": 2,
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="must equal"):
        history_tracker.confirm_artworks_and_record_publication(
            ["aic_2"], "media-2", "single", publication_id="publication-2"
        )

    assert history["posted_artworks"][0]["status"] == "PUBLISHING"
    assert uploads == []


def test_post_publish_history_failure_keeps_every_artwork_publishing(monkeypatch):
    artwork_ids = ["aic_1", "aic_2"]
    history = {
        "posted_artworks": [
            _locked_artwork(artwork_id, "publication-carousel", "carousel")
            for artwork_id in artwork_ids
        ]
    }
    original = copy.deepcopy(history)
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda *args: (_ for _ in ()).throw(OSError("R2 finalization failed")),
    )

    with pytest.raises(OSError, match="R2 finalization failed"):
        history_tracker.confirm_artworks_and_record_publication(
            artwork_ids,
            "media-carousel",
            "carousel",
            publication_id="publication-carousel",
        )

    assert history == original
    assert history_tracker.get_posted_ids() == set(artwork_ids)
    assert "publications" not in history


def test_conditional_upload_translates_r2_precondition_failure(monkeypatch):
    calls = []

    class FakeS3:
        def put_object(self, **kwargs):
            calls.append(kwargs)
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "stale ETag"}},
                "PutObject",
            )

    monkeypatch.setattr(history_tracker, "_get_s3_client", lambda: FakeS3())
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "bucket")

    with pytest.raises(history_tracker.ConcurrentWriteError):
        history_tracker._upload_history({"posted_artworks": []}, '"etag-1"')

    assert calls[0]["IfMatch"] == '"etag-1"'


def test_single_publication_history_contains_its_artwork_exactly_once(monkeypatch):
    history = {
        "posted_artworks": [
            {
                "id": "aic_a",
                "status": "PUBLISHED",
                "publication_id": "publication-a",
                "publication_type": "single",
                "media_id": "media-a",
            }
        ],
        "publications": [_publication("publication-a", "media-a", ["aic_a"])],
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    recent = history_tracker.get_recent_history()

    assert len(recent) == 1
    assert [item["id"] for item in recent] == ["aic_a"]


def test_carousel_history_contains_each_artwork_exactly_once(monkeypatch):
    artwork_ids = [f"aic_{index}" for index in range(8)]
    history = {
        "posted_artworks": [
            {
                "id": artwork_id,
                "status": "PUBLISHED",
                "publication_id": "publication-carousel",
                "publication_type": "carousel",
                "media_id": "media-carousel",
            }
            for artwork_id in artwork_ids
        ],
        "publications": [
            _publication("publication-carousel", "media-carousel", artwork_ids, "carousel")
        ],
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    recent = history_tracker.get_recent_history()
    recent_ids = [item["id"] for item in recent]

    assert len(recent) == 8
    assert recent_ids == artwork_ids
    assert all(recent_ids.count(artwork_id) == 1 for artwork_id in artwork_ids)


def test_mixed_legacy_and_publication_history_has_three_distinct_logical_slots(monkeypatch):
    history = {
        "posted_artworks": [
            {"id": "met_legacy_1"},
            {"id": "met_legacy_2"},
            {
                "id": "aic_new_1",
                "status": "PUBLISHED",
                "publication_id": "publication-new-1",
                "publication_type": "single",
                "media_id": "media-new-1",
            },
        ],
        "publications": [
            _publication("publication-new-1", "media-new-1", ["aic_new_1"])
        ],
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    assert [
        item["id"] for item in history_tracker.get_recent_artworks_by_publication(1)
    ] == ["aic_new_1"]
    assert [
        item["id"] for item in history_tracker.get_recent_artworks_by_publication(2)
    ] == ["met_legacy_2", "aic_new_1"]
    assert [
        item["id"] for item in history_tracker.get_recent_artworks_by_publication(3)
    ] == ["met_legacy_1", "met_legacy_2", "aic_new_1"]

    flattened_ids = [item["id"] for item in history_tracker.get_recent_history()]
    assert len(flattened_ids) == 3
    assert flattened_ids == ["met_legacy_1", "met_legacy_2", "aic_new_1"]
    assert len(set(flattened_ids)) == 3


@pytest.mark.parametrize(
    ("status", "timestamp_kind", "included"),
    [
        pytest.param("PUBLISHED", None, True, id="published"),
        pytest.param("PENDING", "active", False, id="active-pending"),
        pytest.param("PENDING", "stale", False, id="stale-pending"),
        pytest.param("PENDING", "malformed", False, id="malformed-pending"),
        pytest.param("PUBLISHING", None, False, id="publishing"),
        pytest.param("AMBIGUOUS", None, False, id="ambiguous"),
        pytest.param("EXPIRED", None, False, id="expired"),
        pytest.param(None, None, True, id="legacy-statusless"),
        pytest.param("UNRECOGNIZED", None, False, id="unknown"),
    ],
)
def test_editorial_history_status_matrix(monkeypatch, status, timestamp_kind, included):
    now = datetime.now(timezone.utc)
    record = {"id": "aic_status"}
    if status is not None:
        record["status"] = status
    if timestamp_kind == "active":
        record["reserved_at"] = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif timestamp_kind == "stale":
        record["reserved_at"] = (
            now - history_tracker.PENDING_RESERVATION_TTL - timedelta(minutes=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif timestamp_kind == "malformed":
        record["reserved_at"] = "not-a-timestamp"
    history = {"posted_artworks": [record]}
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    recent = history_tracker.get_recent_history()

    assert (len(recent) == 1) is included


@pytest.mark.parametrize(
    ("status", "timestamp_kind", "protected"),
    [
        pytest.param("PUBLISHED", None, True, id="published"),
        pytest.param("PENDING", "active", True, id="active-pending"),
        pytest.param("PENDING", "stale", False, id="stale-pending"),
        pytest.param("PENDING", "malformed", True, id="malformed-pending"),
        pytest.param("PUBLISHING", None, True, id="publishing"),
        pytest.param("AMBIGUOUS", None, True, id="ambiguous"),
        pytest.param("EXPIRED", None, False, id="expired"),
        pytest.param(None, None, True, id="legacy-statusless"),
        pytest.param("UNRECOGNIZED", None, True, id="unknown"),
    ],
)
def test_duplicate_lock_status_matrix(monkeypatch, status, timestamp_kind, protected):
    now = datetime.now(timezone.utc)
    record = {"id": "aic_status"}
    if status is not None:
        record["status"] = status
    if timestamp_kind == "active":
        record["reserved_at"] = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif timestamp_kind == "stale":
        record["reserved_at"] = (
            now - history_tracker.PENDING_RESERVATION_TTL - timedelta(minutes=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif timestamp_kind == "malformed":
        record["reserved_at"] = "not-a-timestamp"
    history = {"posted_artworks": [record]}
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    assert ("aic_status" in history_tracker.get_posted_ids()) is protected


def _finalized_single_history():
    return {
        "posted_artworks": [
            {
                "id": "aic_1",
                "status": "PUBLISHED",
                "media_id": "media-1",
                "publication_id": "publication-1",
                "publication_type": "single",
                "posted_at": POSTED_AT,
            }
        ],
        "publications": [_publication("publication-1", "media-1", ["aic_1"])],
        "grid_publication_count": 1,
        "active_color_tone": "warm",
    }


def test_finalized_publication_rejects_conflicting_media_id_before_write(monkeypatch):
    history = _finalized_single_history()
    original = copy.deepcopy(history)
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(history_tracker.CorruptedHistoryError, match="Conflicting publication ID"):
        history_tracker.confirm_artworks_and_record_publication(
            ["aic_1"], "media-conflict", "single", publication_id="publication-1"
        )

    assert history == original
    assert uploads == []


def test_finalized_publication_rejects_conflicting_type_before_write(monkeypatch):
    history = _finalized_single_history()
    original = copy.deepcopy(history)
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(RuntimeError, match="publication type"):
        history_tracker.confirm_artworks_and_record_publication(
            ["aic_1"], "media-1", "carousel", publication_id="publication-1"
        )

    assert history == original
    assert uploads == []


def test_finalized_publication_rejects_conflicting_artwork_ids_before_write(monkeypatch):
    history = _finalized_single_history()
    history["posted_artworks"].append(
        {
            "id": "aic_2",
            "status": "PUBLISHED",
            "media_id": "media-2",
            "publication_id": "publication-2",
            "publication_type": "single",
            "posted_at": POSTED_AT,
        }
    )
    history["publications"].append(_publication("publication-2", "media-2", ["aic_2"]))
    history["grid_publication_count"] = 2
    original = copy.deepcopy(history)
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(RuntimeError, match="does not belong"):
        history_tracker.confirm_artworks_and_record_publication(
            ["aic_1", "aic_2"],
            "media-1",
            "single",
            publication_id="publication-1",
        )

    assert history == original
    assert uploads == []


def test_identical_carousel_retry_is_idempotent_without_another_upload(monkeypatch):
    artwork_ids = [f"aic_{index}" for index in range(8)]
    history = {
        "posted_artworks": [
            _locked_artwork(artwork_id, "publication-carousel", "carousel")
            for artwork_id in artwork_ids
        ],
        "active_color_tone": "warm",
    }
    uploads = []
    _install_history(monkeypatch, history, uploads)

    first = history_tracker.confirm_artworks_and_record_publication(
        artwork_ids,
        "media-carousel",
        "carousel",
        publication_id="publication-carousel",
        theme="portrait",
    )
    second = history_tracker.confirm_artworks_and_record_publication(
        artwork_ids,
        "media-carousel",
        "carousel",
        publication_id="publication-carousel",
        theme="portrait",
    )

    assert second == first
    assert len(history["publications"]) == 1
    assert history["grid_publication_count"] == 1
    assert len(uploads) == 1


def test_carousel_finalizer_uses_one_ordered_additive_history_update(monkeypatch):
    cover_id = "aic_cover"
    featured_ids = [f"aic_{index}" for index in range(1, 9)]
    artwork_ids = [cover_id, *featured_ids]
    finalizer = Mock(return_value={"artwork_ids": artwork_ids})
    monkeypatch.setattr(
        history_tracker, "confirm_artworks_and_record_publication", finalizer
    )

    finalized_count = history_tracker.confirm_carousel_publication(
        cover_id, featured_ids, "media-carousel"
    )

    assert finalized_count == 9
    finalizer.assert_called_once_with(
        artwork_ids, "media-carousel", "carousel"
    )


def test_legacy_grid_rotates_only_after_three_continuous_new_publications(monkeypatch):
    history = {
        "posted_artworks": [{"id": "met_legacy"}],
        "active_color_tone": "warm",
    }
    uploads = []
    rotations = []
    _install_history(monkeypatch, history, uploads)

    def choose_new_tone(tones):
        rotations.append(list(tones))
        return "blue"

    monkeypatch.setattr(history_tracker.random, "choice", choose_new_tone)

    for publication_number in range(1, 4):
        assert history_tracker.get_grid_color_tone() == "warm"
        history["posted_artworks"].append(
            _locked_artwork(
                f"aic_{publication_number}",
                f"publication-{publication_number}",
                "single",
            )
        )
        history_tracker.confirm_artworks_and_record_publication(
            [f"aic_{publication_number}"],
            f"media-{publication_number}",
            "single",
            publication_id=f"publication-{publication_number}",
        )
        assert history["grid_publication_count"] == publication_number
        if publication_number < 3:
            assert history["active_color_tone"] == "warm"
            assert rotations == []

    assert history["active_color_tone"] == "blue"
    assert len(rotations) == 1
    assert "warm" not in rotations[0]
    assert history_tracker.get_grid_color_tone() == "blue"


def test_malformed_publications_cannot_disable_legacy_duplicate_lock(monkeypatch):
    history = {
        "posted_artworks": [{"id": "artic_84774", "status": "PUBLISHED"}],
        "publications": {"malformed": "not-a-list"},
    }
    original = copy.deepcopy(history)
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    with pytest.raises(history_tracker.CorruptedHistoryError, match="must be a list"):
        history_tracker.get_recent_history()

    assert "aic_84774" in history_tracker.get_posted_ids()
    assert history == original


@pytest.mark.parametrize(
    ("records", "publication_type", "expected_error", "message"),
    [
        pytest.param(
            [
                _locked_artwork("aic_1", "publication-1", "carousel"),
                _locked_artwork("aic_2", "publication-1", "carousel"),
                {
                    **_locked_artwork("aic_extra", "publication-extra", "carousel"),
                    "publication_id": None,
                },
            ],
            "carousel",
            history_tracker.CorruptedHistoryError,
            "Only some target artworks",
            id="extra-unexpected-child",
        ),
        pytest.param(
            [
                _locked_artwork("aic_1", "publication-1", "carousel"),
                _locked_artwork("aic_2", "publication-2", "carousel"),
            ],
            "carousel",
            RuntimeError,
            "does not belong",
            id="different-publication-id",
        ),
        pytest.param(
            [
                _locked_artwork("aic_1", "publication-1", "carousel"),
                _locked_artwork("aic_2", "publication-1", "single"),
            ],
            "carousel",
            RuntimeError,
            "publication type",
            id="different-publication-type",
        ),
        pytest.param(
            [
                _locked_artwork("aic_1", "publication-1", "single"),
                _locked_artwork("aic_2", "publication-1", "single"),
            ],
            "single",
            ValidationError,
            "single publications require exactly one artwork",
            id="single-with-multiple-children",
        ),
    ],
)
def test_invalid_reservation_groups_fail_before_write(
    monkeypatch, records, publication_type, expected_error, message
):
    history = {"posted_artworks": copy.deepcopy(records)}
    original = copy.deepcopy(history)
    uploads = []
    _install_history(monkeypatch, history, uploads)

    with pytest.raises(expected_error, match=message):
        history_tracker.confirm_artworks_and_record_publication(
            [record["id"] for record in records],
            "media-1",
            publication_type,
            publication_id="publication-1",
        )

    assert history == original
    assert uploads == []


def test_stale_recovery_preserves_publication_and_grid_metadata(monkeypatch):
    publications = [_publication("publication-1", "media-1", ["aic_published"])]
    history = {
        "posted_artworks": [
            {
                "id": "aic_published",
                "status": "PUBLISHED",
                "media_id": "media-1",
                "publication_id": "publication-1",
                "publication_type": "single",
                "posted_at": POSTED_AT,
            },
            {
                "id": "aic_stale",
                "status": "PENDING",
                "reserved_at": "2026-08-24T07:00:00Z",
            },
            {
                "id": "aic_active",
                "status": "PENDING",
                "reserved_at": "2026-08-24T09:30:00Z",
            },
        ],
        "publications": publications,
        "grid_publication_count": 1,
        "active_color_tone": "cool",
    }
    original_publications = copy.deepcopy(history["publications"])
    uploads = []
    _install_history(monkeypatch, history, uploads)

    recovered = history_tracker.recover_stale_reservations(
        datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    )

    assert recovered == 1
    assert history["posted_artworks"][0]["status"] == "PUBLISHED"
    assert history["posted_artworks"][1]["status"] == "EXPIRED"
    assert history["posted_artworks"][2]["status"] == "PENDING"
    assert history["publications"] == original_publications
    assert history["grid_publication_count"] == 1
    assert history["active_color_tone"] == "cool"
    assert len(uploads) == 1


def test_legacy_history_reads_never_generate_publication_uuid(monkeypatch):
    legacy = {
        "posted_artworks": [{"id": "met_legacy", "title": "Legacy"}],
        "active_color_tone": "warm",
    }
    uuid4 = Mock(side_effect=AssertionError("legacy reads must not generate UUIDs"))
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (legacy, "etag"))
    monkeypatch.setattr(history_tracker.uuid, "uuid4", uuid4)

    assert history_tracker.get_recent_publications() == []
    assert history_tracker.get_recent_history() == legacy["posted_artworks"]
    assert history_tracker.get_recent_artworks_by_publication(1) == legacy["posted_artworks"]
    assert history_tracker.get_posted_ids() == {"met_legacy"}
    assert history_tracker.get_grid_color_tone() == "warm"
    uuid4.assert_not_called()


def test_cleanup_queue_survives_expired_artwork_rereservation(monkeypatch):
    history = {
        "posted_artworks": [
            {
                "id": "aic_1",
                "publication_id": "expired-publication",
                "publication_type": "single",
                "status": "EXPIRED",
                "reserved_at": "2026-08-26T09:00:00Z",
                "expired_at": "2026-08-26T11:00:00Z",
            }
        ],
        history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY: [
            {
                "publication_id": "expired-publication",
                "eligible_at": "2026-08-26T11:00:00Z",
                "reason": "pre_meta_staging_failure",
            }
        ],
    }
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, "etag")
    )
    monkeypatch.setattr(history_tracker, "_upload_history", lambda *args: None)

    new_publication_id = history_tracker.reserve_artwork(_artwork("aic_1"))

    assert new_publication_id != "expired-publication"
    assert history["posted_artworks"][0]["publication_id"] == new_publication_id
    assert history_tracker.list_staging_media_cleanup_publication_ids(
        limit=10
    ) == ["expired-publication"]


def test_cleanup_queue_skips_active_publication_ids(monkeypatch):
    history = {
        "posted_artworks": [
            {
                "id": "aic_1",
                "publication_id": "publication-active",
                "publication_type": "single",
                "status": "PUBLISHING",
                "reserved_at": "2026-08-26T09:00:00Z",
            }
        ],
        history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY: [
            {
                "publication_id": "publication-active",
                "eligible_at": "2026-08-26T11:00:00Z",
                "reason": "stale_entry",
            }
        ],
    }
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, "etag")
    )

    assert history_tracker.list_staging_media_cleanup_publication_ids(
        limit=10
    ) == []


def test_malformed_cleanup_queue_fails_closed(monkeypatch):
    history = {
        "posted_artworks": [],
        history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY: [
            {
                "publication_id": "../posted_history",
                "eligible_at": "2026-08-26T11:00:00Z",
                "reason": "unsafe",
            }
        ],
    }
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, "etag")
    )

    assert history_tracker.list_staging_media_cleanup_publication_ids(
        limit=10
    ) == []
