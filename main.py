import argparse
import os
import sys
import logging
import hashlib
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Sequence

import config
from src.aic_image_policy import get_aic_image_request_policy
from src import (
    art_fetcher,
    content_diversity,
    gemini_ai,
    history_tracker,
    image_processor,
    instagram_poster,
    pinterest_poster,
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
from src.instagram_image import (
    InstagramImagePublishability,
    InstagramImagePublishabilityReason,
    PreparedSingleImage,
    SingleImageProcessing,
    prepare_single_instagram_image,
)
from src.carousel_themes import (
    get_default_theme_registry,
    plan_carousel_theme,
    primary_search_query,
)
from src.single_post_diversity import classify_orientation as classify_single_orientation
from src.production_config import (
    validate_production_configuration,
    validate_reconciliation_configuration,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

CAROUSEL_THEME_ATTEMPT_LIMIT = 5
SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT = 5


class ProductionMode(str, Enum):
    AUTO = "auto"
    SINGLE = "single"
    CAROUSEL = "carousel"


class SinglePostResolutionCode(str, Enum):
    READY = "READY"
    PROCESSING_FAILURE = "PROCESSING_FAILURE"
    NO_SINGLE_POST_PUBLISHABLE_CANDIDATE = (
        "NO_SINGLE_POST_PUBLISHABLE_CANDIDATE"
    )


@dataclass(frozen=True)
class SinglePostCandidateDiagnostic:
    canonical_id: str
    reason: InstagramImagePublishabilityReason


@dataclass(frozen=True)
class SinglePostResolution:
    result: SinglePostResolutionCode
    attempted: int
    zero_touch: int
    compatibility_processed: int
    single_ineligible: int
    fatal_failures: int
    diagnostics: tuple[SinglePostCandidateDiagnostic, ...]


class SinglePostProcessingError(RuntimeError):
    """A candidate failed technical processing after secure acquisition."""

    def __init__(
        self,
        canonical_id: str,
        result: InstagramImagePublishability,
    ):
        self.canonical_id = canonical_id
        self.result = result
        super().__init__(
            "Single-image compatibility processing failed: "
            f"{result.reason.value}"
        )


_SINGLE_LANE_INELIGIBLE_REASONS = frozenset(
    {
        InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE,
        InstagramImagePublishabilityReason.UNSUPPORTED_TRANSPARENCY,
        InstagramImagePublishabilityReason.UNSUPPORTED_FORMAT,
        InstagramImagePublishabilityReason.FILE_TOO_LARGE,
        InstagramImagePublishabilityReason.SIZE_UNRECOVERABLE,
    }
)


def _get_grid_color_tone_for_run(dry_run: bool) -> str:
    """Read grid state without allowing dry-run to create a new row."""
    if dry_run:
        return history_tracker.get_grid_color_tone(read_only=True)
    return history_tracker.get_grid_color_tone()


def _log_dry_run_success(mode: str, artworks: list[dict], artifact_paths: list[str]) -> None:
    """Report the local artifacts produced without invoking publish mutations."""
    selected_ids = ",".join(artwork["id"] for artwork in artworks)
    quality_summary = ",".join(
        f"{artwork['id']}:quality={artwork.get('quality_score')} selection={artwork.get('selection_score')}"
        for artwork in artworks
    )
    logger.info(
        "DRY RUN SUCCESS mode=%s selected_ids=%s local_artifacts=%s scores=%s "
        "history_mutation=skipped media_upload=skipped instagram_publish=skipped pinterest_publish=skipped",
        mode,
        selected_ids,
        ",".join(artifact_paths),
        quality_summary,
    )


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
        "media_upload=skipped instagram_publish=skipped pinterest_publish=skipped",
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
    requested_mode = ProductionMode(args.mode)
    if args.force_carousel or requested_mode is ProductionMode.CAROUSEL:
        return ProductionMode.CAROUSEL
    if requested_mode is ProductionMode.SINGLE:
        return ProductionMode.SINGLE
    current_hour = (now or datetime.now(timezone.utc)).hour
    return (
        ProductionMode.CAROUSEL
        if current_hour in {12, 21}
        else ProductionMode.SINGLE
    )


def _log_single_candidate_resolution(resolution: SinglePostResolution) -> None:
    logger.info(
        "single_candidate_resolution attempted=%s zero_touch=%s "
        "compatibility_processed=%s single_ineligible=%s fatal_failures=%s result=%s",
        resolution.attempted,
        resolution.zero_touch,
        resolution.compatibility_processed,
        resolution.single_ineligible,
        resolution.fatal_failures,
        resolution.result.value,
    )


def _resolve_single_post_candidate(
    posted_ids: set,
) -> tuple[dict | None, PreparedSingleImage | None, SinglePostResolution]:
    diagnostics: list[SinglePostCandidateDiagnostic] = []
    zero_touch = 0
    compatibility_processed = 0
    single_ineligible = 0

    candidates = art_fetcher.iter_single_post_candidates(
        posted_ids,
        max_candidates=SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT,
    )
    for attempt, artwork in enumerate(
        islice(candidates, SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT), start=1
    ):
        prepared_image = prepare_single_instagram_image(
            artwork["local_image_path"],
            config.OUTPUT_IMAGE_PATH,
        )
        publishability = prepared_image.publishability
        logger.info(
            "single_image_publishability canonical_id=%s size=%sx%s aspect=%s "
            "format=%s file_size=%s result=%s processing=%s",
            artwork["id"],
            publishability.width,
            publishability.height,
            (
                f"{publishability.aspect_ratio:.3f}"
                if publishability.aspect_ratio is not None
                else "unknown"
            ),
            publishability.image_format or "unknown",
            publishability.file_size,
            publishability.reason.value,
            prepared_image.processing.value,
        )
        diagnostics.append(
            SinglePostCandidateDiagnostic(
                canonical_id=artwork["id"],
                reason=publishability.reason,
            )
        )

        if publishability.publishable and prepared_image.path is not None:
            if prepared_image.processing is SingleImageProcessing.ZERO_TOUCH:
                zero_touch += 1
                result = "SUPPORTED_AS_IS"
            else:
                compatibility_processed += 1
                result = "SUPPORTED_AFTER_TECHNICAL_COMPATIBILITY"
            logger.info(
                "single_candidate_attempt attempt=%s canonical_id=%s result=%s action=PUBLISH",
                attempt,
                artwork["id"],
                result,
            )
            if not prepared_image.source_bytes_preserved:
                logger.info(
                    "single_image_compatibility canonical_id=%s source_bytes=%s "
                    "published_bytes=%s source_dimensions=%sx%s published_dimensions=%sx%s "
                    "processing=%s byte_preservation=false jpeg_quality=%s attempts=%s",
                    artwork["id"],
                    prepared_image.source.file_size,
                    publishability.file_size,
                    prepared_image.source.width,
                    prepared_image.source.height,
                    publishability.width,
                    publishability.height,
                    prepared_image.processing.value,
                    prepared_image.jpeg_quality,
                    prepared_image.compatibility_attempts,
                )
            resolution = SinglePostResolution(
                result=SinglePostResolutionCode.READY,
                attempted=attempt,
                zero_touch=zero_touch,
                compatibility_processed=compatibility_processed,
                single_ineligible=single_ineligible,
                fatal_failures=0,
                diagnostics=tuple(diagnostics),
            )
            _log_single_candidate_resolution(resolution)
            return artwork, prepared_image, resolution

        if publishability.reason in _SINGLE_LANE_INELIGIBLE_REASONS:
            single_ineligible += 1
            logger.info(
                "single_candidate_attempt attempt=%s canonical_id=%s result=%s action=SKIP_SINGLE",
                attempt,
                artwork["id"],
                publishability.reason.value,
            )
            continue

        logger.error(
            "single_candidate_attempt attempt=%s canonical_id=%s result=%s action=ABORT",
            attempt,
            artwork["id"],
            publishability.reason.value,
        )
        resolution = SinglePostResolution(
            result=SinglePostResolutionCode.PROCESSING_FAILURE,
            attempted=attempt,
            zero_touch=zero_touch,
            compatibility_processed=compatibility_processed,
            single_ineligible=single_ineligible,
            fatal_failures=1,
            diagnostics=tuple(diagnostics),
        )
        _log_single_candidate_resolution(resolution)
        raise SinglePostProcessingError(artwork["id"], publishability)

    resolution = SinglePostResolution(
        result=SinglePostResolutionCode.NO_SINGLE_POST_PUBLISHABLE_CANDIDATE,
        attempted=len(diagnostics),
        zero_touch=zero_touch,
        compatibility_processed=compatibility_processed,
        single_ineligible=single_ineligible,
        fatal_failures=0,
        diagnostics=tuple(diagnostics),
    )
    _log_single_candidate_resolution(resolution)
    return None, None, resolution


def run_single_post(args) -> SinglePostResolution:
    logger.info("Running single post logic...")
    posted_ids = history_tracker.get_posted_ids()
    artwork, prepared_image, resolution = _resolve_single_post_candidate(posted_ids)
    if artwork is None or prepared_image is None:
        if not args.dry_run:
            logger.info(
                "selection_complete mode=single result=%s selected=none",
                resolution.result.value,
            )
        logger.warning(
            "No single-post-publishable candidate found within attempt budget=%s; "
            "ending run without publication.",
            SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT,
        )
        return resolution

    if not args.dry_run:
        logger.info(
            "selection_complete mode=single result=%s selected=%s",
            resolution.result.value,
            artwork["id"],
        )

    publishability = prepared_image.publishability

    artwork.update(
        {
            "published_width": publishability.width,
            "published_height": publishability.height,
            "published_image_format": publishability.image_format,
            "published_image_file_size": publishability.file_size,
            "source_width": prepared_image.source.width,
            "source_height": prepared_image.source.height,
            "source_image_format": prepared_image.source.image_format,
            "source_image_file_size": prepared_image.source.file_size,
            "exif_orientation": prepared_image.source.exif_orientation,
            "image_processing": prepared_image.processing.value,
            "compatibility_conversion": prepared_image.compatibility_conversion,
            "source_bytes_preserved": prepared_image.source_bytes_preserved,
            "jpeg_compatibility_quality": prepared_image.jpeg_quality,
            "compatibility_attempts": prepared_image.compatibility_attempts,
            "published_orientation": classify_single_orientation(
                publishability.width,
                publishability.height,
            ).value,
        }
    )

    selection_breakdown = artwork.get("_single_selection_breakdown")
    if isinstance(selection_breakdown, dict):
        logger.info(
            "single_selection_breakdown canonical_id=%s quality=%.2f museum=%+.2f region=%+.2f orientation=%+.2f artist=%+.2f visual_category=%+.2f discovery=%+.2f serendipity=%+.2f final=%.2f",
            artwork["id"],
            selection_breakdown["quality"],
            selection_breakdown["museum"],
            selection_breakdown["region"],
            selection_breakdown["orientation"],
            selection_breakdown["artist"],
            selection_breakdown["visual_category"],
            selection_breakdown["discovery"],
            selection_breakdown["serendipity"],
            selection_breakdown["final"],
        )

    if args.dry_run:
        logger.info("[DRY-RUN MODE] Skipping history reservation.")
        publication_id = None
    else:
        logger.info("Reserving artwork in history (PRE-WRITE)...")
        publication_id = history_tracker.reserve_artwork(artwork)
        logger.info("reservation_complete mode=single count=1")

    output_media_path = prepared_image.path

    recent_history = history_tracker.get_recent_history()
    content_type = content_diversity.select_content_type(recent_history)
    artwork["content_type"] = content_type

    logger.info(f"Analyzing artwork with Google Gemini AI (Content Type: {content_type})...")
    ai_analysis = gemini_ai.analyze_artwork(
        output_media_path,
        artwork["title"], 
        artwork["artist"], 
        artwork["date"], 
        artwork["museum"],
        artwork.get("medium", ""),
        artwork.get("classification", ""),
        content_type=content_type
    )
    
    clean_title = artwork['title'].strip() if artwork.get('title') else "Untitled"
    clean_artist = artwork['artist'].strip() if artwork.get('artist') else "Unknown Artist"
    
    if ai_analysis:
        logger.info("Gemini analysis successful! Updating metadata...")

        ref_num = int(hashlib.md5(f"{clean_title}{clean_artist}".encode('utf-8')).hexdigest()[:8], 16) % 100000
        catalog_index = f"ARTFOLIO / REF-{ref_num:05d}"
        artwork["catalog_index"] = catalog_index
        
        artwork["caption"] = (
            f"⠀\n"
            f"{clean_title}\n"
            f"\n"
            f"{clean_artist} - {artwork.get('date', 'Unknown')}\n"
            f"\n"
            f"{artwork.get('museum', 'Unknown')}\n"
            f"\n"
            f"{ai_analysis.get('caption', '')}\n"
            f"\n"
            f"{ai_analysis.get('hashtags', '')}"
        )
        artwork["alt_text"] = ai_analysis.get("alt_text", artwork.get("alt_text", ""))
    else:
        logger.info("Gemini analysis skipped or failed. Using fallback templates.")
        raw_desc = artwork.get('description', '')
        if not raw_desc:
            raw_desc = f"A classic piece titled '{clean_title}' by {clean_artist}, created in {artwork.get('date', 'unknown date')}."
        artist_hashtag = clean_artist.replace(" ", "").replace("-", "")
        hashtags = f"#Art #{artist_hashtag} #{artwork.get('museum', '').replace(' ', '')} #ClassicArt #ArtHistory"
        artwork["caption"] = (f"⠀\n{clean_title}\n\n{clean_artist} - {artwork.get('date', 'Unknown')}\n\n{artwork.get('museum', 'Unknown')}\n\n{raw_desc}\n\n{hashtags}")
        artwork["alt_text"] = f"Artwork: {clean_title} by {clean_artist}"

    if not args.dry_run:
        logger.info("media_prepared mode=single count=1")

    if args.dry_run:
        _log_dry_run_success("single", [artwork], [output_media_path])
        return resolution

    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    if args.image_url or os.environ.get("PUBLIC_IMAGE_URL"):
        logger.warning(
            "External single-image publishing override ignored; uploading the "
            "securely validated selected artwork."
        )
    if publication_id is None:
        raise RuntimeError("Single publication reservation returned no publication ID")
    try:
        media_upload = image_processor.upload_temp_media(
            output_media_path, publication_id
        )
    except Exception:
        _handle_pre_meta_staging_failure(
            [artwork["id"]], publication_id, ()
        )
        raise
    public_media_url = media_upload.public_url
    logger.info("upload_complete mode=single count=1")

    publish_attempt_started = False

    def before_publish(container_id: str, child_container_ids: tuple[str, ...]) -> None:
        nonlocal publish_attempt_started
        history_tracker.start_publication_attempt(
            [artwork["id"]], container_id, child_container_ids
        )
        publish_attempt_started = True

    try:
        media_id = instagram_poster.post_to_instagram_graph_api(
            media_url=public_media_url,
            caption=artwork["caption"],
            account_id=account_id,
            access_token=access_token,
            alt_text=artwork.get("alt_text"),
            media_type="IMAGE",
            before_publish=before_publish,
        )
        if not publish_attempt_started:
            history_tracker.mark_artwork_ambiguous(
                artwork["id"], "publisher_skipped_durable_boundary"
            )
            raise RuntimeError("Instagram publisher skipped the durable publication boundary")
        logger.info("publish_complete mode=single media_id=%s", media_id)
    except instagram_poster.InstagramPublishAmbiguousError:
        logger.error("Instagram publish result is ambiguous; preserving the duplicate lock.")
        try:
            history_tracker.mark_artwork_ambiguous(artwork["id"])
        except Exception:
            logger.exception("Failed to preserve the ambiguous single-post reservation.")
            raise
        raise
    except instagram_poster.InstagramAPIError as error:
        if publish_attempt_started:
            history_tracker.mark_publication_not_published(
                [artwork["id"]],
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
                history_tracker.mark_artwork_ambiguous(
                    artwork["id"],
                    f"unexpected_post_boundary_error:{type(error).__name__}",
                )
            except Exception:
                logger.exception("Failed to preserve the uncertain single-post reservation.")
        raise

    try:
        history_tracker.record_publish_response([artwork["id"]], media_id)
    except Exception:
        logger.exception(
            "Failed to record Instagram media ID before final history confirmation; "
            "the durable container lock remains."
        )
    history_tracker.confirm_artwork(artwork["id"], media_id)
    logger.info("history_confirmed mode=single count=1")
    
    if args.pinterest:
        logger.info("Triggering Pinterest cross-post...")
        title = f"{artwork['title']} by {artwork['artist']}"
        pinterest_poster.post_to_pinterest(
            image_url=public_media_url,
            title=title[:100],
            description=artwork["caption"],
            link=public_media_url
        )
    return resolution


def run_carousel_post(args):
    logger.info("Running carousel post logic...")
    posted_ids = history_tracker.get_posted_ids()
    color_tone = _get_grid_color_tone_for_run(args.dry_run)

    selection_run_seed = art_fetcher.resolve_selection_run_seed()
    theme_history = history_tracker.get_recent_carousel_theme_history()
    theme_registry = get_default_theme_registry()
    theme_selection = plan_carousel_theme(
        theme_registry,
        theme_history,
        run_seed=selection_run_seed.value,
        current_month=datetime.now(timezone.utc).month,
    )
    if hasattr(theme_selection, "ranked_themes"):
        ordered_candidates = theme_selection.ranked_themes(theme_registry)
        fallback_enabled = True
    else:
        # Compatibility for callers/tests supplying the former one-theme plan shape.
        ordered_candidates = (theme_selection.theme,)
        fallback_enabled = False

    attempted_themes: list[tuple[str, str]] = []
    acquisition_run_state = art_fetcher.AcquisitionRunState()
    artworks = None
    cover = None
    acquisition = None
    set_optimization = None
    theme_definition = None
    for attempt, candidate_theme in enumerate(
        ordered_candidates[:CAROUSEL_THEME_ATTEMPT_LIMIT],
        start=1,
    ):
        search_query = primary_search_query(candidate_theme)
        if attempted_themes:
            logger.info(
                "theme_fallback from=%s to=%s attempt=%s",
                attempted_themes[-1][0],
                candidate_theme.id,
                attempt,
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
            )
            if isinstance(selection, art_fetcher.ThemedArtworkSelection):
                candidate_artworks = list(selection.artworks)
                candidate_acquisition = selection.acquisition
                candidate_set_optimization = selection.set_optimization
            else:
                candidate_artworks = selection
                candidate_acquisition = None
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
            availability = getattr(error, "availability", None) or getattr(
                candidate_acquisition,
                "availability",
                None,
            )
            logger.info(
                "theme_unavailable theme=%s reason=%s eligible=%s target=%s attempt=%s",
                candidate_theme.id,
                reason,
                getattr(availability, "estimated_safe_pool", "unknown"),
                getattr(availability, "target", "unknown"),
                attempt,
            )
            continue

        artworks = candidate_artworks
        cover = candidate_cover
        acquisition = candidate_acquisition
        set_optimization = candidate_set_optimization
        theme_definition = candidate_theme
        break

    if theme_definition is None or artworks is None or cover is None:
        raise art_fetcher.CarouselThemeAvailabilityError(attempted_themes)

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
    ai_analysis = gemini_ai.analyze_carousel(
        theme_definition.title,
        artworks,
        **analysis_context,
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

    # All selection, copy, validation, and rendering has succeeded. Reserve the
    # all variable-length canonical IDs together before any Instagram media operation.
    publication_id = history_tracker.reserve_carousel(
        dict(plan.cover.artwork),
        [dict(art) for art in plan.featured_artworks],
        theme_id=plan.theme.id,
        theme_family=plan.theme.family.value,
        carousel_format=plan.theme.format.value,
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
    parser.add_argument("--force-carousel", action="store_true", help="Force the bot to post a carousel")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in ProductionMode],
        default=ProductionMode.AUTO.value,
        help="Select single/carousel explicitly, or retain legacy UTC-hour auto mode",
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
    parser.add_argument(
        "--image-url",
        type=str,
        help="Deprecated compatibility option; external publish overrides are ignored",
    )
    parser.add_argument("--pinterest", action="store_true", help="Also cross-post to Pinterest")
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

        if mode is ProductionMode.CAROUSEL:
            run_carousel_post(args)
        else:
            resolution = run_single_post(args)
            if (
                not args.dry_run
                and resolution.result
                is SinglePostResolutionCode.NO_SINGLE_POST_PUBLISHABLE_CANDIDATE
            ):
                logger.info(
                    "production_no_publish mode=single reason=%s",
                    resolution.result.value,
                )
                return 0

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
