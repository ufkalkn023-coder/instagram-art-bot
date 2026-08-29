import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src import history_tracker, image_processor, instagram_poster
from src.carousel_caption import format_carousel_caption
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_themes import CarouselFormat, get_default_theme_registry, get_format_policy
from src.theme_acquisition import ThemeAvailabilityResult


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "qc_carousels.py"
SPEC = importlib.util.spec_from_file_location("qc_carousels", SCRIPT_PATH)
qc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qc
SPEC.loader.exec_module(qc)


@dataclass(frozen=True)
class _Score:
    theme_id: str
    total: float


def _artwork(index: int, source: Path | None = None) -> dict:
    return {
        "id": f"work_{index}", "title": f"Title {index}", "artist": f"Artist {index}",
        "date": str(1800 + index), "museum": f"Museum {index}", "region": "europe",
        "medium": "Oil on canvas", "quality_score": 90.0, "selection_score": 91.0,
        "theme_relevance_score": 92.0, "local_image_path": str(source or Path(f"raw_{index}.jpg")),
    }


def _availability(theme_id: str) -> ThemeAvailabilityResult:
    return ThemeAvailabilityResult(theme_id, 30, 25, 20, 18, 16, 16, 16, 12, True, None, 2, 8, ())


def _write_image(path: Path) -> None:
    Image.new("RGB", (40, 50), "navy").save(path, "JPEG")


def _fake_plan(theme, bundle: Path, featured_count: int = 8):
    source_paths = [
        bundle / "raw_cover.jpg",
        *[bundle / f"raw_{index}.jpg" for index in range(1, featured_count + 1)],
    ]
    for path in source_paths:
        _write_image(path)
    featured = [
        _artwork(index, source_paths[index])
        for index in range(1, featured_count + 1)
    ]
    cover = SimpleNamespace(
        canonical_id="cover_1", local_image_path=str(source_paths[0]), mode=CoverMode.FULL_ARTWORK,
        cover_score=87.0, score_breakdown={"total": 87.0}, visual_features=None,
    )
    caption = format_carousel_caption(
        theme_title=theme.title,
        editorial_intro="A deterministic editorial introduction.",
        hashtags="#Art #Artfolio",
        featured_artworks=featured,
    )
    return SimpleNamespace(
        theme=theme, theme_id=theme.id, cover=cover, featured_artworks=featured, caption=caption,
        set_optimization=SimpleNamespace(
            finalist_count=8,
            set_score=99.0,
            breakdown={"total": 99.0},
            optimizer_size_decision="test_decision",
            marginal_diagnostics=({"featured_count": featured_count},),
        ),
        sequence=SimpleNamespace(sequence_score=77.0, breakdown={"total": 77.0}),
    )


def _install_main_fakes(monkeypatch, tmp_path, themes, failures=()):
    def by_id(theme_id):
        for theme in themes:
            if theme.id == theme_id:
                return theme
        raise KeyError(theme_id)

    registry = SimpleNamespace(
        by_id=by_id,
        themes=tuple(themes), enabled_themes=tuple(themes),
    )
    scores = tuple(_Score(theme.id, 100 - index) for index, theme in enumerate(themes))
    monkeypatch.setattr(qc, "ROOT", tmp_path)
    monkeypatch.setattr(qc, "get_default_theme_registry", lambda: registry)
    monkeypatch.setattr(qc.history_tracker, "get_recent_carousel_theme_history", lambda: [])
    monkeypatch.setattr(qc, "plan_carousel_theme", lambda *args, **kwargs: SimpleNamespace(ranked_scores=scores))
    monkeypatch.setattr(qc.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(qc.history_tracker, "get_grid_color_tone", lambda **kwargs: "neutral")

    def build(theme, **kwargs):
        if theme.id in failures:
            raise qc.art_fetcher.CarouselSelectionError("unavailable", reason="no_pool")
        return _fake_plan(theme, Path(qc.config.DATA_DIR)), SimpleNamespace(availability=_availability(theme.id)), False

    monkeypatch.setattr(qc, "_build_plan", build)

    def render(plan, bundle, *, color_tone):
        paths = [bundle / "carousel_cover.jpg", *[bundle / f"carousel_{index:02d}.jpg" for index in range(1, 9)]]
        for path in paths:
            _write_image(path)
        presentation = qc.derive_carousel_featured_presentation(
            plan.featured_artworks,
            cover_visual_features=plan.cover.visual_features,
            grid_color_tone=color_tone,
        )
        renders = tuple(
            qc.CarouselFeaturedRenderResult(
                str(path),
                qc.calculate_contain_geometry(40, 50),
            )
            for path in paths[1:]
        )
        return [str(path) for path in paths], presentation, renders

    monkeypatch.setattr(qc, "_render_plan", render)


def test_cli_default_count_and_format_filtering_are_bounded(monkeypatch, tmp_path):
    registry = get_default_theme_registry()
    formats = [CarouselFormat.MONOGRAPHIC, CarouselFormat.MUSEUM_SPOTLIGHT]
    themes = [next(theme for theme in registry.enabled_themes if theme.format is fmt) for fmt in formats]
    _install_main_fakes(monkeypatch, tmp_path, themes)
    assert qc._build_parser().parse_args([]).count == 8
    monkeypatch.setattr(sys, "argv", ["qc", "--format", "MONOGRAPHIC", "--count", "1", "--seed", "same"])
    assert qc.main() == 0
    result = next((tmp_path / "data" / "qc_carousels").glob("*/run_manifest.json"))
    manifest = json.loads(result.read_text())
    assert manifest["results"][0]["theme_id"] == themes[0].id
    assert manifest["qc_network_summary"] == {
        "403_failures": 0,
        "aic_image_requests": {
            "analysis_843": 0,
            "final_1686": 0,
            "fallback_843": 0,
            "rate_limited": 0,
            "recovered": 0,
            "failed": 0,
            "circuit_open": False,
        },
        "active_adapters": [],
        "adapter_calls": 0,
        "adapters_disabled_for_run": [],
        "runtime_disabled_adapters": {},
        "themes_attempted": 0,
        "unavailable_adapters": {},
    }


def test_invalid_and_explicit_theme_handling(monkeypatch, tmp_path):
    theme = get_default_theme_registry().enabled_themes[0]
    _install_main_fakes(monkeypatch, tmp_path, [theme])
    monkeypatch.setattr(sys, "argv", ["qc", "--theme", "not_a_theme"])
    with pytest.raises(SystemExit):
        qc.main()
    monkeypatch.setattr(sys, "argv", ["qc", "--theme", theme.id, "--count", "8"])
    assert qc.main() == 0
    run = next((tmp_path / "data" / "qc_carousels").glob("*/run_manifest.json"))
    assert len(json.loads(run.read_text())["results"]) == 1


def test_explicit_unavailable_theme_does_not_fallback(monkeypatch, tmp_path):
    registry = get_default_theme_registry()
    themes = list(registry.enabled_themes[:2])
    _install_main_fakes(monkeypatch, tmp_path, themes, failures={themes[0].id})
    monkeypatch.setattr(sys, "argv", ["qc", "--theme", themes[0].id])
    assert qc.main() == 1
    run = next((tmp_path / "data" / "qc_carousels").glob("*/run_manifest.json"))
    assert json.loads(run.read_text())["results"] == [{"reason": "no_pool", "status": "unavailable", "theme_id": themes[0].id}]


def test_explicit_theme_bypasses_planner(monkeypatch, tmp_path):
    theme = get_default_theme_registry().enabled_themes[0]
    _install_main_fakes(monkeypatch, tmp_path, [theme])
    monkeypatch.setattr(
        qc,
        "plan_carousel_theme",
        lambda *args, **kwargs: pytest.fail("planner must not run for an exact theme"),
    )
    monkeypatch.setattr(sys, "argv", ["qc", "--theme", theme.id])

    assert qc.main() == 0


def test_explicit_theme_fallback_attempts_requested_theme_first(monkeypatch, tmp_path):
    registry = get_default_theme_registry()
    themes = list(registry.enabled_themes[:2])
    _install_main_fakes(monkeypatch, tmp_path, themes, failures={themes[0].id})
    attempts = []
    original_build = qc._build_plan

    def record_build(theme, **kwargs):
        attempts.append(theme.id)
        return original_build(theme, **kwargs)

    monkeypatch.setattr(qc, "_build_plan", record_build)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qc", "--theme", themes[0].id, "--allow-fallback"],
    )

    assert qc.main() == 0
    assert attempts[:2] == [themes[0].id, themes[1].id]


def test_sampling_is_deterministic_for_the_same_seed(monkeypatch, tmp_path):
    registry = get_default_theme_registry()
    themes = [
        next(theme for theme in registry.enabled_themes if theme.format is carousel_format)
        for carousel_format in list(CarouselFormat)[:3]
    ]
    _install_main_fakes(monkeypatch, tmp_path, themes)
    monkeypatch.setattr(sys, "argv", ["qc", "--count", "2", "--seed", "repeatable"])
    assert qc.main() == 0
    assert qc.main() == 0
    manifests = sorted((tmp_path / "data" / "qc_carousels").glob("*/run_manifest.json"))
    assert len(manifests) == 2
    assert [item["theme_id"] for item in json.loads(manifests[0].read_text())["results"]] == [
        item["theme_id"] for item in json.loads(manifests[1].read_text())["results"]
    ]


def test_no_gemini_uses_fallback_without_gemini_call(monkeypatch, tmp_path):
    theme = get_default_theme_registry().enabled_themes[0]
    artworks = [_artwork(index) for index in range(1, 9)]
    cover_artwork = _artwork(99)
    cover_artwork["id"] = "cover"
    breakdown = CoverScoreBreakdown(1, 2, 3, 4, 5, 6, 7)
    cover = CoverAsset(cover_artwork, "cover.jpg", CoverMode.FULL_ARTWORK, breakdown.total, breakdown)
    selection = SimpleNamespace(artworks=tuple(artworks), acquisition=SimpleNamespace(availability=_availability(theme.id)), set_optimization=None)
    monkeypatch.setattr(qc.art_fetcher, "fetch_themed_artworks", lambda *args, **kwargs: selection)
    monkeypatch.setattr(qc, "select_editorial_cover", lambda **kwargs: cover)
    monkeypatch.setattr(qc, "sequence_carousel_artworks", lambda artworks, **kwargs: SimpleNamespace(ordered_artworks=tuple(artworks)))
    monkeypatch.setattr(qc.gemini_ai, "analyze_carousel", lambda *args, **kwargs: pytest.fail("Gemini called"))
    plan, _, gemini_used = qc._build_plan(theme, posted_ids=set(), color_tone="neutral", seed=qc.art_fetcher.SelectionRunSeed("fixed", "test"), use_gemini=False)
    assert not gemini_used
    assert plan.caption.startswith(f"{theme.title}\n\n")
    assert plan.caption.count("\nFeatured Works\n\n") == 1


def test_bundle_artifacts_caption_order_and_publish_isolation(monkeypatch, tmp_path):
    theme = get_default_theme_registry().enabled_themes[0]
    _install_main_fakes(monkeypatch, tmp_path, [theme])
    forbidden = [
        "reserve_artwork", "reserve_carousel", "mark_artworks_publishing", "mark_artworks_pending",
        "mark_artworks_ambiguous", "confirm_artwork", "confirm_carousel_publication",
    ]
    for name in forbidden:
        monkeypatch.setattr(history_tracker, name, lambda *args, _name=name, **kwargs: pytest.fail(f"history mutation: {_name}"))
    monkeypatch.setattr(image_processor, "upload_temp_media", lambda *args, **kwargs: pytest.fail("R2 upload"))
    monkeypatch.setattr( instagram_poster, "post_to_instagram_graph_api", lambda *args, **kwargs: pytest.fail("Instagram publish"))
    monkeypatch.setattr(instagram_poster, "post_carousel_to_instagram_graph_api", lambda *args, **kwargs: pytest.fail("Instagram carousel publish"))
    monkeypatch.setattr(qc.gemini_ai, "analyze_carousel", lambda *args, **kwargs: pytest.fail("Gemini called"))
    monkeypatch.setattr(sys, "argv", ["qc", "--theme", theme.id, "--no-gemini", "--seed", "fixed"])
    assert qc.main() == 0
    bundle = next((tmp_path / "data" / "qc_carousels").glob("*/01_*"))
    required = {"carousel_cover.jpg", *{f"carousel_{index:02d}.jpg" for index in range(1, 9)}, "caption.txt", "manifest.json", "selection_report.txt", "contact_sheet.jpg", "index.html"}
    assert required <= {path.name for path in bundle.iterdir()}
    manifest = json.loads((bundle / "manifest.json").read_text())
    caption = (bundle / "caption.txt").read_text().strip()
    assert manifest["gemini_used"] is False
    assert [work["canonical_id"] for work in manifest["featured_artworks"]] == [f"work_{index}" for index in range(1, 9)]
    assert "cover_1" not in caption
    assert [caption.index(f"Title {index} — Artist {index}") for index in range(1, 9)] == sorted(caption.index(f"Title {index} — Artist {index}") for index in range(1, 9))
    assert "Museum 8" in caption
    assert (bundle.parent / "index.html").exists()


@pytest.mark.parametrize("featured_count", [5, 6, 8])
def test_qc_manifest_and_contact_sheet_use_actual_adaptive_slide_count(
    tmp_path, featured_count
):
    theme = get_default_theme_registry().enabled_themes[0]
    plan = _fake_plan(theme, tmp_path, featured_count)
    paths = [
        tmp_path / "carousel_cover.jpg",
        *[
            tmp_path / f"carousel_{index:02d}.jpg"
            for index in range(1, featured_count + 1)
        ],
    ]
    for path in paths:
        _write_image(path)
    acquisition = SimpleNamespace(availability=_availability(theme.id))

    manifest = qc._manifest(
        plan,
        acquisition,
        _Score(theme.id, 99.0),
        1,
        [str(path) for path in paths],
        gemini_used=False,
    )
    qc._contact_sheet([str(path) for path in paths], tmp_path / "contact_sheet.jpg")

    assert manifest["featured_count"] == featured_count
    assert manifest["total_slide_count"] == featured_count + 1
    assert manifest["editorial_facts"]["featured_count"] == featured_count
    assert manifest["editorial_facts"]["total_slide_count"] == featured_count + 1
    assert manifest["cover_title"] == theme.title
    assert "cover_subtitle" in manifest
    assert "cover_microfacts" in manifest
    assert "caption_intro" in manifest
    assert manifest["featured_render_mode"] == "CAROUSEL_GALLERY_FIELD"
    assert manifest["canvas_width"] == 1080
    assert manifest["canvas_height"] == 1350
    assert manifest["field_policy"] == "NEUTRAL_LIGHT"
    assert manifest["field_family"] == "NEUTRAL_LIGHT"
    assert all(
        {
            "source_width",
            "source_height",
            "source_aspect_ratio",
            "rendered_artwork_x",
            "rendered_artwork_y",
            "rendered_artwork_width",
            "rendered_artwork_height",
            "rendered_artwork_aspect_ratio",
        }
        <= work.keys()
        for work in manifest["featured_artworks"]
    )
    report = qc._selection_report(manifest)
    assert "EDITORIAL COPY" in report
    assert f"Cover title: {theme.title}" in report
    assert "FEATURED PRESENTATION\nMode: Gallery Field" in report
    assert "Canvas: 1080x1350\nField: NEUTRAL_LIGHT" in report
    assert len(manifest["output_paths"]) == featured_count + 1
    assert manifest["optimizer_size_decision"] == "test_decision"
    with Image.open(tmp_path / "contact_sheet.jpg") as contact_sheet:
        assert contact_sheet.height == 372 * ((featured_count + 3) // 3)


def test_qc_manifest_rejects_out_of_contract_slide_count(tmp_path):
    theme = get_default_theme_registry().enabled_themes[0]
    plan = _fake_plan(theme, tmp_path, featured_count=2)
    acquisition = SimpleNamespace(availability=_availability(theme.id))

    with pytest.raises(ValueError, match="outside the 5–8"):
        qc._manifest(
            plan,
            acquisition,
            _Score(theme.id, 99.0),
            1,
            ["carousel_cover.jpg", "carousel_01.jpg", "carousel_02.jpg"],
            gemini_used=False,
        )


def test_failed_batch_item_continues_and_zero_successes_fail(monkeypatch, tmp_path):
    registry = get_default_theme_registry()
    format_with_two = next(
        carousel_format
        for carousel_format in CarouselFormat
        if len([theme for theme in registry.enabled_themes if theme.format is carousel_format]) >= 2
    )
    themes = [theme for theme in registry.enabled_themes if theme.format is format_with_two][:2]
    _install_main_fakes(monkeypatch, tmp_path, themes, failures={themes[0].id})
    monkeypatch.setattr(sys, "argv", ["qc", "--format", format_with_two.value, "--count", "2"])
    assert qc.main() == 0
    run = next((tmp_path / "data" / "qc_carousels").glob("*/run_manifest.json"))
    assert any(item["status"] == "generated" for item in json.loads(run.read_text())["results"])
    _install_main_fakes(monkeypatch, tmp_path, [themes[0]], failures={themes[0].id})
    monkeypatch.setattr(sys, "argv", ["qc", "--theme", themes[0].id])
    assert qc.main() == 1


def test_format_policy_exemptions_remain_explicit():
    assert not get_format_policy(CarouselFormat.MONOGRAPHIC).penalize_artist_similarity
    assert not get_format_policy(CarouselFormat.MUSEUM_SPOTLIGHT).penalize_museum_similarity
    assert not get_format_policy(CarouselFormat.REGIONAL).penalize_region_similarity
    assert not get_format_policy(CarouselFormat.PERIOD_FOCUS).penalize_period_similarity
    assert not get_format_policy(CarouselFormat.MEDIUM_FOCUS).penalize_medium_similarity
    assert not get_format_policy(CarouselFormat.COLOR_STUDY).penalize_color_similarity
    assert not get_format_policy(CarouselFormat.LIGHT_STUDY).penalize_luminance_similarity
