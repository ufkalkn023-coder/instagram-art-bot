import inspect
import os
from types import SimpleNamespace

import pytest

import main
from src import instagram_poster, r2_media
from src.art_fetcher import SelectionRunSeed
from src.carousel_cover import EditorialCoverSelectionError
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_themes import get_default_theme_registry


def _artwork(identifier, number):
    return {
        "id": identifier,
        "title": f"Featured Title {number}",
        "artist": f"Featured Artist {number}",
        "date": str(1800 + number),
        "museum": f"Featured Museum {number}",
        "local_image_path": f"raw-{number}.jpg",
        "quality_score": 90.0,
        "selection_score": 90.0,
    }


def _featured():
    return [_artwork(f"aic_{index}", index) for index in range(1, 9)]


def _cover():
    artwork = {
        "id": "met_cover",
        "title": "COVER IDENTITY TITLE",
        "artist": "COVER IDENTITY ARTIST",
        "date": "1499",
        "museum": "COVER IDENTITY MUSEUM",
        "local_image_path": "raw-cover.jpg",
        "quality_score": 91.0,
        "selection_score": 91.0,
    }
    breakdown = CoverScoreBreakdown(20, 30, 12, 9, 9, 4, 4)
    return CoverAsset(artwork, "raw-cover.jpg", CoverMode.DETAIL_CROP, breakdown.total, breakdown)


def _owned_upload(path, publication_id):
    suffix = os.path.basename(path)
    nonce = f"{abs(hash(suffix)):032x}"[-32:]
    return r2_media.TempMediaUpload(
        f"images/publications/{publication_id}/20260826120000_{nonce}.jpg",
        f"https://media.example/{suffix}",
        publication_id,
    )


def _install_reads_and_selection(monkeypatch, calls, featured=None, cover=None):
    featured = featured or _featured()
    cover = cover or _cover()
    theme = get_default_theme_registry().by_id("winter_light")
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: "cool")
    monkeypatch.setattr(main.history_tracker, "get_recent_carousel_theme_history", lambda: [])
    monkeypatch.setattr(main, "plan_carousel_theme", lambda *args, **kwargs: SimpleNamespace(theme=theme))
    monkeypatch.setattr(
        main.art_fetcher,
        "resolve_selection_run_seed",
        lambda: SelectionRunSeed("fixed", "test"),
    )
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda *args, **kwargs: calls.append("featured_selection") or featured,
    )
    monkeypatch.setattr(
        main,
        "select_editorial_cover",
        lambda **kwargs: calls.append("cover_selection") or cover,
    )
    return featured, cover


def _install_copy_and_render(monkeypatch, calls):
    monkeypatch.setattr(
        main.gemini_ai,
        "analyze_carousel",
        lambda theme, artworks: calls.append("gemini") or {
            "theme_title": "WINTER LIGHT",
            "editorial_subtitle": "Snow, silence and changing light across centuries.",
            "editorial_intro": " ".join(["Grounded editorial introduction"] * 10),
            "hashtags": "#Art #Winter #MuseumArt #Painting",
            "recommended_font_size": 46,
        },
    )
    monkeypatch.setattr(
        main,
        "create_carousel_editorial_cover",
        lambda **kwargs: calls.append("cover_render") or kwargs["output_path"],
    )
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda path, **kwargs: calls.append(f"featured_render:{os.path.basename(kwargs['output_path'])}")
        or SimpleNamespace(output_path=kwargs["output_path"]),
    )


def test_carousel_media_and_caption_order_are_cover_then_featured_one_through_eight(monkeypatch):
    calls = []
    featured, cover = _install_reads_and_selection(monkeypatch, calls)
    _install_copy_and_render(monkeypatch, calls)
    reserved = []
    protected = []
    published = []
    confirmed = []
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_carousel",
        lambda cover_artwork, featured_artworks, **theme_metadata: calls.append("reserve")
        or reserved.append((cover_artwork, featured_artworks, theme_metadata))
        or "publication-1",
    )
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path, publication_id: _owned_upload(path, publication_id),
    )
    monkeypatch.setattr(
        main.history_tracker,
        "start_publication_attempt",
        lambda ids, *args: protected.extend(ids) or 9,
    )
    monkeypatch.setattr(main.history_tracker, "record_publish_response", lambda *args: 9)

    def publish(**kwargs):
        kwargs["before_publish"]("parent-1", tuple(f"child-{index}" for index in range(9)))
        published.append(kwargs)
        return "media-1"

    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        publish,
    )
    monkeypatch.setattr(
        main.history_tracker,
        "confirm_carousel_publication",
        lambda *args: confirmed.append(args) or 9,
    )
    monkeypatch.setattr(
        main.r2_media,
        "cleanup_publication_media",
        lambda *args, **kwargs: pytest.fail("successful carousel media was cleaned"),
    )

    main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert calls[:3] == ["featured_selection", "cover_selection", "gemini"]
    assert calls.index("reserve") > calls.index("featured_render:carousel_08.jpg")
    assert reserved[0][0]["id"] == cover.canonical_id
    assert [art["id"] for art in reserved[0][1]] == [art["id"] for art in featured]
    assert reserved[0][2] == {
        "theme_id": "winter_light",
        "theme_family": "season",
        "carousel_format": "LIGHT_STUDY",
    }
    assert protected == [cover.canonical_id, *[art["id"] for art in featured]]
    assert published[0]["media_urls"] == [
        "https://media.example/carousel_cover.jpg",
        *[f"https://media.example/carousel_{index:02d}.jpg" for index in range(1, 9)],
    ]
    caption = published[0]["caption"]
    assert caption.count("Featured Title") == 8
    assert "1. Featured Title 1" in caption
    assert "8. Featured Title 8" in caption
    assert "COVER IDENTITY" not in caption
    assert caption.startswith("Winter Light\n")
    assert confirmed == [(cover.canonical_id, tuple(art["id"] for art in featured), "media-1")]


@pytest.mark.parametrize(("failure_attempt", "expected_rollbacks"), ((1, 0), (4, 3)))
def test_carousel_staging_failure_rolls_back_only_completed_owned_uploads(
    monkeypatch, failure_attempt, expected_rollbacks
):
    calls = []
    featured, cover = _install_reads_and_selection(monkeypatch, calls)
    _install_copy_and_render(monkeypatch, calls)
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_carousel",
        lambda *args, **kwargs: "publication-1",
    )
    uploaded = []

    def upload(path, publication_id):
        if len(uploaded) + 1 == failure_attempt:
            raise RuntimeError("staging failed")
        handle = _owned_upload(path, publication_id)
        uploaded.append(handle)
        return handle

    monkeypatch.setattr(main.image_processor, "upload_temp_media", upload)
    expired = []
    monkeypatch.setattr(
        main.history_tracker,
        "mark_publication_not_published",
        lambda ids, *args, **kwargs: expired.append(tuple(ids)) or len(ids),
    )
    exact_deletes = []
    monkeypatch.setattr(
        main.r2_media,
        "cleanup_temp_media_upload",
        lambda handle, **kwargs: exact_deletes.append(handle.object_key) or True,
    )
    prefix_cleanups = []
    monkeypatch.setattr(
        main.r2_media,
        "cleanup_publication_media",
        lambda publication_id, **kwargs: prefix_cleanups.append(publication_id)
        or r2_media.MediaCleanupSummary(
            publication_id, 0, 0, 0, True, kwargs["reason"]
        ),
    )
    acknowledged = []
    monkeypatch.setattr(
        main.history_tracker,
        "acknowledge_staging_media_cleanup",
        lambda publication_id: acknowledged.append(publication_id) or True,
    )
    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        lambda **kwargs: pytest.fail("Meta was called after staging failure"),
    )

    with pytest.raises(RuntimeError, match="staging failed"):
        main.run_carousel_post(
            SimpleNamespace(dry_run=False, image_url=None, pinterest=False)
        )

    assert len(uploaded) == expected_rollbacks
    assert exact_deletes == [
        handle.object_key for handle in reversed(uploaded)
    ]
    assert prefix_cleanups == ["publication-1"]
    assert acknowledged == ["publication-1"]
    assert expired == [
        (cover.canonical_id, *[art["id"] for art in featured])
    ]


def test_final_sequence_controls_artifacts_caption_reservation_and_history_positions(monkeypatch):
    calls = []
    featured, cover = _install_reads_and_selection(monkeypatch, calls)
    _install_copy_and_render(monkeypatch, calls)
    final_order = list(reversed(featured))
    monkeypatch.setattr(
        main,
        "sequence_carousel_artworks",
        lambda *args, **kwargs: SimpleNamespace(ordered_artworks=tuple(final_order)),
    )
    rendered = []
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda path, **kwargs: rendered.append(
            (path, os.path.basename(kwargs["output_path"]))
        )
        or SimpleNamespace(output_path=kwargs["output_path"]),
    )
    reserved = []
    published = []
    confirmed = []
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_carousel",
        lambda cover_artwork, featured_artworks, **metadata: reserved.extend(featured_artworks)
        or "publication-1",
    )
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path, publication_id: _owned_upload(path, publication_id),
    )
    monkeypatch.setattr(main.history_tracker, "start_publication_attempt", lambda *args: 9)
    monkeypatch.setattr(main.history_tracker, "record_publish_response", lambda *args: 9)

    def publish(**kwargs):
        kwargs["before_publish"]("parent-1", tuple(f"child-{index}" for index in range(9)))
        published.append(kwargs)
        return "publication"

    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        publish,
    )
    monkeypatch.setattr(
        main.history_tracker,
        "confirm_carousel_publication",
        lambda cover_id, featured_ids, publication_id: confirmed.append(featured_ids),
    )

    main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert rendered == [
        (artwork["local_image_path"], f"carousel_{position:02d}.jpg")
        for position, artwork in enumerate(final_order, start=1)
    ]
    assert [artwork["id"] for artwork in reserved] == [artwork["id"] for artwork in final_order]
    assert confirmed == [tuple(artwork["id"] for artwork in final_order)]
    caption = published[0]["caption"]
    assert "1. Featured Title 8" in caption
    assert "8. Featured Title 1" in caption


def test_registry_title_and_explicit_query_flow_through_carousel_boundaries(monkeypatch):
    calls = []
    featured, cover = _install_reads_and_selection(monkeypatch, calls)
    observed = {}
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda posted_ids, query, **kwargs: observed.update(search_query=query) or featured,
    )
    monkeypatch.setattr(
        main,
        "select_editorial_cover",
        lambda **kwargs: observed.update(cover_query=kwargs["theme"]) or cover,
    )
    monkeypatch.setattr(
        main.gemini_ai,
        "analyze_carousel",
        lambda theme_title, artworks: observed.update(gemini_title=theme_title) or None,
    )
    monkeypatch.setattr(
        main,
        "format_carousel_caption",
        lambda **kwargs: observed.update(caption_title=kwargs["theme_title"]) or "caption",
    )
    monkeypatch.setattr(
        main,
        "create_carousel_editorial_cover",
        lambda **kwargs: observed.update(cover_title=kwargs["editorial_title"]) or kwargs["output_path"],
    )
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda path, **kwargs: SimpleNamespace(output_path=kwargs["output_path"]),
    )

    main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert observed == {
        "search_query": "winter light painting",
        "cover_query": "winter light painting",
        "gemini_title": "Winter Light",
        "caption_title": "Winter Light",
        "cover_title": "Winter Light",
    }


def test_main_no_longer_owns_a_hardcoded_random_theme_list():
    source = inspect.getsource(main.run_carousel_post)

    assert "random.choice" not in source
    assert "themes =" not in source
    assert "plan_carousel_theme" in source


def test_cover_selection_failure_precedes_gemini_reservation_and_instagram(monkeypatch):
    calls = []
    _install_reads_and_selection(monkeypatch, calls)
    monkeypatch.setattr(
        main,
        "select_editorial_cover",
        lambda **kwargs: (_ for _ in ()).throw(EditorialCoverSelectionError("none")),
    )
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args: calls.append("gemini"))
    monkeypatch.setattr(main.history_tracker, "reserve_carousel", lambda *args, **kwargs: calls.append("reserve"))
    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        lambda **kwargs: calls.append("instagram"),
    )

    with pytest.raises(EditorialCoverSelectionError):
        main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert calls == ["featured_selection"]


def test_featured_render_failure_happens_before_reservation_or_upload(monkeypatch):
    calls = []
    _install_reads_and_selection(monkeypatch, calls)
    _install_copy_and_render(monkeypatch, calls)
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )
    monkeypatch.setattr(main.history_tracker, "reserve_carousel", lambda *args, **kwargs: calls.append("reserve"))
    monkeypatch.setattr(main.image_processor, "upload_temp_media", lambda path: calls.append("upload"))

    with pytest.raises(RuntimeError, match="render failed"):
        main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert "reserve" not in calls
    assert "upload" not in calls


def test_definite_publish_failure_expires_all_nine_ids(monkeypatch):
    calls = []
    featured, cover = _install_reads_and_selection(monkeypatch, calls)
    _install_copy_and_render(monkeypatch, calls)
    publishing = []
    expired = []
    monkeypatch.setattr(main.history_tracker, "reserve_carousel", lambda *args, **kwargs: "publication-1")
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path, publication_id: _owned_upload(path, publication_id),
    )
    monkeypatch.setattr(
        main.history_tracker,
        "start_publication_attempt",
        lambda ids, *args: publishing.extend(ids) or 9,
    )
    monkeypatch.setattr(
        main.history_tracker,
        "mark_publication_not_published",
        lambda ids, *args, **kwargs: expired.extend(ids) or 9,
    )
    cleaned = []
    monkeypatch.setattr(
        main.r2_media,
        "cleanup_publication_media",
        lambda publication_id, **kwargs: cleaned.append(publication_id)
        or r2_media.MediaCleanupSummary(
            publication_id, 9, 9, 0, True, kwargs["reason"]
        ),
    )
    monkeypatch.setattr(
        main.history_tracker,
        "acknowledge_staging_media_cleanup",
        lambda publication_id: True,
    )

    def reject(**kwargs):
        kwargs["before_publish"]("parent-1", tuple(f"child-{index}" for index in range(9)))
        raise instagram_poster.InstagramAPIError("definite")

    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        reject,
    )

    with pytest.raises(instagram_poster.InstagramAPIError):
        main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    expected = [cover.canonical_id, *[art["id"] for art in featured]]
    assert publishing == expected
    assert expired == expected
    assert cleaned == ["publication-1"]
