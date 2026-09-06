import argparse
import os
import sys
import logging
import inspect
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Sequence

import config
from src.aic_image_policy import get_aic_image_request_policy
from src import (
    art_fetcher,
    gemini_ai,
    history_tracker,
    image_processor,
    instagram_poster,
    publication_reconciliation,
    r2_media,
)
from src.carousel_caption import format_carousel_caption
from src.carousel_featured import (
    derive_carousel_featured_presentation,
    render_carousel_featured_artwork,
)
from src.carousel_cover import (
    create_carousel_editorial_cover,
    EditorialCoverSelectionError,
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
from src.carousel_policy import MAX_FEATURED_WORKS
from src.carousel_sequence import sequence_carousel_artworks
from src.editorial_experiments import (
    ENGAGEMENT_MODEL_VERSION,
    SELECTION_MODEL_VERSION,
    CaptionHookType,
    CoverVariant,
    canonical_publish_slot,
    select_caption_hook_type,
)
from src.engagement_learning import EngagementModel, analyze_engagement_learning
from src.insights_storage import InsightsStorage
from src.carousel_themes import (
    CarouselFormat,
    CarouselThemeDefinition,
    ThemeEvidenceMode,
    ThemeFamily,
    get_default_theme_registry,
    plan_carousel_theme,
    primary_search_query,
)
from src.theme_acquisition import ThemeAcquisitionPolicy
from src.theme_fallback import ThemeAttemptPlanner
from src.theme_feasibility import (
    FeasibilityAttemptRanker,
    ThemeFeasibilityStorage,
    load_feasibility_state,
    record_theme_availability,
)
from src.production_config import (
    validate_production_configuration,
    validate_reconciliation_configuration,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

CAROUSEL_THEME_ATTEMPT_LIMIT = 5
ARTFOLIO_SELECTION_THEME = CarouselThemeDefinition(
    id="artfolio_selection",
    title="Artfolio Selection",
    family=ThemeFamily.SUBJECT,
    format=CarouselFormat.THEMATIC_COLLECTION,
    description="A neutral selection of strong, safe artworks without a shared subject claim.",
    primary_queries=("museum art", "painting", "artwork"),
    secondary_queries=("open access art",),
)


class ProductionMode(str, Enum):
    CAROUSEL = "carousel"


def _format_adapter_reasons(values: dict[str, str]) -> str:
    return ",".join(
        f"{source}:{reason}" for source, reason in sorted(values.items())
    ) or "none"


def _log_adapter_capacity(run_state: art_fetcher.AcquisitionRunState) -> None:
    diagnostics = run_state.diagnostics()
    logger.info(
        "adapter_capacity active=%s unavailable=%s disabled_during_run=%s",
        ",".join(diagnostics["active_adapters"]) or "none",
        _format_adapter_reasons(diagnostics["unavailable_adapters"]),
        _format_adapter_reasons(diagnostics["runtime_disabled_adapters"]),
    )


def _get_grid_color_tone_for_run(dry_run: bool) -> str:
    """Read grid state without allowing dry-run to create a new row."""
    if dry_run:
        return history_tracker.get_grid_color_tone(read_only=True)
    return history_tracker.get_grid_color_tone()


def _cleanup_authoritatively_expired_media(
    publication_id: str, *, reason: str
) -> None:
    """Best-effort cleanup after the EXPIRED transition is durably persisted."""
    cleanup = r2_media.cleanup_publication_media(publication_id, reason=reason)
    if not cleanup.complete:
        return
    try:
        history_tracker.acknowledge_staging_media_cleanup(publication_id)
    except Exception as error:
        logger.exception(
            "r2_publication_cleanup_summary publication_id=%s "
            "result=ack_failed error=%s",
            publication_id,
            type(error).__name__,
        )


def _handle_pre_meta_staging_failure(
    artwork_ids: Sequence[str],
    publication_id: str,
    uploads: Sequence[r2_media.TempMediaUpload],
) -> None:
    """Expire and roll back media that this invocation never supplied to Meta."""
    expiration_persisted = False
    try:
        history_tracker.mark_publication_not_published(
            artwork_ids,
            "pre_meta_staging_failure",
            authoritative=True,
        )
        expiration_persisted = True
    except Exception as error:
        logger.exception(
            "pre_meta_staging_expiration_failed publication_id=%s error=%s",
            publication_id,
            type(error).__name__,
        )

    try:
        r2_media.rollback_temp_media_uploads(publication_id, uploads)
        if not expiration_persisted:
            return
        cleanup = r2_media.cleanup_publication_media(
            publication_id,
            reason="pre_meta_staging_failure",
        )
    except Exception as error:
        logger.exception(
            "r2_publication_cleanup_summary publication_id=%s "
            "result=failed reason=pre_meta_staging_failure error=%s",
            publication_id,
            type(error).__name__,
        )
        return
    if cleanup.complete and expiration_persisted:
        try:
            history_tracker.acknowledge_staging_media_cleanup(publication_id)
        except Exception as error:
            logger.exception(
                "r2_publication_cleanup_summary publication_id=%s "
                "result=ack_failed error=%s",
                publication_id,
                type(error).__name__,
            )


def _log_carousel_dry_run_success(plan: CarouselPlan, artifact_paths: list[str]) -> None:
    logger.info(
        "DRY RUN SUCCESS mode=carousel theme=%s cover_id=%s cover_mode=%s "
        "featured_count=%s total_slide_count=%s featured_ids=%s local_artifacts=%s history_mutation=skipped "
        "media_upload=skipped instagram_publish=skipped",
        plan.theme_id,
        plan.cover.canonical_id,
        plan.cover.mode.value,
        len(plan.featured_artworks),
        len(plan.publication_ids),
        ",".join(plan.featured_ids),
        ",".join(artifact_paths),
    )


def _cleanup_failed_theme_artifacts(artworks) -> None:
    """Remove only generated featured downloads when a later cover/theme gate fails."""
    for artwork in artworks or ():
        path = str(artwork.get("local_image_path", ""))
        if (
            os.path.dirname(os.path.abspath(path)) == os.path.abspath(config.DATA_DIR)
            and os.path.basename(path).startswith("output_raw_")
        ):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


_GENERATED_ARTIFACT_PATTERNS = (
    ".image-download-*.tmp",
    "carousel_[0-9][0-9].jpg",
    "carousel_candidate_*.jpg",
    "carousel_cover.jpg",
    "carousel_final_*.jpg",
    "cover_candidate_*.jpg",
    "output_post.jpg",
    "output_raw_*.jpg",
    "raw_artwork.jpg",
)


def _snapshot_generated_artifacts() -> set[Path]:
    """Capture existing generated files so local user artifacts are preserved."""
    data_directory = Path(config.DATA_DIR).absolute()
    return {
        path.absolute()
        for pattern in _GENERATED_ARTIFACT_PATTERNS
        for path in data_directory.glob(pattern)
        if path.is_file()
    }


def _cleanup_new_generated_artifacts(existing: set[Path]) -> None:
    """Remove only production artifacts created by the current invocation."""
    current = _snapshot_generated_artifacts()
    removed = 0
    for path in current - existing:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning(
                "production_cleanup_failed artifact=%s error=%s",
                path.name,
                type(error).__name__,
            )
    logger.info("production_cleanup_complete removed=%s", removed)


def _resolve_production_mode(args, now: datetime | None = None) -> ProductionMode:
    del now
    return ProductionMode(args.mode)


def _load_engagement_model() -> EngagementModel:
    """Best-effort optimization layer; publishing remains safe without Insights."""
    try:
        history, _ = history_tracker.load_history_with_etag()
        storage = InsightsStorage()
        snapshots = storage.load_all_snapshots()
        audit = analyze_engagement_learning(history, snapshots)
        model = audit.model
    except Exception as error:
        logger.warning(
            "engagement_learning_unavailable error=%s fallback=quality_editorial",
            type(error).__name__,
        )
        return EngagementModel.cold_start()
    logger.info(
        "engagement_model_loaded version=%s useful_carousel_observations=%s "
        "effective_observations=%.3f confidence=%.3f global_score=%.2f",
        model.version,
        model.useful_carousel_observations,
        model.effective_observations,
        model.confidence,
        model.global_score,
    )
    logger.info(
        "engagement_learning_funnel total_publication_records=%s carousel_publications=%s "
        "valid_publication_media_identities=%s publications_with_snapshots=%s "
        "eligible_learning_observations=%s snapshot_identity_mismatches=%s "
        "invalid_loaded_snapshots=%s",
        audit.total_publication_records,
        audit.carousel_publications,
        audit.valid_publication_media_identities,
        audit.publications_with_snapshots,
        audit.eligible_learning_observations,
        audit.excluded_by_reason.get("snapshot_identity_mismatch", 0),
        storage.last_snapshot_load_diagnostics.invalid_snapshots,
    )
    return model


def _preceding_post_distance_minutes(now: datetime) -> float | None:
    try:
        publications = history_tracker.get_recent_publications(limit=1)
    except Exception as error:
        logger.warning(
            "preceding_post_distance_unavailable error=%s",
            type(error).__name__,
        )
        return None
    if not publications:
        return None
    try:
        posted_at = datetime.fromisoformat(
            str(publications[-1]["posted_at"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError):
        return None
    if posted_at.tzinfo is None or posted_at.utcoffset() is None:
        return None
    return max(0.0, (now - posted_at.astimezone(timezone.utc)).total_seconds() / 60)


def run_carousel_post(args):
    logger.info("Running carousel post logic...")
    posted_ids = history_tracker.get_posted_ids()
    color_tone = _get_grid_color_tone_for_run(args.dry_run)

    selection_run_seed = art_fetcher.resolve_selection_run_seed()
    run_time = datetime.now(timezone.utc)
    publish_slot = canonical_publish_slot(run_time)
    engagement_model = _load_engagement_model()
    exploration_selected = engagement_model.exploration_selected(
        selection_run_seed.value
    )
    base_engagement_context = {
        "publish_slot": publish_slot,
        "publication_weekday": run_time.strftime("%A").casefold(),
        "cover_variant": CoverVariant.EDITORIAL.value,
    }
    theme_history = history_tracker.get_recent_carousel_theme_history()
    theme_registry = get_default_theme_registry()
    theme_selection = plan_carousel_theme(
        theme_registry,
        theme_history,
        run_seed=selection_run_seed.value,
        current_month=datetime.now(timezone.utc).month,
        eligible_evidence_modes=(ThemeEvidenceMode.METADATA,),
    )
    if hasattr(theme_selection, "ranked_themes"):
        ordered_candidates = theme_selection.ranked_themes(theme_registry)
        fallback_enabled = True
    else:
        # Compatibility for callers/tests supplying the former one-theme plan shape.
        ordered_candidates = (theme_selection.theme,)
        fallback_enabled = False
    ranked_scores = getattr(theme_selection, "ranked_scores", ())
    base_scores = {
        score.theme_id: score.total
        for score in ranked_scores
        if hasattr(score, "theme_id") and hasattr(score, "total")
    }
    editorial_scores = dict(base_scores)
    if engagement_model.confidence > 0:
        ordered_candidates = engagement_model.rank_themes(
            ordered_candidates,
            base_scores=base_scores,
            context=base_engagement_context,
            run_seed=selection_run_seed.value,
            exploration_selected=exploration_selected,
        )
        editorial_scores = {
            theme.id: engagement_model.theme_score(
                theme,
                base_score=base_scores.get(theme.id, 50.0),
                context=base_engagement_context,
                exploration_selected=exploration_selected,
            )
            for theme in ordered_candidates
        }

    attempted_themes: list[tuple[str, str]] = []
    actual_theme_attempt_order: list[str] = []
    acquisition_run_state = art_fetcher.AcquisitionRunState()
    museum_adapters = tuple(art_fetcher.get_museum_adapters())
    feasibility_storage = ThemeFeasibilityStorage()
    feasibility_state = load_feasibility_state(feasibility_storage)
    feasibility_ranker = FeasibilityAttemptRanker(
        editorial_scores=editorial_scores,
        state=feasibility_state,
        adapters=museum_adapters,
        run_state=acquisition_run_state,
        now=run_time,
        log_limit=CAROUSEL_THEME_ATTEMPT_LIMIT,
    )
    attempt_planner = ThemeAttemptPlanner(
        ordered_candidates,
        attempt_limit=CAROUSEL_THEME_ATTEMPT_LIMIT,
        ranker=feasibility_ranker.rank,
    )
    initial_attempt_plan = attempt_planner.preview()
    feasibility_ranker.log_shortlist(initial_attempt_plan)
    logger.info(
        "theme_attempt_plan themes=%s evidence_modes=%s",
        ",".join(theme.id for theme in initial_attempt_plan),
        ",".join(theme.evidence_mode.value for theme in initial_attempt_plan),
    )
    artworks = None
    cover = None
    acquisition = None
    set_optimization = None
    theme_definition = None
    caption_hook_type: CaptionHookType | None = None
    attempt = 0
    while candidate_theme := attempt_planner.next_theme():
        attempt += 1
        actual_theme_attempt_order.append(candidate_theme.id)
        search_query = primary_search_query(candidate_theme)
        candidate_hook_type = select_caption_hook_type(
            candidate_theme,
            run_seed=selection_run_seed.value,
        )
        engagement_context = {
            **base_engagement_context,
            "carousel_theme": candidate_theme.id,
            "carousel_format": candidate_theme.format.value,
            "caption_hook_type": candidate_hook_type.value,
        }
        if attempted_themes:
            previous_failure = attempt_planner.failures[-1]
            logger.info(
                "theme_fallback from=%s to=%s attempt=%s previous_reason=%s",
                attempted_themes[-1][0],
                candidate_theme.id,
                attempt,
                previous_failure.reason,
            )
        logger.info(
            "carousel_theme_attempt theme=%s title=%r attempt=%s compatibility_query=%r",
            candidate_theme.id,
            candidate_theme.title,
            attempt,
            search_query,
        )
        candidate_acquisition = None
        candidate_set_optimization = None
        candidate_artworks = None
        availability_recorded = False
        try:
            selection = art_fetcher.fetch_themed_artworks(
                posted_ids,
                search_query,
                count=MAX_FEATURED_WORKS,
                color_tone=color_tone,
                selection_run_seed=selection_run_seed,
                theme_definition=candidate_theme,
                return_acquisition=True,
                acquisition_run_state=acquisition_run_state,
                engagement_model=engagement_model,
                engagement_context=engagement_context,
                exploration_selected=exploration_selected,
                adapters=museum_adapters,
            )
            if isinstance(selection, art_fetcher.ThemedArtworkSelection):
                candidate_artworks = list(selection.artworks)
                candidate_acquisition = selection.acquisition
                candidate_set_optimization = selection.set_optimization
            else:
                candidate_artworks = selection
                candidate_acquisition = None
            if candidate_acquisition is not None:
                record_theme_availability(
                    candidate_acquisition.availability,
                    theme=candidate_theme,
                    adapters=museum_adapters,
                    run_state=acquisition_run_state,
                    attempted_at=datetime.now(timezone.utc),
                    storage=feasibility_storage,
                )
                availability_recorded = True
            candidate_cover = select_editorial_cover(
                posted_ids=posted_ids,
                featured_artworks=candidate_artworks,
                theme=search_query,
                color_tone=color_tone,
                selection_run_seed=selection_run_seed,
                theme_definition=candidate_theme,
                acquisition=candidate_acquisition,
            )
        except (art_fetcher.CarouselSelectionError, EditorialCoverSelectionError) as error:
            if not fallback_enabled:
                raise
            _cleanup_failed_theme_artifacts(candidate_artworks)
            reason = getattr(error, "reason", type(error).__name__)
            attempted_themes.append((candidate_theme.id, reason))
            attempt_planner.record_failure(candidate_theme, reason)
            availability = getattr(error, "availability", None) or getattr(
                candidate_acquisition,
                "availability",
                None,
            )
            if availability is not None and not availability_recorded:
                record_theme_availability(
                    availability,
                    theme=candidate_theme,
                    adapters=museum_adapters,
                    run_state=acquisition_run_state,
                    attempted_at=datetime.now(timezone.utc),
                    storage=feasibility_storage,
                )
            logger.info(
                "theme_unavailable theme=%s reason=%s eligible=%s target=%s attempt=%s",
                candidate_theme.id,
                reason,
                getattr(availability, "estimated_safe_pool", "unknown"),
                getattr(availability, "target", "unknown"),
                attempt,
            )
            feasibility_ranker.log_shortlist(attempt_planner.remaining_preview())
            continue

        artworks = candidate_artworks
        cover = candidate_cover
        acquisition = candidate_acquisition
        set_optimization = candidate_set_optimization
        theme_definition = candidate_theme
        caption_hook_type = candidate_hook_type
        logger.info(
            "theme_attempt_succeeded theme=%s attempt=%s",
            candidate_theme.id,
            attempt,
        )
        break

    logger.info(
        "theme_attempt_order themes=%s",
        ",".join(actual_theme_attempt_order) or "none",
    )

    if theme_definition is None and fallback_enabled:
        generic_theme = ARTFOLIO_SELECTION_THEME
        generic_query = primary_search_query(generic_theme)
        generic_hook_type = select_caption_hook_type(
            generic_theme,
            run_seed=selection_run_seed.value,
        )
        generic_context = {
            **base_engagement_context,
            "carousel_theme": generic_theme.id,
            "carousel_format": generic_theme.format.value,
            "caption_hook_type": generic_hook_type.value,
        }
        candidate_artworks = None
        candidate_acquisition = None
        availability_recorded = False
        logger.info(
            "generic_production_fallback theme=%s title=%r after_failures=%s",
            generic_theme.id,
            generic_theme.title,
            len(attempted_themes),
        )
        try:
            selection = art_fetcher.fetch_themed_artworks(
                posted_ids,
                generic_query,
                count=MAX_FEATURED_WORKS,
                color_tone=color_tone,
                selection_run_seed=selection_run_seed,
                theme_definition=generic_theme,
                return_acquisition=True,
                acquisition_policy=ThemeAcquisitionPolicy(
                    require_theme_relevance=False
                ),
                acquisition_run_state=acquisition_run_state,
                engagement_model=engagement_model,
                engagement_context=generic_context,
                exploration_selected=exploration_selected,
                adapters=museum_adapters,
            )
            if isinstance(selection, art_fetcher.ThemedArtworkSelection):
                candidate_artworks = list(selection.artworks)
                candidate_acquisition = selection.acquisition
                candidate_set_optimization = selection.set_optimization
            else:
                candidate_artworks = selection
                candidate_set_optimization = None
            if candidate_acquisition is not None:
                record_theme_availability(
                    candidate_acquisition.availability,
                    theme=generic_theme,
                    adapters=museum_adapters,
                    run_state=acquisition_run_state,
                    attempted_at=datetime.now(timezone.utc),
                    storage=feasibility_storage,
                )
                availability_recorded = True
            candidate_cover = select_editorial_cover(
                posted_ids=posted_ids,
                featured_artworks=candidate_artworks,
                theme=generic_query,
                color_tone=color_tone,
                selection_run_seed=selection_run_seed,
                theme_definition=generic_theme,
                acquisition=candidate_acquisition,
            )
        except (art_fetcher.CarouselSelectionError, EditorialCoverSelectionError) as error:
            _cleanup_failed_theme_artifacts(candidate_artworks)
            reason = getattr(error, "reason", type(error).__name__)
            attempted_themes.append((generic_theme.id, reason))
            availability = getattr(error, "availability", None) or getattr(
                candidate_acquisition,
                "availability",
                None,
            )
            if availability is not None and not availability_recorded:
                record_theme_availability(
                    availability,
                    theme=generic_theme,
                    adapters=museum_adapters,
                    run_state=acquisition_run_state,
                    attempted_at=datetime.now(timezone.utc),
                    storage=feasibility_storage,
                )
            logger.info(
                "generic_production_fallback_unavailable theme=%s reason=%s",
                generic_theme.id,
                reason,
            )
        else:
            artworks = candidate_artworks
            cover = candidate_cover
            acquisition = candidate_acquisition
            set_optimization = candidate_set_optimization
            theme_definition = generic_theme
            caption_hook_type = generic_hook_type
            logger.info(
                "generic_production_fallback_succeeded theme=%s featured=%s",
                generic_theme.id,
                len(candidate_artworks),
            )

    if (
        theme_definition is None
        or artworks is None
        or cover is None
        or caption_hook_type is None
    ):
        _log_adapter_capacity(acquisition_run_state)
        raise art_fetcher.CarouselThemeAvailabilityError(attempted_themes)

    _log_adapter_capacity(acquisition_run_state)

    if not args.dry_run:
        logger.info(
            "selection_complete mode=carousel featured=%s cover=%s",
            len(artworks),
            cover.canonical_id,
        )

    logger.info("Fetched %s artworks for the carousel.", len(artworks))
    logger.info("Selected editorial cover %s (%s).", cover.canonical_id, cover.mode.value)
    aic_images = get_aic_image_request_policy().diagnostics()
    logger.info(
        "aic_image_requests analysis_843=%s final_1686=%s fallback_843=%s "
        "rate_limited=%s recovered=%s failed=%s circuit_open=%s",
        aic_images.analysis_843,
        aic_images.final_1686,
        aic_images.fallback_843,
        aic_images.rate_limited,
        aic_images.recovered,
        aic_images.failed,
        aic_images.circuit_open,
    )
    logger.info(
        "carousel_theme_selected theme=%s family=%s format=%s availability_pool=%s "
        "featured=%s cover=%s queries_used=%s",
        theme_definition.id,
        theme_definition.family.value,
        theme_definition.format.value,
        acquisition.availability.estimated_safe_pool if acquisition is not None else "compatibility",
        len(artworks),
        cover.canonical_id,
        acquisition.availability.query_count if acquisition is not None else "compatibility",
    )

    sequence = sequence_carousel_artworks(
        artworks,
        theme=theme_definition,
        cover_visual_features=cover.visual_features,
    )
    artworks = list(sequence.ordered_artworks)

    editorial_facts = derive_carousel_editorial_facts(
        artworks,
        theme_id=theme_definition.id,
        theme_title=theme_definition.title,
        carousel_format=theme_definition.format,
    )
    logger.info(
        "carousel_editorial_facts theme=%s featured=%s artists=%s museums=%s date_span=%s",
        editorial_facts.theme_id,
        editorial_facts.featured_count,
        editorial_facts.distinct_artist_count,
        editorial_facts.distinct_museum_count,
        editorial_facts.date_span_label or "omitted",
    )
        
    analysis_parameters = inspect.signature(gemini_ai.analyze_carousel).parameters
    analysis_context = {}
    if "carousel_format" in analysis_parameters:
        analysis_context["carousel_format"] = theme_definition.format.value
    if "format_target" in analysis_parameters:
        analysis_context["format_target"] = (
            theme_definition.format_target.model_dump(mode="json", exclude_none=True)
            if theme_definition.format_target
            else None
        )
    if "editorial_facts" in analysis_parameters:
        analysis_context["editorial_facts"] = editorial_facts.as_dict()
    if "caption_hook_type" in analysis_parameters:
        analysis_context["caption_hook_type"] = caption_hook_type.value
    ai_analysis = (
        None
        if theme_definition.id == ARTFOLIO_SELECTION_THEME.id
        else gemini_ai.analyze_carousel(
            theme_definition.title,
            artworks,
            **analysis_context,
        )
    )
    format_target_name = None
    if theme_definition.format_target:
        format_target_name = (
            theme_definition.format_target.artist_name
            or theme_definition.format_target.museum_name
        )
    fallback_intro = fallback_carousel_intro(editorial_facts, format_target_name)
    editorial_intro = grounded_gemini_intro(
        (ai_analysis or {}).get("editorial_intro"), editorial_facts, fallback_intro
    )
    hashtags = (
        ai_analysis.get("hashtags", "#Art #Artfolio #ClassicArt #MuseumArt")
        if ai_analysis else "#Art #Artfolio #ClassicArt #MuseumArt"
    )
    theme_title = editorial_facts.theme_title
    editorial_subtitle = fallback_editorial_subtitle(editorial_facts)

    final_caption = format_carousel_caption(
        theme_title=theme_title,
        editorial_intro=editorial_intro,
        hashtags=hashtags,
        featured_artworks=artworks,
    )

    plan = CarouselPlan.build(
        theme=theme_definition,
        editorial_title=theme_title,
        editorial_subtitle=editorial_subtitle,
        cover_micro_facts=derive_cover_micro_facts(editorial_facts),
        editorial_facts=editorial_facts,
        caption_intro=editorial_intro,
        cover=cover,
        featured_artworks=artworks,
        caption=final_caption,
        cover_variant=CoverVariant.EDITORIAL.value,
        caption_hook_type=caption_hook_type.value,
        set_optimization=set_optimization,
        sequence=sequence,
    )

    output_media_paths = [
        create_carousel_editorial_cover(
            cover=plan.cover,
            editorial_title=plan.editorial_title,
            editorial_subtitle=plan.editorial_subtitle,
            micro_facts=plan.cover_micro_facts,
            output_path=os.path.join(config.DATA_DIR, "carousel_cover.jpg"),
        )
    ]

    featured_presentation = derive_carousel_featured_presentation(
        plan.featured_artworks,
        cover_visual_features=plan.cover.visual_features,
        grid_color_tone=color_tone,
    )
    logger.info(
        "carousel_featured_presentation mode=%s field=%s median_luminance=%s",
        featured_presentation.mode.value,
        featured_presentation.field_family.value,
        featured_presentation.evidence_median_luminance,
    )

    for index, art in enumerate(plan.featured_artworks, start=1):
        render_result = render_carousel_featured_artwork(
            art["local_image_path"],
            presentation=featured_presentation,
            output_path=os.path.join(config.DATA_DIR, f"carousel_{index:02d}.jpg"),
        )
        output_media_paths.append(render_result.output_path)

    if args.dry_run:
        _log_carousel_dry_run_success(plan, output_media_paths)
        return

    logger.info("media_prepared mode=carousel count=%s", len(output_media_paths))

    final_engagement_context = {
        **base_engagement_context,
        "carousel_theme": plan.theme.id,
        "carousel_format": plan.theme.format.value,
        "caption_hook_type": plan.caption_hook_type,
        "featured_count": len(plan.featured_artworks),
    }
    set_prediction = engagement_model.score_set(
        plan.featured_artworks,
        final_engagement_context,
    )
    editorial_quality = sum(
        (
            float(artwork.get("quality_score") or 0.0)
            if plan.theme.id == ARTFOLIO_SELECTION_THEME.id
            else 0.65 * float(artwork.get("theme_relevance_score") or 0.0)
            + 0.35 * float(artwork.get("quality_score") or 0.0)
        )
        for artwork in plan.featured_artworks
    ) / len(plan.featured_artworks)
    diversity_adjustment = 0.0
    if plan.set_optimization is not None:
        breakdown = plan.set_optimization.breakdown
        diversity_adjustment = (
            breakdown.total
            - breakdown.individual_strength
            - breakdown.engagement_prediction_adjustment
        )
    selection_components = engagement_model.blend_candidate_score(
        quality_editorial_score=editorial_quality,
        prediction=set_prediction,
        exploration_selected=exploration_selected,
        diversity_component=diversity_adjustment,
    )
    preceding_distance = _preceding_post_distance_minutes(run_time)
    publication_metadata = {
        "selection_model_version": SELECTION_MODEL_VERSION,
        "engagement_model_version": ENGAGEMENT_MODEL_VERSION,
        "carousel_theme": plan.theme.id,
        "carousel_format": plan.theme.format.value,
        "featured_count": len(plan.featured_artworks),
        "cover_variant": plan.cover_variant,
        "caption_hook_type": plan.caption_hook_type,
        "publish_slot": publish_slot,
        "exploration_selected": exploration_selected,
        "learned_score": selection_components.learned_score,
        "engagement_confidence": selection_components.engagement_confidence,
        "quality_component": selection_components.quality_component,
        "engagement_component": selection_components.engagement_component,
        "diversity_component": selection_components.diversity_component,
        "exploration_component": selection_components.exploration_component,
        **(
            {"preceding_post_distance_minutes": round(preceding_distance, 3)}
            if preceding_distance is not None
            else {}
        ),
    }
    logger.info(
        "carousel_selection_score quality=%.2f engagement=%.2f "
        "engagement_confidence=%.3f exploration=%.2f diversity=%.2f final=%.2f "
        "slot=%s exploration_selected=%s",
        selection_components.quality_component,
        selection_components.engagement_component,
        selection_components.engagement_confidence,
        selection_components.exploration_component,
        selection_components.diversity_component,
        selection_components.final_score,
        publish_slot,
        exploration_selected,
    )

    # All selection, copy, validation, and rendering has succeeded. Reserve the
    # all variable-length canonical IDs together before any Instagram media operation.
    publication_id = history_tracker.reserve_carousel(
        dict(plan.cover.artwork),
        [dict(art) for art in plan.featured_artworks],
        theme_id=plan.theme.id,
        theme_family=plan.theme.family.value,
        carousel_format=plan.theme.format.value,
        publication_metadata=publication_metadata,
    )
    logger.info("reservation_complete mode=carousel count=%s", len(plan.publication_ids))
    media_uploads: list[r2_media.TempMediaUpload] = []
    try:
        for path in output_media_paths:
            media_uploads.append(
                image_processor.upload_temp_media(path, publication_id)
            )
    except Exception:
        _handle_pre_meta_staging_failure(
            plan.publication_ids, publication_id, media_uploads
        )
        raise
    public_urls = [upload.public_url for upload in media_uploads]
    logger.info("upload_complete mode=carousel count=%s", len(public_urls))

    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")

    publish_attempt_started = False

    def before_publish(container_id: str, child_container_ids: tuple[str, ...]) -> None:
        nonlocal publish_attempt_started
        history_tracker.start_publication_attempt(
            plan.publication_ids, container_id, child_container_ids
        )
        publish_attempt_started = True

    try:
        # Exact order: editorial cover, then every selected Featured Work.
        carousel_id = instagram_poster.post_carousel_to_instagram_graph_api(
            media_urls=public_urls,
            caption=plan.caption,
            account_id=account_id,
            access_token=access_token,
            before_publish=before_publish,
        )
        if not publish_attempt_started:
            history_tracker.mark_artworks_ambiguous(
                plan.publication_ids, "publisher_skipped_durable_boundary"
            )
            raise RuntimeError("Instagram publisher skipped the durable publication boundary")
        logger.info("publish_complete mode=carousel media_id=%s", carousel_id)
    except instagram_poster.InstagramPublishAmbiguousError:
        logger.error("Instagram carousel publish result is ambiguous; preserving duplicate locks.")
        try:
            history_tracker.mark_artworks_ambiguous(plan.publication_ids)
        except Exception:
            logger.exception("Failed to preserve ambiguous carousel reservations.")
            raise
        raise
    except instagram_poster.InstagramAPIError as error:
        if publish_attempt_started:
            history_tracker.mark_publication_not_published(
                plan.publication_ids,
                f"definitive_media_publish_rejection:{type(error).__name__}",
                authoritative=True,
            )
            _cleanup_authoritatively_expired_media(
                publication_id,
                reason="definitive_media_publish_rejection",
            )
        raise
    except Exception as error:
        if publish_attempt_started:
            try:
                history_tracker.mark_artworks_ambiguous(
                    plan.publication_ids,
                    f"unexpected_post_boundary_error:{type(error).__name__}",
                )
            except Exception:
                logger.exception("Failed to preserve uncertain carousel reservations.")
        raise

    try:
        history_tracker.record_publish_response(plan.publication_ids, carousel_id)
    except Exception:
        logger.exception(
            "Failed to record Instagram carousel media ID before final history confirmation; "
            "the durable parent-container lock remains."
        )
    history_tracker.confirm_carousel_publication(
        plan.cover.canonical_id,
        plan.featured_ids,
        carousel_id,
    )
    logger.info("history_confirmed mode=carousel count=%s", len(plan.publication_ids))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Instagram Art Museum Automation Bot")
    parser.add_argument("--dry-run", action="store_true", help="Run bot locally without posting to Instagram")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in ProductionMode],
        default=ProductionMode.CAROUSEL.value,
        help="Publish the canonical carousel feed product",
    )
    parser.add_argument(
        "--validate-production-config",
        action="store_true",
        help="Validate required production environment variables and exit",
    )
    parser.add_argument(
        "--reconcile-publications",
        action="store_true",
        help="Reconcile existing publication lifecycle state without creating or publishing media",
    )
    args = parser.parse_args(argv)

    existing_artifacts: set[Path] | None = None
    try:
        if args.validate_production_config:
            optional_status = validate_production_configuration()
            logger.info(
                "validation_complete config_only=true optional_integrations=%s",
                ",".join(
                    f"{name}:{status}"
                    for name, status in sorted(optional_status.items())
                ),
            )
            return 0

        if args.reconcile_publications:
            if args.dry_run:
                raise ValueError("--reconcile-publications cannot be combined with --dry-run")
            validate_reconciliation_configuration()
            summary = publication_reconciliation.reconcile_publications(
                access_token=os.environ.get("INSTAGRAM_ACCESS_TOKEN", ""),
                limit=publication_reconciliation.MANUAL_RECONCILIATION_LIMIT,
                max_age=None,
            )
            logger.info(
                "reconciliation_complete manual=true inspected=%s published=%s "
                "not_published=%s ambiguous=%s errors=%s cleanup_inspected=%s "
                "cleanup_deleted=%s cleanup_failures=%s",
                summary.inspected,
                summary.confirmed_published,
                summary.confirmed_not_published,
                summary.still_ambiguous,
                summary.errors,
                getattr(summary, "cleanup_inspected", 0),
                getattr(summary, "cleanup_deleted", 0),
                getattr(summary, "cleanup_failures", 0),
            )
            return 1 if (
                summary.errors or getattr(summary, "cleanup_failures", 0)
            ) else 0

        mode = _resolve_production_mode(args)
        if not args.dry_run:
            logger.info("production_start mode=%s", mode.value)
            optional_status = validate_production_configuration()
            logger.info(
                "validation_complete config_only=false optional_integrations=%s",
                ",".join(
                    f"{name}:{status}"
                    for name, status in sorted(optional_status.items())
                ),
            )
            existing_artifacts = _snapshot_generated_artifacts()

        if args.dry_run:
            logger.info("[DRY-RUN MODE] Skipping publication reconciliation.")
        else:
            summary = publication_reconciliation.reconcile_publications(
                access_token=os.environ.get("INSTAGRAM_ACCESS_TOKEN", ""),
            )
            logger.info(
                "reconciliation_complete manual=false inspected=%s published=%s "
                "not_published=%s ambiguous=%s errors=%s cleanup_inspected=%s "
                "cleanup_deleted=%s cleanup_failures=%s",
                summary.inspected,
                summary.confirmed_published,
                summary.confirmed_not_published,
                summary.still_ambiguous,
                summary.errors,
                getattr(summary, "cleanup_inspected", 0),
                getattr(summary, "cleanup_deleted", 0),
                getattr(summary, "cleanup_failures", 0),
            )

        run_carousel_post(args)

        if not args.dry_run:
            logger.info("production_success mode=%s", mode.value)
        return 0
    except Exception as error:
        logger.exception("production_failure error=%s", type(error).__name__)
        return 1
    finally:
        if existing_artifacts is not None:
            _cleanup_new_generated_artifacts(existing_artifacts)

if __name__ == "__main__":
    sys.exit(main())
