import logging
from types import SimpleNamespace

import pytest

import main
from src import history_tracker
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_themes import get_default_theme_registry
from src.instagram_image import (
    InstagramImagePublishability,
    InstagramImagePublishabilityReason,
    PreparedSingleImage,
    SingleImageProcessing,
)


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


def _prepared_single(path="local-single.jpg"):
    result = InstagramImagePublishability(
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
    return PreparedSingleImage(
        path=path,
        source=result,
        publishability=result,
        processing=SingleImageProcessing.ZERO_TOUCH,
        source_bytes_preserved=True,
        compatibility_conversion=False,
    )


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
    monkeypatch.setattr(main.instagram_poster, "post_to_instagram_graph_api", forbidden("Instagram single publish"))
    monkeypatch.setattr(main.instagram_poster, "post_carousel_to_instagram_graph_api", forbidden("Instagram carousel publish"))
    monkeypatch.setattr(main.pinterest_poster, "post_to_pinterest", forbidden("Pinterest publish"))


def _install_single_read_and_local_pipeline(monkeypatch, artwork, calls):
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: calls.append("history_ids") or set())
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: calls.append("history_recent") or [])
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda *args, **kwargs: calls.append(
            ("selection", kwargs["max_candidates"])
        )
        or iter([artwork]),
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda path, output_path: calls.append(("prepare_single", path, output_path))
        or _prepared_single(),
    )
    monkeypatch.setattr(
        main.content_diversity,
        "select_content_type",
        lambda history: calls.append("content_type") or "SINGLE_ARTWORK",
    )
    monkeypatch.setattr(
        main.gemini_ai,
        "analyze_artwork",
        lambda *args, **kwargs: calls.append("gemini") or None,
    )


def test_single_dry_run_validates_content_locally_without_external_mutations(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=main.__name__)
    calls = []
    _install_single_read_and_local_pipeline(monkeypatch, _artwork(), calls)
    _forbid_mutations(monkeypatch)

    main.run_single_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=True))

    assert calls == [
        "history_ids",
        ("selection", main.SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT),
        ("prepare_single", "downloaded.jpg", main.config.OUTPUT_IMAGE_PATH),
        "history_recent",
        "content_type",
        "gemini",
    ]
    assert "DRY RUN SUCCESS mode=single" in caplog.text
    assert "local-single.jpg" in caplog.text
    assert "single_image_publishability canonical_id=aic_1" in caplog.text
    assert "result=SUPPORTED_AS_IS processing=ZERO_TOUCH" in caplog.text


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


def test_dry_run_processing_failure_happens_before_every_mutation(monkeypatch):
    calls = []
    _install_single_read_and_local_pipeline(monkeypatch, _artwork(), calls)
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("processing failed")),
    )
    _forbid_mutations(monkeypatch)

    with pytest.raises(RuntimeError, match="processing failed"):
        main.run_single_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert "gemini" not in calls


def test_nonpublishable_single_image_returns_typed_result_before_reservation(monkeypatch):
    calls = []
    _install_single_read_and_local_pipeline(monkeypatch, _artwork(), calls)
    result = InstagramImagePublishability(
        publishable=False,
        reason=InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE,
        width=200,
        height=100,
        aspect_ratio=2.0,
        image_format="JPEG",
        file_size=1234,
        exif_orientation=1,
        encoded_width=200,
        encoded_height=100,
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda *args: PreparedSingleImage(
            path=None,
            source=result,
            publishability=result,
            processing=SingleImageProcessing.NONE,
            source_bytes_preserved=False,
            compatibility_conversion=False,
        ),
    )
    _forbid_mutations(monkeypatch)

    resolution = main.run_single_post(
        SimpleNamespace(dry_run=False, image_url=None, pinterest=False)
    )

    assert (
        resolution.result
        is main.SinglePostResolutionCode.NO_SINGLE_POST_PUBLISHABLE_CANDIDATE
    )
    assert resolution.single_ineligible == 1
    assert resolution.diagnostics[0].reason is InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE
    assert "gemini" not in calls


def test_repeated_dry_runs_do_not_change_history_input_or_selection_state(monkeypatch):
    calls = []
    history_ids = {"aic_existing"}
    artwork = _artwork("aic_new")
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: history_ids.copy())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: "warm")
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda posted_ids, **kwargs: calls.append(posted_ids) or iter([artwork.copy()]),
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda path, output_path: _prepared_single(path),
    )
    monkeypatch.setattr(main.content_diversity, "select_content_type", lambda history: "SINGLE_ARTWORK")
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda *args, **kwargs: None)
    _forbid_mutations(monkeypatch)

    args = SimpleNamespace(dry_run=True, image_url=None, pinterest=False)
    main.run_single_post(args)
    main.run_single_post(args)

    assert calls == [history_ids, history_ids]
    assert history_ids == {"aic_existing"}


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
    monkeypatch.setattr(main, "run_single_post", lambda args: calls.append(("single", args.dry_run)))
    monkeypatch.setattr(main, "run_carousel_post", lambda args: calls.append(("carousel", args.dry_run)))

    monkeypatch.setattr(main.sys, "argv", ["main.py", "--dry-run", "--force-carousel"])
    main.main()
    assert calls == [("carousel", True)]

    calls.clear()
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--force-carousel"])
    main.main()
    assert calls == ["reconcile", ("carousel", False)]
