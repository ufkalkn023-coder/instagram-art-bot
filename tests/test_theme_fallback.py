from types import SimpleNamespace

import pytest

import main
from src import art_fetcher, r2_media
from src.art_fetcher import SelectionRunSeed
from src.carousel_cover import EditorialCoverSelectionError
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_themes import get_default_theme_registry


def _artworks(prefix):
    return [
        {
            "id": f"aic_{prefix}_{index}",
            "title": f"Artwork {index}",
            "artist": f"Artist {index}",
            "date": "1880",
            "museum": f"Museum {index % 4}",
            "local_image_path": f"raw-{prefix}-{index}.jpg",
            "quality_score": 90.0,
            "selection_score": 90.0,
        }
        for index in range(8)
    ]


def _cover(identifier="met_cover"):
    artwork = {
        "id": identifier,
        "title": "Cover",
        "artist": "Cover Artist",
        "date": "1880",
        "museum": "Cover Museum",
        "local_image_path": "raw-cover.jpg",
    }
    breakdown = CoverScoreBreakdown(20, 30, 12, 9, 9, 4, 4)
    return CoverAsset(artwork, "raw-cover.jpg", CoverMode.FULL_ARTWORK, breakdown.total, breakdown)


def _owned_upload(path, publication_id):
    return r2_media.TempMediaUpload(
        f"images/publications/{publication_id}/"
        "20260826120000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        f"https://media/{path}",
        publication_id,
    )


class PlannerOrder:
    def __init__(self, themes):
        self.theme = themes[0]
        self._themes = tuple(themes)

    def ranked_themes(self, registry):
        return self._themes


def _install_common(monkeypatch, themes, calls):
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(main.history_tracker, "get_grid_color_tone", lambda **kwargs: "cool")
    monkeypatch.setattr(main.history_tracker, "get_recent_carousel_theme_history", lambda: [])
    monkeypatch.setattr(main, "plan_carousel_theme", lambda *args, **kwargs: PlannerOrder(themes))
    monkeypatch.setattr(
        main.art_fetcher,
        "resolve_selection_run_seed",
        lambda: SelectionRunSeed("fixed", "test"),
    )
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args: None)
    monkeypatch.setattr(
        main,
        "create_carousel_editorial_cover",
        lambda **kwargs: kwargs["output_path"],
    )
    monkeypatch.setattr(
        main,
        "render_carousel_featured_artwork",
        lambda path, **kwargs: SimpleNamespace(output_path=kwargs["output_path"]),
    )
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_carousel",
        lambda *args, **kwargs: calls.append("reserve"),
    )


def test_unavailable_first_theme_falls_back_in_planner_order_before_mutation(monkeypatch, caplog):
    caplog.set_level("INFO", logger=main.__name__)
    registry = get_default_theme_registry()
    first = registry.by_id("women_reading")
    second = registry.by_id("winter_light")
    calls = []
    _install_common(monkeypatch, [first, second], calls)

    def select(posted_ids, query, **kwargs):
        calls.append(("selection", kwargs["theme_definition"].id))
        if kwargs["theme_definition"].id == first.id:
            raise art_fetcher.CarouselSelectionError(
                "insufficient relevance",
                reason="insufficient_relevance_pool",
            )
        return _artworks("fallback")

    monkeypatch.setattr(main.art_fetcher, "fetch_themed_artworks", select)
    monkeypatch.setattr(
        main,
        "select_editorial_cover",
        lambda **kwargs: calls.append(("cover", kwargs["theme_definition"].id)) or _cover(),
    )

    main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert calls[:3] == [
        ("selection", first.id),
        ("selection", second.id),
        ("cover", second.id),
    ]
    assert "reserve" not in calls
    assert f"theme_fallback from={first.id} to={second.id} attempt=2" in caplog.text
    assert f"carousel_theme_selected theme={second.id}" in caplog.text


def test_cover_unavailability_causes_safe_theme_fallback(monkeypatch):
    registry = get_default_theme_registry()
    first = registry.by_id("women_reading")
    second = registry.by_id("winter_light")
    calls = []
    _install_common(monkeypatch, [first, second], calls)
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda posted_ids, query, **kwargs: calls.append(("selection", kwargs["theme_definition"].id))
        or _artworks(kwargs["theme_definition"].id),
    )

    def cover(**kwargs):
        theme_id = kwargs["theme_definition"].id
        calls.append(("cover", theme_id))
        if theme_id == first.id:
            raise EditorialCoverSelectionError("none", reason="cover_unavailable")
        return _cover()

    monkeypatch.setattr(main, "select_editorial_cover", cover)

    main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert calls[:4] == [
        ("selection", first.id),
        ("cover", first.id),
        ("selection", second.id),
        ("cover", second.id),
    ]
    assert "reserve" not in calls


def test_first_viable_ranked_theme_stops_without_fallback(monkeypatch):
    registry = get_default_theme_registry()
    themes = [registry.by_id("women_reading"), registry.by_id("winter_light")]
    calls = []
    _install_common(monkeypatch, themes, calls)
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda posted_ids, query, **kwargs: calls.append(kwargs["theme_definition"].id)
        or _artworks("first"),
    )
    monkeypatch.setattr(main, "select_editorial_cover", lambda **kwargs: _cover())

    main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))

    assert calls == [themes[0].id]


def test_only_successful_fallback_theme_is_reserved_in_history(monkeypatch):
    registry = get_default_theme_registry()
    first = registry.by_id("women_reading")
    second = registry.by_id("winter_light")
    calls = []
    _install_common(monkeypatch, [first, second], calls)
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda posted_ids, query, **kwargs: _artworks(kwargs["theme_definition"].id),
    )

    def cover(**kwargs):
        if kwargs["theme_definition"].id == first.id:
            raise EditorialCoverSelectionError("none", reason="cover_unavailable")
        return _cover()

    monkeypatch.setattr(main, "select_editorial_cover", cover)
    reservations = []
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_carousel",
        lambda cover_artwork, featured_artworks, **metadata: reservations.append(metadata)
        or "publication-1",
    )
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path, publication_id: _owned_upload(path, publication_id),
    )
    monkeypatch.setattr(main.history_tracker, "start_publication_attempt", lambda *args: None)
    monkeypatch.setattr(main.history_tracker, "record_publish_response", lambda *args: None)

    def publish(**kwargs):
        kwargs["before_publish"]("parent-1", tuple(f"child-{index}" for index in range(9)))
        return "publication"

    monkeypatch.setattr(
        main.instagram_poster,
        "post_carousel_to_instagram_graph_api",
        publish,
    )
    monkeypatch.setattr(main.history_tracker, "confirm_carousel_publication", lambda *args: None)

    main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert reservations == [
        {
            "theme_id": second.id,
            "theme_family": second.family.value,
            "carousel_format": second.format.value,
        }
    ]


def test_fallback_attempts_are_strictly_bounded_and_raise_specific_error(monkeypatch):
    registry = get_default_theme_registry()
    themes = registry.enabled_themes[:8]
    calls = []
    _install_common(monkeypatch, themes, calls)
    monkeypatch.setattr(
        main.art_fetcher,
        "fetch_themed_artworks",
        lambda posted_ids, query, **kwargs: calls.append(kwargs["theme_definition"].id)
        or (_ for _ in ()).throw(
            art_fetcher.CarouselSelectionError("none", reason="insufficient_confirmed_rights")
        ),
    )
    monkeypatch.setattr(main.gemini_ai, "analyze_carousel", lambda *args: calls.append("gemini"))
    monkeypatch.setattr(
        main,
        "select_editorial_cover",
        lambda **kwargs: pytest.fail("cover must not run"),
    )

    with pytest.raises(art_fetcher.CarouselThemeAvailabilityError) as error:
        main.run_carousel_post(SimpleNamespace(dry_run=False, image_url=None, pinterest=False))

    assert calls == [theme.id for theme in themes[:main.CAROUSEL_THEME_ATTEMPT_LIMIT]]
    assert len(error.value.attempts) == main.CAROUSEL_THEME_ATTEMPT_LIMIT
    assert "reserve" not in calls
    assert "gemini" not in calls


def test_same_inputs_produce_the_same_fallback_sequence(monkeypatch):
    registry = get_default_theme_registry()
    themes = [registry.by_id("women_reading"), registry.by_id("winter_light")]
    sequences = []

    for _ in range(2):
        calls = []
        _install_common(monkeypatch, themes, calls)

        def select(posted_ids, query, **kwargs):
            calls.append(kwargs["theme_definition"].id)
            if len(calls) % 2 == 1:
                raise art_fetcher.CarouselSelectionError("none", reason="insufficient_relevance_pool")
            return _artworks("stable")

        monkeypatch.setattr(main.art_fetcher, "fetch_themed_artworks", select)
        monkeypatch.setattr(main, "select_editorial_cover", lambda **kwargs: _cover())
        main.run_carousel_post(SimpleNamespace(dry_run=True, image_url=None, pinterest=False))
        sequences.append([call for call in calls if isinstance(call, str)])

    assert sequences == [[theme.id for theme in themes], [theme.id for theme in themes]]
