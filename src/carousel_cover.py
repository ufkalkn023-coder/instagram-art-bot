"""Selection, grounded copy, and rendering for Artfolio editorial covers."""

from __future__ import annotations

import logging
import math
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

import config
from src import content_diversity
from src.art_fetcher import (
    DEFAULT_WEIGHTS,
    SelectionRunSeed,
    derive_selection_rng,
    get_museum_adapters,
    resolve_selection_run_seed,
)
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_editorial import (
    derive_carousel_editorial_facts,
    format_count,
)
from src.artwork_visual_features import extract_visual_features, features_from_dimensions
from src.carousel_themes import CarouselThemeDefinition, ThemeEvidenceMode
from src.quality_filter import (
    calculate_measurement_coverage,
    calculate_quality_score,
    validate_and_download_image_with_metadata,
)
from src.rights_policy import is_rights_eligible
from src.aic_image_policy import ImageDownloadPurpose, is_aic_iiif_url
from src.region import normalize_region
from src.theme_acquisition import ThemeAcquisitionResult, ThemeCandidate

logger = logging.getLogger(__name__)

COVER_WIDTH = 1080
COVER_HEIGHT = 1350
MAX_CANDIDATES_PER_ADAPTER = 24


class EditorialCoverSelectionError(RuntimeError):
    """Raised before reservation when no safe, distinct cover can be selected."""

    def __init__(self, message: str, *, reason: str = "cover_unavailable"):
        self.reason = reason
        super().__init__(message)


def _theme_relevance(candidate, theme: str, stage_index: int) -> float:
    searchable = " ".join(
        str(value or "")
        for value in (
            candidate.title,
            candidate.description,
            candidate.classification,
            candidate.medium,
            candidate.style_or_period,
        )
    ).casefold()
    terms = [term for term in re.findall(r"[a-z0-9]+", theme.casefold()) if len(term) > 2]
    metadata_match = bool(terms) and any(term in searchable for term in terms)
    # Every result was acquired with the theme query; a metadata match is extra evidence.
    return max(10.0, 20.0 - stage_index * 2.0 - (0.0 if metadata_match else 4.0))


def _resolution_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.0
    shortest_side = min(width, height)
    megapixels = width * height / 1_000_000
    return round(min(15.0, shortest_side / 1080 * 8.0 + megapixels * 1.5), 2)


def _crop_retention(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.0
    source_aspect = width / height
    target_aspect = COVER_WIDTH / COVER_HEIGHT
    return min(source_aspect / target_aspect, target_aspect / source_aspect)


def determine_cover_mode(width: int | None, height: int | None) -> CoverMode:
    """Choose a deterministic, center-safe presentation without model crop coordinates."""
    retained = _crop_retention(width, height)
    # Exact/near-exact portrait sources need no editorial detail crop. Moderately
    # different sources can be safely center-cropped; extremes retain the full work.
    if 0.65 <= retained < 0.95:
        return CoverMode.DETAIL_CROP
    return CoverMode.FULL_ARTWORK


def _composition_scores(width: int | None, height: int | None) -> tuple[float, float, float]:
    retained = _crop_retention(width, height)
    if retained <= 0:
        return 0.0, 0.0, 0.0
    source_aspect = width / height
    extreme_penalty = min(1.0, abs(math.log(source_aspect / (COVER_WIDTH / COVER_HEIGHT))) / 2.0)
    composition = 10.0 * (1.0 - 0.55 * extreme_penalty)
    crop_flexibility = 10.0 * retained
    aspect_ratio = 5.0 * (1.0 - extreme_penalty)
    return tuple(round(value, 2) for value in (composition, crop_flexibility, aspect_ratio))


def _visual_readability_score(image_path: str) -> float:
    """Estimate overlay readability cheaply from luminance range and midtone balance."""
    try:
        with Image.open(image_path) as image:
            sample = ImageOps.grayscale(ImageOps.exif_transpose(image))
            sample.thumbnail((160, 160))
            stats = ImageStat.Stat(sample)
            mean = stats.mean[0]
            contrast = stats.stddev[0]
    except Exception:
        return 2.5

    contrast_component = min(1.0, contrast / 64.0)
    midtone_component = 1.0 - min(1.0, abs(mean - 127.5) / 127.5)
    return round(5.0 * (0.7 * contrast_component + 0.3 * midtone_component), 2)


def _extract_cover_visual_features(
    image_path: str, width: int | None, height: int | None
):
    try:
        return extract_visual_features(image_path)
    except (OSError, ValueError):
        return features_from_dimensions(width, height)


def calculate_cover_score(
    candidate,
    *,
    theme: str,
    stage_index: int,
    visual_readability: float = 2.5,
    explicit_theme_relevance: float | None = None,
) -> CoverScoreBreakdown:
    composition, crop_flexibility, aspect_ratio = _composition_scores(
        candidate.image_width, candidate.image_height
    )
    return CoverScoreBreakdown(
        theme_relevance=(
            round(min(100.0, explicit_theme_relevance) * 0.20, 2)
            if explicit_theme_relevance is not None
            else _theme_relevance(candidate, theme, stage_index)
        ),
        technical_quality=round(float(candidate.quality_score or 0.0) * 0.35, 2),
        resolution=_resolution_score(candidate.image_width, candidate.image_height),
        composition_suitability=composition,
        crop_flexibility=crop_flexibility,
        visual_readability=visual_readability,
        aspect_ratio=aspect_ratio,
    )


def _candidate_tiebreak(run_seed: SelectionRunSeed, candidate_id: str) -> float:
    return derive_selection_rng(run_seed.value, f"editorial-cover:{candidate_id}").random()


def _cover_artwork_dict(candidate, local_path: str) -> dict:
    features = content_diversity.get_candidate_metadata_features(candidate)
    return {
        "id": candidate.canonical_id,
        "title": candidate.title,
        "artist": candidate.artist_name,
        "date": candidate.creation_date,
        "museum": candidate.museum_name,
        "image_url": candidate.image_url,
        "local_image_path": local_path,
        "image_width": candidate.image_width,
        "image_height": candidate.image_height,
        "medium": candidate.medium,
        "classification": candidate.classification,
        "quality_score": candidate.quality_score,
        "measurement_coverage": candidate.measurement_coverage,
        "selection_score": candidate.selection_score,
        "visual_category": features.get("visual_category", "other"),
        "period": features.get("period", "unknown"),
        "region": normalize_region(candidate.region),
        "source": candidate.source,
        "artwork_url": candidate.artwork_url,
        "credit_line": candidate.credit_line,
        "license": candidate.license,
        "is_public_domain": candidate.is_public_domain,
        "rights_status": candidate.rights_status,
        "rights_text": candidate.rights_text,
        "copyright_notice": candidate.copyright_notice,
    }


def select_editorial_cover(
    *,
    posted_ids: set[str],
    featured_artworks: Sequence[Mapping[str, object]],
    theme: str,
    color_tone: str,
    selection_run_seed: SelectionRunSeed | None = None,
    theme_definition: CarouselThemeDefinition | None = None,
    acquisition: ThemeAcquisitionResult | None = None,
) -> CoverAsset:
    """Select one distinct, rights-cleared, securely validated thematic cover."""
    run_seed = selection_run_seed or resolve_selection_run_seed()
    if theme_definition is not None and acquisition is not None:
        return _select_editorial_cover_from_acquisition(
            posted_ids=posted_ids,
            featured_artworks=featured_artworks,
            theme=theme_definition,
            acquisition=acquisition,
            run_seed=run_seed,
        )
    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_score = getattr(config, "MIN_QUALITY_SCORE", 50)
    excluded_ids = set(posted_ids)
    excluded_ids.update(str(artwork["id"]) for artwork in featured_artworks)
    candidates_by_id = {}
    attempted_ids: set[str] = set()
    rejection_counts: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    stages = [
        ("tone_and_theme", f"{color_tone} {theme}".strip(), MAX_CANDIDATES_PER_ADAPTER),
        ("theme", theme, MAX_CANDIDATES_PER_ADAPTER * 2),
    ]

    for stage_index, (stage_name, query, limit) in enumerate(stages):
        for adapter in get_museum_adapters():
            try:
                adapter_rng = derive_selection_rng(
                    run_seed.value,
                    f"museum:{adapter.source_id}:stage:cover_{stage_name}:query:{query}",
                )
                fetched = adapter.fetch_candidates(limit=limit, query=query, rng=adapter_rng)
            except Exception as exc:
                reject("adapter_failure")
                logger.warning(
                    "cover_adapter_failure source=%s stage=%s error=%s",
                    adapter.source_id,
                    stage_name,
                    type(exc).__name__,
                )
                continue

            for candidate in fetched:
                candidate_id = candidate.canonical_id
                if candidate_id in excluded_ids:
                    reject("excluded_or_duplicate")
                    continue
                if not is_rights_eligible(candidate):
                    reject("rights_policy")
                    continue
                if not candidate.image_url:
                    reject("missing_image_url")
                    continue
                candidate.quality_score = calculate_quality_score(candidate, museum_weights)
                candidate.measurement_coverage = calculate_measurement_coverage(candidate)
                candidate.selection_score = candidate.quality_score
                if candidate.quality_score < min_score:
                    reject("pre_quality_below_threshold")
                    continue
                previous = candidates_by_id.get(candidate_id)
                if previous is None or candidate.quality_score > previous[0].quality_score:
                    candidates_by_id[candidate_id] = (candidate, stage_index)

        ranked = sorted(
            (
                (candidate, candidate_stage, calculate_cover_score(candidate, theme=theme, stage_index=candidate_stage))
                for candidate, candidate_stage in candidates_by_id.values()
                if candidate.canonical_id not in attempted_ids
            ),
            key=lambda item: (
                -item[2].total,
                -_candidate_tiebreak(run_seed, item[0].canonical_id),
                item[0].canonical_id,
            ),
        )

        for candidate, candidate_stage, _preliminary in ranked:
            candidate_id = candidate.canonical_id
            attempted_ids.add(candidate_id)
            candidate_path = os.path.join(config.DATA_DIR, f"cover_candidate_{uuid.uuid4().hex}.jpg")
            validation = validate_and_download_image_with_metadata(candidate.image_url, candidate_path)
            if not validation.valid:
                reject("image_validation_failed")
                logger.info(
                    "cover_candidate_rejected id=%s reason=image_validation_failed validation_reason=%s",
                    candidate_id,
                    validation.reason,
                )
                continue

            candidate.image_width = validation.width
            candidate.image_height = validation.height
            candidate.quality_score = calculate_quality_score(candidate, museum_weights)
            candidate.measurement_coverage = calculate_measurement_coverage(candidate)
            candidate.selection_score = candidate.quality_score
            if candidate.quality_score < min_score:
                reject("post_quality_below_threshold")
                try:
                    os.remove(candidate_path)
                except FileNotFoundError:
                    pass
                continue

            readability = _visual_readability_score(candidate_path)
            breakdown = calculate_cover_score(
                candidate,
                theme=theme,
                stage_index=candidate_stage,
                visual_readability=readability,
            )
            final_path = os.path.join(config.DATA_DIR, "output_raw_cover.jpg")
            os.replace(candidate_path, final_path)
            mode = determine_cover_mode(candidate.image_width, candidate.image_height)
            logger.info(
                "cover_candidate id=%s quality=%.2f resolution=%.2f composition=%.2f "
                "crop_flexibility=%.2f visual_readability=%.2f aspect=%.2f theme=%.2f "
                "cover_score=%.2f mode=%s",
                candidate_id,
                candidate.quality_score,
                breakdown.resolution,
                breakdown.composition_suitability,
                breakdown.crop_flexibility,
                breakdown.visual_readability,
                breakdown.aspect_ratio,
                breakdown.theme_relevance,
                breakdown.total,
                mode.value,
            )
            return CoverAsset(
                artwork=_cover_artwork_dict(candidate, final_path),
                local_image_path=final_path,
                mode=mode,
                cover_score=breakdown.total,
                score_breakdown=breakdown,
                visual_features=_extract_cover_visual_features(
                    final_path, candidate.image_width, candidate.image_height
                ),
            )

    rejection_summary = ",".join(f"{key}:{value}" for key, value in sorted(rejection_counts.items())) or "none"
    logger.error(
        "cover_selection_failed theme=%s candidates=%s attempted=%s rejections=%s",
        theme,
        len(candidates_by_id),
        len(attempted_ids),
        rejection_summary,
    )
    raise EditorialCoverSelectionError(
        f"Unable to select a safe editorial cover for theme {theme!r}; "
        f"candidates={len(candidates_by_id)} attempted={len(attempted_ids)}"
    )


def _select_editorial_cover_from_acquisition(
    *,
    posted_ids: set[str],
    featured_artworks: Sequence[Mapping[str, object]],
    theme: CarouselThemeDefinition,
    acquisition: ThemeAcquisitionResult,
    run_seed: SelectionRunSeed,
) -> CoverAsset:
    """Select a relevant cover from the same bounded pool used by Featured Works."""
    if (
        theme.evidence_mode is not ThemeEvidenceMode.METADATA
        and acquisition.validated_artworks
    ):
        return _select_validated_hybrid_cover(
            posted_ids=posted_ids,
            featured_artworks=featured_artworks,
            theme=theme,
            acquisition=acquisition,
            run_seed=run_seed,
        )
    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_quality = getattr(config, "MIN_QUALITY_SCORE", 50)
    excluded_ids = set(posted_ids)
    excluded_ids.update(str(artwork["id"]) for artwork in featured_artworks)
    attempted = 0
    rejection_counts: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    def preliminary(candidate: ThemeCandidate):
        return calculate_cover_score(
            candidate.artwork,
            theme=theme.title,
            stage_index=0,
            explicit_theme_relevance=candidate.evidence.theme_relevance_score,
        )

    ranked = sorted(
        (
            (candidate, preliminary(candidate))
            for candidate in acquisition.candidates
            if candidate.artwork.canonical_id not in excluded_ids
            and (
                not acquisition.policy.require_theme_relevance
                or candidate.evidence.theme_relevance_score
                >= acquisition.policy.minimum_relevance
            )
        ),
        key=lambda item: (
            -item[1].total,
            -item[0].evidence.theme_relevance_score,
            -_candidate_tiebreak(run_seed, item[0].artwork.canonical_id),
            item[0].artwork.canonical_id,
        ),
    )
    for themed_candidate, _ in ranked:
        candidate = themed_candidate.artwork
        candidate_id = candidate.canonical_id
        if not is_rights_eligible(candidate):
            reject("rights_policy")
            continue
        if (
            acquisition.policy.require_theme_relevance
            and themed_candidate.evidence.theme_relevance_score
            < acquisition.policy.minimum_relevance
        ):
            reject("theme_relevance_below_threshold")
            continue
        attempted += 1
        candidate_path = os.path.join(config.DATA_DIR, f"cover_candidate_{uuid.uuid4().hex}.jpg")
        validation = validate_and_download_image_with_metadata(candidate.image_url, candidate_path)
        if not validation.valid:
            reject("image_validation_failed")
            logger.info(
                "cover_candidate_rejected id=%s theme=%s reason=image_validation_failed validation_reason=%s",
                candidate_id,
                theme.id,
                validation.reason,
            )
            continue

        candidate.image_width = validation.width
        candidate.image_height = validation.height
        previous_quality = float(candidate.quality_score or 0.0)
        candidate.quality_score = calculate_quality_score(candidate, museum_weights)
        candidate.measurement_coverage = calculate_measurement_coverage(candidate)
        if candidate.selection_score is not None:
            candidate.selection_score += (candidate.quality_score - previous_quality) * 0.30
        if candidate.quality_score < min_quality:
            reject("post_quality_below_threshold")
            try:
                os.remove(candidate_path)
            except FileNotFoundError:
                pass
            continue

        readability = _visual_readability_score(candidate_path)
        breakdown = calculate_cover_score(
            candidate,
            theme=theme.title,
            stage_index=0,
            visual_readability=readability,
            explicit_theme_relevance=themed_candidate.evidence.theme_relevance_score,
        )
        final_path = os.path.join(config.DATA_DIR, "output_raw_cover.jpg")
        os.replace(candidate_path, final_path)
        mode = determine_cover_mode(candidate.image_width, candidate.image_height)
        artwork = _cover_artwork_dict(candidate, final_path)
        artwork["theme_relevance_score"] = themed_candidate.evidence.theme_relevance_score
        artwork["matched_queries"] = tuple(
            hit.query for hit in themed_candidate.evidence.matched_queries
        )
        logger.info(
            "cover_candidate id=%s theme=%s relevance=%.2f quality=%.2f cover_score=%.2f mode=%s",
            candidate_id,
            theme.id,
            themed_candidate.evidence.theme_relevance_score,
            candidate.quality_score,
            breakdown.total,
            mode.value,
        )
        return CoverAsset(
            artwork=artwork,
            local_image_path=final_path,
            mode=mode,
            cover_score=breakdown.total,
            score_breakdown=breakdown,
            visual_features=_extract_cover_visual_features(
                final_path, candidate.image_width, candidate.image_height
            ),
        )

    rejection_summary = ",".join(
        f"{key}:{value}" for key, value in sorted(rejection_counts.items())
    ) or "none"
    logger.error(
        "cover_selection_failed theme=%s pool=%s attempted=%s rejections=%s",
        theme.id,
        len(ranked),
        attempted,
        rejection_summary,
    )
    raise EditorialCoverSelectionError(
        f"Unable to select a relevant safe cover for theme {theme.id}; "
        f"pool={len(ranked)} attempted={attempted}",
        reason="cover_unavailable",
    )


def _select_validated_hybrid_cover(
    *,
    posted_ids: set[str],
    featured_artworks: Sequence[Mapping[str, object]],
    theme: CarouselThemeDefinition,
    acquisition: ThemeAcquisitionResult,
    run_seed: SelectionRunSeed,
) -> CoverAsset:
    """Choose the distinct cover from already validated, final-relevant hybrid images."""
    excluded_ids = set(posted_ids)
    excluded_ids.update(str(artwork["id"]) for artwork in featured_artworks)
    candidates_by_id = {
        candidate.artwork.canonical_id: candidate for candidate in acquisition.candidates
    }
    ranked: list[tuple[Mapping[str, object], ThemeCandidate, CoverScoreBreakdown]] = []
    for artwork in acquisition.validated_artworks:
        candidate_id = str(artwork["id"])
        themed_candidate = candidates_by_id.get(candidate_id)
        if candidate_id in excluded_ids or themed_candidate is None:
            continue
        relevance = float(artwork.get("theme_relevance_score") or 0.0)
        if relevance < acquisition.policy.minimum_relevance:
            continue
        image_path = str(artwork["local_image_path"])
        breakdown = calculate_cover_score(
            themed_candidate.artwork,
            theme=theme.title,
            stage_index=0,
            visual_readability=_visual_readability_score(image_path),
            explicit_theme_relevance=relevance,
        )
        ranked.append((artwork, themed_candidate, breakdown))

    ranked.sort(
        key=lambda item: (
            -item[2].total,
            -float(item[0].get("theme_relevance_score") or 0.0),
            -_candidate_tiebreak(run_seed, item[1].artwork.canonical_id),
            item[1].artwork.canonical_id,
        )
    )
    if not ranked:
        for artwork in acquisition.validated_artworks:
            try:
                os.remove(str(artwork["local_image_path"]))
            except FileNotFoundError:
                pass
        raise EditorialCoverSelectionError(
            f"Unable to select a distinct final-relevant cover for hybrid theme {theme.id!r}"
        )

    artwork, themed_candidate, breakdown = ranked[0]
    source_path = str(artwork["local_image_path"])
    if is_aic_iiif_url(themed_candidate.artwork.image_url or ""):
        final_render_path = os.path.join(
            config.DATA_DIR, f"cover_final_{uuid.uuid4().hex}.jpg"
        )
        final_validation = validate_and_download_image_with_metadata(
            themed_candidate.artwork.image_url,
            final_render_path,
            purpose=ImageDownloadPurpose.FINAL_RENDER,
        )
        if final_validation.valid:
            try:
                os.remove(source_path)
            except FileNotFoundError:
                pass
            source_path = final_render_path
            themed_candidate.artwork.image_width = final_validation.width
            themed_candidate.artwork.image_height = final_validation.height
        else:
            try:
                os.remove(final_render_path)
            except FileNotFoundError:
                pass
    final_path = os.path.join(config.DATA_DIR, "output_raw_cover.jpg")
    os.replace(source_path, final_path)
    cover_artwork = _cover_artwork_dict(themed_candidate.artwork, final_path)
    cover_artwork["theme_relevance_score"] = artwork["theme_relevance_score"]
    cover_artwork["matched_queries"] = artwork.get("matched_queries", ())
    for unused in acquisition.validated_artworks:
        unused_path = str(unused["local_image_path"])
        if unused_path == source_path:
            continue
        try:
            os.remove(unused_path)
        except FileNotFoundError:
            pass
    mode = determine_cover_mode(
        themed_candidate.artwork.image_width, themed_candidate.artwork.image_height
    )
    logger.info(
        "cover_candidate id=%s theme=%s relevance=%.2f quality=%.2f cover_score=%.2f "
        "mode=%s source=validated_hybrid_pool",
        themed_candidate.artwork.canonical_id,
        theme.id,
        float(artwork["theme_relevance_score"]),
        themed_candidate.artwork.quality_score,
        breakdown.total,
        mode.value,
    )
    return CoverAsset(
        artwork=cover_artwork,
        local_image_path=final_path,
        mode=mode,
        cover_score=breakdown.total,
        score_breakdown=breakdown,
        visual_features=artwork.get("visual_features")
        or _extract_cover_visual_features(
            final_path,
            themed_candidate.artwork.image_width,
            themed_candidate.artwork.image_height,
        ),
    )


def derive_cover_micro_facts(
    featured_artworks: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Compatibility adapter for callers that have not yet built editorial facts."""
    facts = derive_carousel_editorial_facts(
        featured_artworks,
        theme_id="selection",
        theme_title="Selection",
        carousel_format="THEMATIC_COLLECTION",
    )
    first = format_count(facts.featured_count, "work")
    if facts.museum_metadata_complete:
        first += f" · {format_count(facts.distinct_museum_count, 'collection')}"
    result = [first]
    if facts.date_span_label:
        result.append(f"Works from {facts.date_span_label}")
    return tuple(result[:2])


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = " ".join(text.split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        proposed = word if not current else f"{current} {word}"
        if current and draw.textbbox((0, 0), proposed, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = proposed
    if current:
        lines.append(current)
    return lines


def _cover_background(source: Image.Image, mode: CoverMode) -> Image.Image:
    source = ImageOps.exif_transpose(source).convert("RGB")
    if mode == CoverMode.DETAIL_CROP:
        return ImageOps.fit(
            source,
            (COVER_WIDTH, COVER_HEIGHT),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

    blurred_fill = ImageOps.fit(
        source,
        (COVER_WIDTH, COVER_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).filter(ImageFilter.GaussianBlur(radius=24))
    contained = ImageOps.contain(source, (COVER_WIDTH, COVER_HEIGHT), Image.Resampling.LANCZOS)
    paste_x = (COVER_WIDTH - contained.width) // 2
    paste_y = (COVER_HEIGHT - contained.height) // 2
    blurred_fill.paste(contained, (paste_x, paste_y))
    return blurred_fill


def create_carousel_editorial_cover(
    *,
    cover: CoverAsset,
    editorial_title: str,
    editorial_subtitle: str,
    micro_facts: Sequence[str] = (),
    output_path: str | None = None,
) -> str:
    """Render a dedicated 1080x1350 editorial opener without artwork identity text."""
    output_path = output_path or os.path.join(config.DATA_DIR, "carousel_cover.jpg")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with Image.open(cover.local_image_path) as source:
        canvas = _cover_background(source, cover.mode)

    # A restrained top-to-bottom dark gradient protects white type while keeping
    # the artwork visible. It is independent of cover metadata.
    overlay = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for y in range(COVER_HEIGHT):
        alpha = int(82 + 82 * (y / (COVER_HEIGHT - 1)))
        overlay_draw.line((0, y, COVER_WIDTH, y), fill=(0, 0, 0, alpha))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    margin = 82
    title_font = _load_font(88, bold=True)
    subtitle_font = _load_font(38)
    facts_font = _load_font(28, bold=True)
    brand_font = _load_font(25, bold=True)
    text_width = COVER_WIDTH - margin * 2

    draw.text((margin, 74), "ARTFOLIO", font=brand_font, fill=(255, 255, 255, 230))
    draw.line((margin, 116, margin + 86, 116), fill=(255, 255, 255, 210), width=3)

    title_lines = _wrap_text(draw, editorial_title.upper(), title_font, text_width)[:3]
    title_y = 700
    for line in title_lines:
        draw.text((margin, title_y), line, font=title_font, fill="white", stroke_width=1, stroke_fill=(0, 0, 0, 80))
        title_y += 98

    subtitle_y = title_y + 22
    for line in _wrap_text(draw, editorial_subtitle, subtitle_font, text_width)[:3]:
        draw.text((margin, subtitle_y), line, font=subtitle_font, fill=(255, 255, 255, 240))
        subtitle_y += 50

    facts = "  ·  ".join(" ".join(str(fact).split()) for fact in micro_facts[:3] if str(fact).strip())
    if facts:
        draw.text((margin, min(subtitle_y + 35, 1250)), facts, font=facts_font, fill=(255, 255, 255, 220))

    canvas.convert("RGB").save(output_path, "JPEG", quality=95, optimize=True)
    logger.info(
        "editorial_cover_rendered path=%s dimensions=%sx%s mode=%s",
        output_path,
        COVER_WIDTH,
        COVER_HEIGHT,
        cover.mode.value,
    )
    return output_path
