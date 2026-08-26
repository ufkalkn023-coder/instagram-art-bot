#!/usr/bin/env python3
# ruff: noqa: E402
"""Generate local, non-publishing review bundles for editorial carousels.

This is deliberately a thin harness around the production selection and rendering
modules.  It does not reserve artwork, upload media, or call either publishing API.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from src import art_fetcher, gemini_ai, history_tracker
from src.carousel_caption import format_carousel_caption
from src.carousel_featured import (
    CarouselFeaturedPresentation,
    CarouselFeaturedRenderResult,
    calculate_contain_geometry,
    derive_carousel_featured_presentation,
    render_carousel_featured_artwork,
)
from src.carousel_cover import (
    EditorialCoverSelectionError,
    create_carousel_editorial_cover,
    select_editorial_cover,
)
from src.carousel_editorial import (
    derive_carousel_editorial_facts,
    derive_cover_micro_facts,
    fallback_carousel_intro,
    fallback_editorial_subtitle,
    grounded_gemini_intro,
)
from src.carousel_plan import CarouselPlan
from src.carousel_policy import (
    MAX_FEATURED_WORKS,
    MAX_TOTAL_SLIDES,
    MIN_FEATURED_WORKS,
    MIN_TOTAL_SLIDES,
)
from src.carousel_sequence import sequence_carousel_artworks
from src.carousel_themes import (
    CarouselFormat,
    CarouselThemeDefinition,
    get_default_theme_registry,
    plan_carousel_theme,
)
from src.format_contracts import target_query_terms
from src.artwork_metadata import period_bucket
from src.theme_acquisition import AcquisitionRunState, ThemeAcquisitionPolicy

logger = logging.getLogger(__name__)
ATTEMPT_LIMIT = 5


def _json_value(value: Any) -> Any:
    """Convert production dataclasses/enums into safe JSON review data."""
    if is_dataclass(value):
        return _json_value(asdict(value))
    if hasattr(value, "value") and type(value).__module__ == "enum":
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug or "carousel"


def _new_run_dir(seed: str) -> Path:
    root = ROOT / "data" / "qc_carousels"
    root.mkdir(parents=True, exist_ok=True)
    base = f"{datetime.now(timezone.utc):%Y-%m-%dT%H%M%S}_{_safe_slug(seed)}"
    candidate = root / base
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base}_{suffix:02d}"
        suffix += 1
    candidate.mkdir()
    return candidate


def _planner_score(selection: Any | None, theme_id: str) -> Any:
    if selection is None:
        return {"theme_id": theme_id, "selection": "explicit_registry"}
    return next(score for score in selection.ranked_scores if score.theme_id == theme_id)


def _planned_queries(theme: CarouselThemeDefinition, query_count: int) -> list[str]:
    policy = ThemeAcquisitionPolicy()
    primary = tuple(dict.fromkeys((*target_query_terms(theme), *theme.primary_queries)))
    plan = [*primary[:policy.max_primary_queries], *theme.secondary_queries[:policy.max_secondary_queries]]
    return list(plan[:query_count])


def _build_plan(
    theme: CarouselThemeDefinition,
    *,
    posted_ids: set[str],
    color_tone: str,
    seed: art_fetcher.SelectionRunSeed,
    use_gemini: bool = True,
    acquisition_run_state: AcquisitionRunState | None = None,
) -> tuple[CarouselPlan, Any, bool]:
    """The production pre-publication carousel path, ending after local rendering inputs."""
    selected = art_fetcher.fetch_themed_artworks(
        posted_ids,
        theme.primary_queries[0],
        count=MAX_FEATURED_WORKS,
        color_tone=color_tone,
        selection_run_seed=seed,
        theme_definition=theme,
        return_acquisition=True,
        acquisition_run_state=acquisition_run_state,
    )
    artworks = list(selected.artworks)
    cover = select_editorial_cover(
        posted_ids=posted_ids,
        featured_artworks=artworks,
        theme=theme.primary_queries[0],
        color_tone=color_tone,
        selection_run_seed=seed,
        theme_definition=theme,
        acquisition=selected.acquisition,
    )
    sequence = sequence_carousel_artworks(
        artworks, theme=theme, cover_visual_features=cover.visual_features
    )
    artworks = list(sequence.ordered_artworks)
    editorial_facts = derive_carousel_editorial_facts(
        artworks,
        theme_id=theme.id,
        theme_title=theme.title,
        carousel_format=theme.format,
    )
    logger.info(
        "carousel_editorial_facts theme=%s featured=%s artists=%s museums=%s date_span=%s",
        editorial_facts.theme_id,
        editorial_facts.featured_count,
        editorial_facts.distinct_artist_count,
        editorial_facts.distinct_museum_count,
        editorial_facts.date_span_label or "omitted",
    )
    analysis = None
    if use_gemini:
        analysis = gemini_ai.analyze_carousel(
            theme.title,
            artworks,
            carousel_format=theme.format.value,
            format_target=(theme.format_target.model_dump(mode="json", exclude_none=True) if theme.format_target else None),
            editorial_facts=editorial_facts.as_dict(),
        )
    target_name = None
    if theme.format_target:
        target_name = theme.format_target.artist_name or theme.format_target.museum_name
    fallback_intro = fallback_carousel_intro(editorial_facts, target_name)
    intro = grounded_gemini_intro(
        (analysis or {}).get("editorial_intro"), editorial_facts, fallback_intro
    )
    subtitle = fallback_editorial_subtitle(editorial_facts)
    caption = format_carousel_caption(
        theme_title=editorial_facts.theme_title,
        editorial_intro=intro,
        hashtags=(analysis or {}).get("hashtags", "#Art #Artfolio #ClassicArt #MuseumArt"),
        featured_artworks=artworks,
    )
    return CarouselPlan.build(
        theme=theme,
        editorial_title=editorial_facts.theme_title,
        editorial_subtitle=subtitle,
        cover_micro_facts=derive_cover_micro_facts(editorial_facts),
        editorial_facts=editorial_facts,
        caption_intro=intro,
        cover=cover,
        featured_artworks=artworks,
        caption=caption,
        set_optimization=selected.set_optimization,
        sequence=sequence,
    ), selected.acquisition, bool(analysis)


def _render_plan(
    plan: CarouselPlan,
    bundle: Path,
    *,
    color_tone: str,
) -> tuple[
    list[str],
    CarouselFeaturedPresentation,
    tuple[CarouselFeaturedRenderResult, ...],
]:
    presentation = derive_carousel_featured_presentation(
        plan.featured_artworks,
        cover_visual_features=plan.cover.visual_features,
        grid_color_tone=color_tone,
    )
    paths = [
        create_carousel_editorial_cover(
            cover=plan.cover,
            editorial_title=plan.editorial_title,
            editorial_subtitle=plan.editorial_subtitle,
            micro_facts=plan.cover_micro_facts,
            output_path=str(bundle / "carousel_cover.jpg"),
        )
    ]
    featured_renders = []
    for position, artwork in enumerate(plan.featured_artworks, 1):
        rendered = render_carousel_featured_artwork(
            artwork["local_image_path"],
            presentation=presentation,
            output_path=str(bundle / f"carousel_{position:02d}.jpg"),
        )
        featured_renders.append(rendered)
        paths.append(rendered.output_path)
    return paths, presentation, tuple(featured_renders)


def _visual_features(artwork: Mapping[str, Any]) -> dict[str, Any]:
    features = artwork.get("visual_features")
    return _json_value(features) if features else {}


def _manifest(
    plan: CarouselPlan,
    acquisition: Any,
    score: Any,
    attempt: int,
    paths: list[str],
    *,
    gemini_used: bool,
    presentation: CarouselFeaturedPresentation | None = None,
    featured_renders: Sequence[CarouselFeaturedRenderResult] | None = None,
) -> dict[str, Any]:
    presentation = presentation or derive_carousel_featured_presentation(
        plan.featured_artworks,
        cover_visual_features=plan.cover.visual_features,
    )
    if featured_renders is None:
        fallback_renders = []
        for artwork in plan.featured_artworks:
            with Image.open(artwork["local_image_path"]) as source:
                display_source = ImageOps.exif_transpose(source)
                geometry = calculate_contain_geometry(
                    display_source.width,
                    display_source.height,
                    canvas_width=presentation.canvas_width,
                    canvas_height=presentation.canvas_height,
                )
            fallback_renders.append(
                CarouselFeaturedRenderResult("", geometry)
            )
        featured_renders = tuple(fallback_renders)
    if len(featured_renders) != len(plan.featured_artworks):
        raise ValueError("QC render geometry must cover every featured artwork")

    featured = []
    for position, (artwork, render_result) in enumerate(
        zip(plan.featured_artworks, featured_renders), 1
    ):
        geometry = render_result.geometry
        featured.append({
            "position": position,
            "canonical_id": artwork["id"], "title": artwork.get("title"),
            "artist": artwork.get("artist"), "date": artwork.get("date"),
            "museum": artwork.get("museum"), "region": artwork.get("region"),
            "medium": artwork.get("medium"),
            "theme_relevance_score": artwork.get("theme_relevance_score"),
            "quality_score": artwork.get("quality_score"),
            "individual_candidate_score": artwork.get("selection_score"),
            "theme_relevance_breakdown": artwork.get("theme_relevance_breakdown"),
            "visual_features": _visual_features(artwork),
            "source_width": geometry.source_width,
            "source_height": geometry.source_height,
            "source_aspect_ratio": geometry.source_aspect_ratio,
            "rendered_artwork_x": geometry.rendered_artwork_x,
            "rendered_artwork_y": geometry.rendered_artwork_y,
            "rendered_artwork_width": geometry.rendered_artwork_width,
            "rendered_artwork_height": geometry.rendered_artwork_height,
            "rendered_artwork_aspect_ratio": geometry.rendered_artwork_aspect_ratio,
        })
    featured_count = len(featured)
    total_slide_count = len(paths)
    if not MIN_FEATURED_WORKS <= featured_count <= MAX_FEATURED_WORKS:
        raise ValueError("QC carousel featured count is outside the 3–8 product contract")
    if (
        not MIN_TOTAL_SLIDES <= total_slide_count <= MAX_TOTAL_SLIDES
        or total_slide_count != featured_count + 1
    ):
        raise ValueError("QC carousel artifacts must contain one cover plus every featured work")
    editorial_facts = getattr(plan, "editorial_facts", None) or derive_carousel_editorial_facts(
        plan.featured_artworks,
        theme_id=plan.theme_id,
        theme_title=plan.theme.title,
        carousel_format=plan.theme.format,
    )
    return _json_value({
        "theme_id": plan.theme_id, "theme_title": plan.theme.title,
        "theme_family": plan.theme.family, "carousel_format": plan.theme.format,
        "planner_score": score, "query_list_actually_used": _planned_queries(plan.theme, acquisition.availability.query_count),
        "fallback_attempt_number": attempt,
        "gemini_used": gemini_used,
        "acquisition": acquisition.availability,
        "validated_finalists": plan.set_optimization.finalist_count if plan.set_optimization else None,
        "cover": {"canonical_id": plan.cover.canonical_id, "mode": plan.cover.mode,
                  "cover_score": plan.cover.cover_score, "score_breakdown": plan.cover.score_breakdown,
                  "visual_features": plan.cover.visual_features},
        "featured_artworks": featured,
        "featured_count": featured_count,
        "total_slide_count": total_slide_count,
        "featured_render_mode": presentation.mode,
        "canvas_width": presentation.canvas_width,
        "canvas_height": presentation.canvas_height,
        "field_policy": presentation.field_policy,
        "field_family": presentation.field_family,
        "editorial_facts": editorial_facts.as_dict(),
        "cover_title": getattr(plan, "editorial_title", editorial_facts.theme_title),
        "cover_subtitle": getattr(plan, "editorial_subtitle", ""),
        "cover_microfacts": list(getattr(plan, "cover_micro_facts", ())),
        "caption_intro": getattr(plan, "caption_intro", ""),
        "set_score": plan.set_optimization.set_score if plan.set_optimization else None,
        "set_score_breakdown": plan.set_optimization.breakdown if plan.set_optimization else None,
        "optimizer_size_decision": (
            getattr(plan.set_optimization, "optimizer_size_decision", None)
            if plan.set_optimization
            else None
        ),
        "marginal_inclusion_diagnostics": (
            getattr(plan.set_optimization, "marginal_diagnostics", ())
            if plan.set_optimization
            else ()
        ),
        "sequence_score": plan.sequence.sequence_score if plan.sequence else None,
        "sequence_score_breakdown": plan.sequence.breakdown if plan.sequence else None,
        "generated_caption": plan.caption,
        "output_paths": [Path(path).name for path in paths],
    })


def _selection_report(manifest: Mapping[str, Any]) -> str:
    works = manifest["featured_artworks"]

    def counts(key: str) -> int:
        return len(
            {
                str(work.get(key, "")).strip()
                for work in works
                if str(work.get(key, "")).strip()
            }
        )

    periods = len({period_bucket(work.get("date")) for work in works if period_bucket(work.get("date"))})
    orientation_count = len({str(work.get("visual_features", {}).get("orientation", "")) for work in works})
    lines = [
        "THEME",
        str(manifest["theme_title"]),
        "",
        "FORMAT",
        str(manifest["carousel_format"]),
        "",
        "EDITORIAL COPY",
        f"Facts: {json.dumps(manifest['editorial_facts'], sort_keys=True)}",
        f"Cover title: {manifest['cover_title']}",
        f"Cover subtitle: {manifest['cover_subtitle']}",
        f"Cover microfacts: {' · '.join(manifest['cover_microfacts'])}",
        f"Caption intro: {manifest['caption_intro']}",
        "",
        "PLANNER",
        json.dumps(manifest["planner_score"], sort_keys=True),
        "",
        "ACQUISITION",
    ]
    acquisition = manifest["acquisition"]
    lines += [
        f"Queries used: {', '.join(manifest['query_list_actually_used'])}",
        f"Raw candidates: {acquisition['raw_candidates']}", f"Unique: {acquisition['unique_candidates']}",
        f"Rights eligible: {acquisition['rights_eligible']}", f"Relevance eligible: {acquisition['relevance_eligible']}",
        f"Validated finalists: {manifest['validated_finalists']}", "", "COVER",
        f"ID: {manifest['cover']['canonical_id']}", f"Mode: {manifest['cover']['mode']}",
        f"Cover score: {manifest['cover']['cover_score']}", "", "FEATURED SET",
    ]
    for work in works:
        lines += [f"{work['position']}. {work['title']} — {work['artist']} ({work['date']})", f"   relevance={work['theme_relevance_score']} quality={work['quality_score']} candidate={work['individual_candidate_score']}"]
    lines += [
        "",
        "FEATURED PRESENTATION",
        "Mode: Gallery Field",
        f"Canvas: {manifest['canvas_width']}x{manifest['canvas_height']}",
        f"Field: {manifest['field_policy']}",
        "",
        "SET SUMMARY",
        f"Featured works: {manifest['featured_count']}",
        f"Total slides: {manifest['total_slide_count']}",
        f"Artists: {counts('artist')}",
        f"Museums: {counts('museum')}",
        f"Regions: {counts('region')}",
        f"Periods: {periods}",
        f"Orientations: {orientation_count}",
        f"Set score: {manifest['set_score']}",
        f"Size decision: {manifest['optimizer_size_decision']}",
        f"Marginal diagnostics: {json.dumps(manifest['marginal_inclusion_diagnostics'], sort_keys=True)}",
        "",
        "SEQUENCE",
        f"Score: {manifest['sequence_score']}",
        json.dumps(manifest['sequence_score_breakdown'], sort_keys=True),
    ]
    return "\n".join(lines) + "\n"


def _contact_sheet(image_paths: Iterable[str], output_path: Path) -> None:
    from PIL import Image, ImageDraw
    paths = list(image_paths)
    thumb = (270, 338)
    row_count = (len(paths) + 2) // 3
    canvas = Image.new("RGB", (thumb[0] * 3, (thumb[1] + 34) * row_count), "white")
    draw = ImageDraw.Draw(canvas)
    for index, image_path in enumerate(paths):
        with Image.open(image_path) as image:
            image.thumbnail(thumb)
            x, y = (index % 3) * thumb[0], (index // 3) * (thumb[1] + 34)
            canvas.paste(image, (x + (thumb[0] - image.width) // 2, y))
            draw.text((x + 8, y + thumb[1] + 8), "Cover" if index == 0 else f"Featured {index}", fill="black")
    canvas.save(output_path, "JPEG", quality=92)


def _write_index(run_dir: Path, bundles: list[dict[str, Any]]) -> None:
    rows = "\n".join(f'<li><a href="{item["dir"]}/">{item["label"]}</a></li>' for item in bundles)
    (run_dir / "index.html").write_text(f"<!doctype html><title>Carousel QC</title><h1>Carousel QC review</h1><ul>{rows}</ul>", encoding="utf-8")
    for item in bundles:
        bundle = run_dir / item["dir"]
        images = item["images"]
        tags = "\n".join(f'<figure><img src="{path}" loading="lazy"><figcaption>{path}</figcaption></figure>' for path in images)
        (bundle / "index.html").write_text(f"<!doctype html><title>{item['label']}</title><style>img{{max-width:320px}}figure{{display:inline-block;vertical-align:top}}</style><h1>{item['label']}</h1>{tags}<p><a href=\"selection_report.txt\">Selection report</a> · <a href=\"manifest.json\">Manifest</a> · <a href=\"caption.txt\">Caption</a></p>", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate local, non-publishing carousel QC bundles.")
    parser.add_argument("--count", type=int, default=8, help="Number of representative themes to attempt (default: 8).")
    parser.add_argument("--theme", help="Generate QC for one exact enabled theme ID.")
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="After an explicit theme fails, try deterministic planner fallbacks.",
    )
    parser.add_argument("--format", choices=[item.value for item in CarouselFormat], help="Restrict sampling to one carousel format.")
    parser.add_argument("--seed", default=datetime.now(timezone.utc).strftime("qc-%Y-%m-%d"), help="Deterministic selection seed.")
    parser.add_argument("--no-gemini", action="store_true", help="Use deterministic local editorial copy without calling Gemini.")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")

    registry = get_default_theme_registry()
    history = history_tracker.get_recent_carousel_theme_history()
    planner = None
    ranked: list[CarouselThemeDefinition] = []
    if args.theme:
        try:
            requested = registry.by_id(args.theme)
        except KeyError:
            parser.error(f"unknown theme: {args.theme}")
        if not requested.enabled:
            parser.error(f"theme is disabled: {args.theme}")
        if args.format and requested.format.value != args.format:
            parser.error("--theme and --format do not match")
        if args.allow_fallback:
            planner = plan_carousel_theme(
                registry,
                history,
                run_seed=args.seed,
                current_month=datetime.now(timezone.utc).month,
            )
            ranked = [registry.by_id(score.theme_id) for score in planner.ranked_scores]
        themes = [requested]
    else:
        if args.allow_fallback:
            parser.error("--allow-fallback requires --theme")
        planner = plan_carousel_theme(
            registry,
            history,
            run_seed=args.seed,
            current_month=datetime.now(timezone.utc).month,
        )
        ranked = [registry.by_id(score.theme_id) for score in planner.ranked_scores]
        if args.format:
            themes = [theme for theme in ranked if theme.format.value == args.format][:args.count]
        else:
            # First pass intentionally spreads review work across formats; the ranked
            # order remains the deterministic tie-breaker within every format.
            by_format: dict[CarouselFormat, list[CarouselThemeDefinition]] = {fmt: [] for fmt in CarouselFormat}
            for theme in ranked:
                by_format[theme.format].append(theme)
            formats = [fmt for fmt in CarouselFormat if by_format[fmt]]
            if args.count < len(formats):
                formats.sort(key=lambda fmt: (art_fetcher.derive_selection_rng(args.seed, f"qc-format:{fmt.value}").random(), fmt.value))
                formats = formats[:args.count]
            themes = [by_format[fmt][0] for fmt in formats]
            if len(themes) < args.count:
                themes.extend(theme for theme in ranked if theme not in themes)
                themes = themes[:args.count]

    run_dir = _new_run_dir(args.seed)
    original_data_dir = config.DATA_DIR
    seed = art_fetcher.SelectionRunSeed(args.seed, "qc")
    posted_ids = history_tracker.get_posted_ids()  # read-only: preserves production duplicate protection.
    color_tone = history_tracker.get_grid_color_tone(read_only=True)
    results: list[dict[str, Any]] = []
    bundles: list[dict[str, str]] = []
    acquisition_run_state = AcquisitionRunState()
    try:
        completed_theme_ids: set[str] = set()
        for ordinal, theme in enumerate(themes, 1):
            # A failed automatic sample follows the production planner's bounded,
            # deterministic availability fallback. Explicit themes remain exact.
            if args.theme and not args.allow_fallback:
                candidates = [theme]
            else:
                fallback_pool = ranked if not args.format else [item for item in ranked if item.format.value == args.format]
                candidates = []
                seen_ids: set[str] = set()
                for item in [theme, *fallback_pool]:
                    if item.id not in completed_theme_ids and item.id not in seen_ids:
                        candidates.append(item)
                        seen_ids.add(item.id)
            bundle = run_dir / f"{ordinal:02d}_pending"
            bundle.mkdir()
            # Production download helpers write to config.DATA_DIR. Point it at this
            # disposable bundle so QC never overwrites normal local render artifacts.
            config.DATA_DIR = str(bundle)
            try:
                plan = acquisition = None
                gemini_used = False
                last_error: Exception | None = None
                attempt = 0
                for attempt, candidate_theme in enumerate(candidates[:ATTEMPT_LIMIT], 1):
                    try:
                        plan, acquisition, gemini_used = _build_plan(
                            candidate_theme,
                            posted_ids=posted_ids,
                            color_tone=color_tone,
                            seed=seed,
                            use_gemini=not args.no_gemini,
                            acquisition_run_state=acquisition_run_state,
                        )
                        break
                    except (art_fetcher.CarouselSelectionError, EditorialCoverSelectionError) as error:
                        last_error = error
                        logger.info("QC fallback theme=%s attempt=%s reason=%s", candidate_theme.id, attempt, getattr(error, "reason", type(error).__name__))
                if plan is None or acquisition is None:
                    raise last_error or RuntimeError("no QC theme candidate was attempted")
                score = _planner_score(planner, plan.theme_id)
                paths, presentation, featured_renders = _render_plan(
                    plan,
                    bundle,
                    color_tone=color_tone,
                )
                manifest = _manifest(
                    plan,
                    acquisition,
                    score,
                    attempt,
                    paths,
                    gemini_used=gemini_used,
                    presentation=presentation,
                    featured_renders=featured_renders,
                )
                (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                (bundle / "caption.txt").write_text(plan.caption + "\n", encoding="utf-8")
                (bundle / "selection_report.txt").write_text(_selection_report(manifest), encoding="utf-8")
                _contact_sheet(paths, bundle / "contact_sheet.jpg")
                # Raw, validated sources are only pipeline scratch files. The review
                # bundle intentionally contains the rendered slides and review data.
                for source in [plan.cover.local_image_path, *(str(artwork["local_image_path"]) for artwork in plan.featured_artworks)]:
                    Path(source).unlink(missing_ok=True)
                final_bundle = run_dir / f"{ordinal:02d}_{_safe_slug(plan.theme_id)}"
                bundle.rename(final_bundle)
                results.append({"theme_id": plan.theme_id, "status": "generated", "bundle": final_bundle.name, "fallback_attempt_number": attempt})
                bundles.append({
                    "dir": final_bundle.name,
                    "label": f"{plan.theme.title} — {plan.theme.format.value}",
                    "images": [Path(path).name for path in paths],
                })
                completed_theme_ids.add(plan.theme_id)
            except (art_fetcher.CarouselSelectionError, EditorialCoverSelectionError) as error:
                results.append({"theme_id": theme.id, "status": "unavailable", "reason": getattr(error, "reason", type(error).__name__)})
                logger.warning("QC theme unavailable: %s (%s)", theme.id, error)
    finally:
        config.DATA_DIR = original_data_dir
    network_summary = acquisition_run_state.diagnostics()
    logger.info(
        "qc_network_summary themes_attempted=%s adapter_calls=%s "
        "adapters_disabled_for_run=%s 403_failures=%s",
        network_summary["themes_attempted"],
        network_summary["adapter_calls"],
        ",".join(network_summary["adapters_disabled_for_run"]) or "none",
        network_summary["403_failures"],
    )
    aic_images = network_summary["aic_image_requests"]
    logger.info(
        "aic_image_requests analysis_843=%s final_1686=%s fallback_843=%s "
        "rate_limited=%s recovered=%s failed=%s circuit_open=%s",
        aic_images["analysis_843"],
        aic_images["final_1686"],
        aic_images["fallback_843"],
        aic_images["rate_limited"],
        aic_images["recovered"],
        aic_images["failed"],
        aic_images["circuit_open"],
    )
    _write_index(run_dir, bundles)
    (run_dir / "run_manifest.json").write_text(json.dumps(_json_value({"seed": args.seed, "generated_at": datetime.now(timezone.utc).isoformat(), "requested_count": args.count, "results": results, "qc_network_summary": network_summary}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(run_dir)
    return 0 if bundles else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(main())
