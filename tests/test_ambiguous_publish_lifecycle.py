from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import main
from src import history_tracker, instagram_poster
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.instagram_image import (
    InstagramImagePublishability,
    InstagramImagePublishabilityReason,
    PreparedSingleImage,
    SingleImageProcessing,
)


def _artwork(artwork_id="aic_84774"):
    return {
        "id": artwork_id,
        "title": "Test Artwork",
        "artist": "Test Artist",
        "date": "1900",
        "museum": "Test Museum",
        "local_image_path": "/tmp/test-artwork.jpg",
    }


def _cover(artwork_id="met_cover"):
    artwork = _artwork(artwork_id)
    breakdown = CoverScoreBreakdown(20, 30, 12, 9, 9, 4, 4)
    return CoverAsset(artwork, artwork["local_image_path"], CoverMode.FULL_ARTWORK, breakdown.total, breakdown)


def test_ambiguous_reservation_is_a_permanent_canonical_duplicate_lock(monkeypatch):
    history = {
        "posted_artworks": [
            {
                "id": "artic_84774",
                "status": "PENDING",
                "reservation_id": "reservation-1",
                "reserved_at": "2026-08-22T10:00:00Z",
            }
        ]
    }
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda value, etag: uploads.append((value, etag)),
    )

    assert history_tracker.mark_artwork_ambiguous("aic_84774") == 1
    record = history["posted_artworks"][0]
    assert record["id"] == "artic_84774"
    assert record["status"] == "AMBIGUOUS"
    assert record["reservation_id"] == "reservation-1"
    assert record["reserved_at"] == "2026-08-22T10:00:00Z"
    assert record["ambiguous_at"]
    assert "aic_84774" in history_tracker.get_posted_ids()
    assert history_tracker.get_recent_history() == []

    assert history_tracker.recover_stale_reservations(
        datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    ) == 0
    assert history_tracker.mark_artwork_ambiguous("artic_84774") == 0
    assert record["status"] == "AMBIGUOUS"
    assert len(uploads) == 1
    assert uploads[0][1] == "etag"


def test_recovery_expires_only_stale_pending_records(monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    history = {
        "posted_artworks": [
            {"id": "aic_stale", "status": "PENDING", "reserved_at": "2026-08-22T09:00:00Z"},
            {"id": "aic_active", "status": "PENDING", "reserved_at": "2026-08-22T11:00:00Z"},
            {"id": "aic_published", "status": "PUBLISHED", "reserved_at": "2026-08-22T08:00:00Z"},
            {"id": "aic_ambiguous", "status": "AMBIGUOUS", "reserved_at": "2026-08-22T08:00:00Z"},
        ]
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda value, etag: None)

    assert history_tracker.recover_stale_reservations(now) == 1
    assert [item["status"] for item in history["posted_artworks"]] == [
        "EXPIRED",
        "PENDING",
        "PUBLISHED",
        "AMBIGUOUS",
    ]


def _mock_single_post_dependencies(monkeypatch, post_result):
    artwork = _artwork()
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda: "warm")
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(main.history_tracker, "reserve_artwork", lambda value: None)
    monkeypatch.setattr(main.history_tracker, "start_publication_attempt", lambda *args: 1)
    monkeypatch.setattr(main.history_tracker, "mark_publication_not_published", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main.history_tracker, "record_publish_response", lambda *args: 1)
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda *args, **kwargs: iter([artwork]),
    )
    publishability = InstagramImagePublishability(
        publishable=True,
        reason=InstagramImagePublishabilityReason.SUPPORTED_AS_IS,
        width=1200,
        height=1200,
        aspect_ratio=1.0,
        image_format="JPEG",
        file_size=1234,
        exif_orientation=1,
        encoded_width=1200,
        encoded_height=1200,
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda *args: PreparedSingleImage(
            path="raw.jpg",
            source=publishability,
            publishability=publishability,
            processing=SingleImageProcessing.ZERO_TOUCH,
            source_bytes_preserved=True,
            compatibility_conversion=False,
        ),
    )
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.content_diversity, "select_content_type", lambda history: "SINGLE_ARTWORK")
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path: "https://example.test/validated-artwork.jpg",
    )
    def publish_with_boundary(**kwargs):
        kwargs["before_publish"]("container-1", ())
        return post_result(**kwargs)

    monkeypatch.setattr(main.instagram_poster, "post_to_instagram_graph_api", publish_with_boundary)
    return artwork


def test_single_ambiguous_publish_marks_history_and_reraises(monkeypatch):
    marked = []
    confirmed = []

    def publish(**kwargs):
        raise instagram_poster.InstagramPublishAmbiguousError("publish response lost")

    artwork = _mock_single_post_dependencies(monkeypatch, publish)
    monkeypatch.setattr(main.history_tracker, "mark_artwork_ambiguous", lambda artwork_id: marked.append(artwork_id))
    monkeypatch.setattr(main.history_tracker, "confirm_artwork", lambda *args: confirmed.append(args))

    with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
        main.run_single_post(SimpleNamespace(dry_run=False, image_url="https://example.test/image.jpg", pinterest=False))

    assert marked == [artwork["id"]]
    assert confirmed == []


def test_single_permanent_instagram_error_does_not_mark_ambiguous(monkeypatch):
    marked = []

    def publish(**kwargs):
        raise instagram_poster.InstagramAPIError("invalid image")

    _mock_single_post_dependencies(monkeypatch, publish)
    monkeypatch.setattr(main.history_tracker, "mark_artwork_ambiguous", lambda artwork_id: marked.append(artwork_id))

    with pytest.raises(instagram_poster.InstagramAPIError):
        main.run_single_post(SimpleNamespace(dry_run=False, image_url="https://example.test/image.jpg", pinterest=False))

    assert marked == []


def test_successful_single_publish_uploads_validated_asset_and_confirms_history(monkeypatch):
    confirmed = []
    published = []
    artwork = _mock_single_post_dependencies(
        monkeypatch,
        lambda **kwargs: published.append(kwargs) or "media-123",
    )
    monkeypatch.setattr(main.history_tracker, "confirm_artwork", lambda *args: confirmed.append(args))

    main.run_single_post(SimpleNamespace(dry_run=False, image_url="https://example.test/image.jpg", pinterest=False))

    assert published[0]["media_url"] == "https://example.test/validated-artwork.jpg"
    assert confirmed == [(artwork["id"], "media-123")]


def test_single_publish_boundary_write_failure_does_not_call_instagram(monkeypatch):
    calls = []
    _mock_single_post_dependencies(monkeypatch, lambda **kwargs: calls.append("publish"))
    monkeypatch.setattr(
        main.history_tracker,
        "start_publication_attempt",
        lambda *args: (_ for _ in ()).throw(OSError("R2 unavailable")),
    )

    with pytest.raises(OSError, match="R2 unavailable"):
        main.run_single_post(
            SimpleNamespace(dry_run=False, image_url="https://example.test/image.jpg", pinterest=False)
        )

    assert calls == []


def test_ambiguous_carousel_marks_every_reservation_and_reraises(monkeypatch):
    artworks = [_artwork(f"aic_{index}") for index in range(1, 9)]
    cover = _cover()
    confirmed = []
    history = {"posted_artworks": []}
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda: "warm")
    monkeypatch.setattr(main.history_tracker, "confirm_carousel_publication", lambda *args: confirmed.append(args))
    monkeypatch.setattr(main.history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(main.history_tracker, "_upload_history", lambda value, etag: None)
    monkeypatch.setattr(main.art_fetcher, "fetch_themed_artworks", lambda *args, **kwargs: artworks)
    monkeypatch.setattr(main, "select_editorial_cover", lambda **kwargs: cover)
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "create_carousel_editorial_cover", lambda **kwargs: "cover-post.jpg")
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda *args, **kwargs: SimpleNamespace(output_path="post.jpg"),
    )
    monkeypatch.setattr(main.image_processor, "upload_temp_media", lambda path: f"https://example.test/{path}")

    def publish(**kwargs):
        kwargs["before_publish"]("parent-container", tuple(f"child-{index}" for index in range(9)))
        raise instagram_poster.InstagramPublishAmbiguousError("publish response lost")

    monkeypatch.setattr(main.instagram_poster, "post_carousel_to_instagram_graph_api", publish)

    with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
        main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert [item["id"] for item in history["posted_artworks"]] == [
        cover.canonical_id,
        *[art["id"] for art in artworks],
    ]
    assert [item["status"] for item in history["posted_artworks"]] == ["AMBIGUOUS"] * 9
    assert confirmed == []
