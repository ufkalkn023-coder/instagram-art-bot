from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import main
from src import history_tracker, instagram_poster, r2_media
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown


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


def _owned_upload(path, publication_id):
    return r2_media.TempMediaUpload(
        f"images/publications/{publication_id}/"
        "20260826120000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        f"https://example.test/{path}",
        publication_id,
    )


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


def test_pre_meta_prefix_cleanup_waits_for_durable_expiration(monkeypatch):
    upload = _owned_upload("validated-artwork.jpg", "publication-1")
    monkeypatch.setattr(
        main.history_tracker,
        "mark_publication_not_published",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("CAS failed")),
    )
    rolled_back = []
    monkeypatch.setattr(
        main.r2_media,
        "rollback_temp_media_uploads",
        lambda publication_id, uploads: rolled_back.append(
            (publication_id, tuple(uploads))
        ),
    )
    monkeypatch.setattr(
        main.r2_media,
        "cleanup_publication_media",
        lambda *args, **kwargs: pytest.fail(
            "prefix cleanup ran before durable EXPIRED"
        ),
    )

    main._handle_pre_meta_staging_failure(
        ["aic_84774"], "publication-1", [upload]
    )

    assert rolled_back == [("publication-1", (upload,))]


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
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path, publication_id: _owned_upload(path, publication_id),
    )

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
