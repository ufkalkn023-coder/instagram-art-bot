import logging
from types import SimpleNamespace

import pytest

import main
from src import history_tracker
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_themes import get_default_theme_registry


def _artwork(identifier="aic_1"):
    return {
        "id": identifier,
        "title": "Artwork",
        "artist": "Artist",
        "date": "1900",
        "museum": "Museum",
        "local_image_path": "downloaded.jpg",
        "quality_score": 90.0,
        "selection_score": 92.0,
        "alt_text": "Artwork alt text",
        "medium": "Oil on canvas",
        "classification": "Painting",
    }


def _cover(identifier="rijksmuseum_cover"):
    artwork = _artwork(identifier)
    artwork["local_image_path"] = "cover.jpg"
    breakdown = CoverScoreBreakdown(20, 30, 12, 9, 9, 4, 4)
    return CoverAsset(artwork, "cover.jpg", CoverMode.FULL_ARTWORK, breakdown.total, breakdown)


def _forbid_mutations(monkeypatch):
    def forbidden(name):
        return lambda *args, **kwargs: pytest.fail(f"dry-run mutation called: {name}")

    monkeypatch.setattr(main.history_tracker, "reserve_artwork", forbidden("history reserve"))
    monkeypatch.setattr(main.history_tracker, "reserve_carousel", forbidden("carousel history reserve"))
    monkeypatch.setattr(main.history_tracker, "confirm_artwork", forbidden("history confirm"))
    monkeypatch.setattr(main.history_tracker, "confirm_carousel_publication", forbidden("carousel history confirm"))
    monkeypatch.setattr(main.history_tracker, "mark_artworks_publishing", forbidden("history publishing"))
    monkeypatch.setattr(main.history_tracker, "mark_artworks_pending", forbidden("history rollback"))
    monkeypatch.setattr(main.history_tracker, "mark_artwork_ambiguous", forbidden("history ambiguous"))
    monkeypatch.setattr(main.history_tracker, "mark_artworks_ambiguous", forbidden("history ambiguous"))
    monkeypatch.setattr(main.image_processor, "upload_temp_media", forbidden("R2 media upload"))
    monkeypatch.setattr(main.r2_media, "cleanup_temp_media_upload", forbidden("R2 exact media deletion"))
    monkeypatch.setattr(main.r2_media, "cleanup_publication_media", forbidden("R2 publication deletion"))
    monkeypatch.setattr(main.r2_media, "rollback_temp_media_uploads", forbidden("R2 media rollback"))
    monkeypatch.setattr(main.instagram_poster, "post_to_instagram_graph_api", forbidden("Instagram single publish"))
    monkeypatch.setattr(main.instagram_poster, "post_carousel_to_instagram_graph_api", forbidden("Instagram carousel publish"))


def test_carousel_dry_run_prepares_every_image_without_external_mutations(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=main.__name__)
    artworks = [_artwork(f"aic_{index}") for index in range(1, 9)]
    calls = []
    theme = get_default_theme_registry().by_id("portrait_gaze")
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: calls.append("history_ids") or set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: "warm")
    monkeypatch.setattr(main.history_tracker, "get_recent_carousel_theme_history", lambda: [])
    monkeypatch.setattr(main, "plan_carousel_theme", lambda *args, **kwargs: SimpleNamespace(theme=theme))
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda *args, **kwargs: calls.append(("selection", args[1], kwargs["count"])) or artworks,
    )
    monkeypatch.setattr(
        main,
        "select_editorial_cover",
        lambda **kwargs: calls.append(("cover_selection", kwargs["theme"])) or _cover(),
    )
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args: calls.append("gemini") or None)
    monkeypatch.setattr(
        main,
        "create_carousel_editorial_cover",
        lambda **kwargs: calls.append(("cover_render", kwargs["output_path"])) or kwargs["output_path"],
    )
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda raw_path, **kwargs: calls.append(
            ("process", raw_path, kwargs["output_path"])
        )
        or SimpleNamespace(output_path=kwargs["output_path"]),
    )
    _forbid_mutations(monkeypatch)

    main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=True))

    assert calls == [
        "history_ids",
        ("selection", "portrait gaze", 8),
        ("cover_selection", "portrait gaze"),
        "gemini",
        ("cover_render", main.os.path.join(main.config.DATA_DIR, "carousel_cover.jpg")),
        *[
            item
            for index in range(1, 9)
            for item in (
                (
                    "process",
                    "downloaded.jpg",
                    main.os.path.join(main.config.DATA_DIR, f"carousel_{index:02d}.jpg"),
                ),
            )
        ],
    ]
    assert "DRY RUN SUCCESS mode=carousel" in caplog.text
    assert "cover_id=rijksmuseum_cover" in caplog.text
    assert "featured_ids=aic_1,aic_2,aic_3,aic_4,aic_5,aic_6,aic_7,aic_8" in caplog.text
    artifact_field = caplog.text.split("local_artifacts=", 1)[1].split(" ", 1)[0]
    assert len(set(artifact_field.split(","))) == 9


def test_read_only_grid_tone_never_writes_history(monkeypatch):
    history = {"posted_artworks": [], "active_color_tone": "warm"}
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda *args: pytest.fail("history write"))

    assert history_tracker.get_grid_color_tone(read_only=True) == "warm"
    assert history == {"posted_artworks": [], "active_color_tone": "warm"}


def test_main_skips_publication_reconciliation_only_for_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "validate_production_configuration", lambda: {})
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **kwargs: calls.append("reconcile")
        or SimpleNamespace(
            inspected=0,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=0,
            errors=0,
        ),
    )
    monkeypatch.setattr(main, "run_carousel_post", lambda args: calls.append(("carousel", args.dry_run)))

    monkeypatch.setattr(main.sys, "argv", ["main.py", "--dry-run"])
    main.main()
    assert calls == [("carousel", True)]

    calls.clear()
    monkeypatch.setattr(main.sys, "argv", ["main.py"])
    main.main()
    assert calls == ["reconcile", ("carousel", False)]
