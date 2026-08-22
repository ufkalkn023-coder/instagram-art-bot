import logging
from types import SimpleNamespace

import pytest

import main
from src import history_tracker


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


def _forbid_mutations(monkeypatch):
    def forbidden(name):
        return lambda *args, **kwargs: pytest.fail(f"dry-run mutation called: {name}")

    monkeypatch.setattr(main.history_tracker, "reserve_artwork", forbidden("history reserve"))
    monkeypatch.setattr(main.history_tracker, "confirm_artwork", forbidden("history confirm"))
    monkeypatch.setattr(main.history_tracker, "mark_artwork_ambiguous", forbidden("history ambiguous"))
    monkeypatch.setattr(main.history_tracker, "mark_artworks_ambiguous", forbidden("history ambiguous"))
    monkeypatch.setattr(main.image_processor, "upload_temp_media", forbidden("R2 media upload"))
    monkeypatch.setattr(main.instagram_poster, "post_to_instagram_graph_api", forbidden("Instagram single publish"))
    monkeypatch.setattr(main.instagram_poster, "post_carousel_to_instagram_graph_api", forbidden("Instagram carousel publish"))
    monkeypatch.setattr(main.pinterest_poster, "post_to_pinterest", forbidden("Pinterest publish"))


def _install_single_read_and_local_pipeline(monkeypatch, artwork, calls):
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: calls.append("history_ids") or set())
    monkeypatch.setattr(
        main.history_tracker,
        "get_grid_color_tone",
        lambda **kwargs: calls.append(("grid_tone", kwargs)) or "warm",
    )
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: calls.append("history_recent") or [])
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda *args, **kwargs: calls.append(("selection", args[3])) or [artwork],
    )
    monkeypatch.setattr(
        main.image_processor,
        "prepare_local_image",
        lambda path: calls.append(("prepare", path)) or ("prepared.jpg", "vertical"),
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
    monkeypatch.setattr(
        main.image_processor,
        "create_feed_post",
        lambda *args, **kwargs: calls.append("process") or "local-single.jpg",
    )


def test_single_dry_run_validates_content_locally_without_external_mutations(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=main.__name__)
    calls = []
    _install_single_read_and_local_pipeline(monkeypatch, _artwork(), calls)
    _forbid_mutations(monkeypatch)

    main.run_single_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=True))

    assert calls == [
        "history_ids",
        ("grid_tone", {"read_only": True}),
        ("selection", "warm"),
        ("prepare", "downloaded.jpg"),
        "history_recent",
        "content_type",
        "gemini",
        "process",
    ]
    assert "DRY RUN SUCCESS mode=single" in caplog.text
    assert "local-single.jpg" in caplog.text


def test_carousel_dry_run_prepares_every_image_without_external_mutations(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=main.__name__)
    artworks = [_artwork("aic_1"), _artwork("met_2")]
    calls = []
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: calls.append("history_ids") or set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: "warm")
    monkeypatch.setattr(main.random, "choice", lambda values: "portrait")
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_carousel_artworks",
        lambda *args, **kwargs: calls.append(("selection", args[1], kwargs["count"])) or artworks,
    )
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args: calls.append("gemini") or None)
    monkeypatch.setattr(
        main.image_processor,
        "prepare_local_image",
        lambda path: calls.append(("prepare", path)) or (path, "vertical"),
    )
    monkeypatch.setattr(
        main.image_processor,
        "create_feed_post",
        lambda raw_path, **kwargs: calls.append(("process", raw_path)) or f"local-{raw_path}",
    )
    _forbid_mutations(monkeypatch)

    main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=True))

    assert calls == [
        "history_ids",
        ("selection", "portrait", 8),
        "gemini",
        ("prepare", "downloaded.jpg"),
        ("process", "downloaded.jpg"),
        ("prepare", "downloaded.jpg"),
        ("process", "downloaded.jpg"),
    ]
    assert "DRY RUN SUCCESS mode=carousel" in caplog.text
    assert "selected_ids=aic_1,met_2" in caplog.text


def test_dry_run_processing_failure_happens_before_every_mutation(monkeypatch):
    calls = []
    _install_single_read_and_local_pipeline(monkeypatch, _artwork(), calls)
    monkeypatch.setattr(
        main.image_processor,
        "create_feed_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("processing failed")),
    )
    _forbid_mutations(monkeypatch)

    with pytest.raises(RuntimeError, match="processing failed"):
        main.run_single_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert "gemini" in calls


def test_single_dry_run_does_not_require_instagram_credentials(monkeypatch):
    calls = []
    _install_single_read_and_local_pipeline(monkeypatch, _artwork(), calls)
    _forbid_mutations(monkeypatch)
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "account\ninvalid")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "secret-token\ninvalid")

    main.run_single_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))


def test_repeated_dry_runs_do_not_change_history_input_or_selection_state(monkeypatch):
    calls = []
    history_ids = {"aic_existing"}
    artwork = _artwork("aic_new")
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: history_ids.copy())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: "warm")
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda posted_ids, *args, **kwargs: calls.append(posted_ids) or [artwork.copy()],
    )
    monkeypatch.setattr(main.image_processor, "prepare_local_image", lambda path: (path, "vertical"))
    monkeypatch.setattr(main.content_diversity, "select_content_type", lambda history: "SINGLE_ARTWORK")
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.image_processor, "create_feed_post", lambda *args, **kwargs: "local.jpg")
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


def test_main_skips_stale_recovery_only_for_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(main.history_tracker, "recover_stale_reservations", lambda: calls.append("recover"))
    monkeypatch.setattr(main, "run_single_post", lambda args: calls.append(("single", args.dry_run)))
    monkeypatch.setattr(main, "run_carousel_post", lambda args: calls.append(("carousel", args.dry_run)))

    monkeypatch.setattr(main.sys, "argv", ["main.py", "--dry-run", "--force-carousel"])
    main.main()
    assert calls == [("carousel", True)]

    calls.clear()
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--force-carousel"])
    main.main()
    assert calls == ["recover", ("carousel", False)]
