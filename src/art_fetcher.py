import logging
import random
import os
import uuid
import hashlib
import secrets
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Dict, Any, Iterator, List
from src.museums import (
    AICAdapter,
    ClevelandAdapter,
    EuropeanaAdapter,
    MetAdapter,
    RijksmuseumAdapter,
    SmithsonianAdapter,
)
from src.quality_filter import (
    ImageValidationResult,
    calculate_measurement_coverage,
    calculate_quality_score,
    validate_and_download_image_with_metadata,
)
from src.aic_image_policy import (
    ImageDownloadPurpose,
    is_aic_iiif_url,
)
from src import history_tracker
from src import content_diversity
from src.region import normalize_region
from src.carousel_themes import CarouselThemeDefinition, ThemeEvidenceMode
from src.artwork_visual_features import extract_visual_features, features_from_dimensions
from src.single_post_diversity import (
    SinglePostDiversityScore,
    candidate_diversity_features,
    history_metadata as single_diversity_history_metadata,
    recent_single_publications,
    score_single_post_diversity,
)
from src.carousel_set_optimizer import (
    FINALIST_POOL_SIZE,
    CarouselSetOptimizationResult,
    normalize_known_identity,
    optimize_carousel_set,
)
from src.carousel_policy import MAX_FEATURED_WORKS, MIN_FEATURED_WORKS, MIN_TOTAL_SLIDES
from src.theme_acquisition import (
    AcquisitionRunState,
    CarouselThemeAvailabilityError as BaseCarouselThemeAvailabilityError,
    CarouselCandidateScoreBreakdown,
    DEFAULT_MIN_THEME_RELEVANCE,
    PREFERRED_PREFLIGHT_TARGET,
    ThemeAcquisitionPolicy,
    ThemeAcquisitionResult,
    acquire_theme_candidates,
    evaluate_theme_relevance,
)
import config

logger = logging.getLogger(__name__)

# Default weights if not specified in config
DEFAULT_WEIGHTS = {
    "aic": 15,
    "rijksmuseum": 15,
    "met": 15,
    "cleveland": 15,
    "smithsonian": 15,
    "europeana": 15,
}

# Legacy query-only selection remains a general helper. The structured editorial
# carousel path below enforces the product's narrower 3–8 featured-work contract.
MIN_CAROUSEL_ITEMS = 2
MAX_CAROUSEL_ITEMS = 10
SINGLE_DIVERSITY_FINALIST_TARGET = 12
# Hybrid prequalification never authorizes an unbounded image sweep. At most 40
# promising, evidence-ranked candidates cross the existing secure download boundary;
# inspection stops earlier once the 24-item finalist headroom is full.
MAX_FINALIST_VALIDATION_ATTEMPTS = 40
SELECTION_SEED_ENV = "ARTFOLIO_SELECTION_SEED"


class CarouselSelectionError(RuntimeError):
    """Raised when a complete, validated carousel cannot be assembled."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "carousel_insufficient_candidates",
        availability=None,
    ):
        self.reason = reason
        self.availability = availability
        RuntimeError.__init__(self, message)


class CarouselThemeAvailabilityError(CarouselSelectionError, BaseCarouselThemeAvailabilityError):
    """Specific bounded-fallback failure that remains selection-error compatible."""

    def __init__(self, attempts):
        self.attempts = tuple(attempts)
        summary = "; ".join(f"{theme_id}={reason}" for theme_id, reason in self.attempts)
        CarouselSelectionError.__init__(
            self,
            f"No viable carousel theme within the fallback limit: {summary}",
            reason="theme_attempt_limit_exhausted",
        )


def get_museum_adapters():
    """Return the trusted museum adapter registry used by every art pipeline."""
    return [
        AICAdapter(),
        ClevelandAdapter(),
        MetAdapter(),
        RijksmuseumAdapter(),
        SmithsonianAdapter(),
        EuropeanaAdapter(),
    ]


def _museum_adapters():
    """Compatibility hook retained for target-side tests and callers."""
    return get_museum_adapters()


@dataclass(frozen=True)
class SelectionRunSeed:
    value: str
    source: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()[:12]


def resolve_selection_run_seed(
    environment: dict[str, str] | None = None,
    entropy_source=secrets.token_hex,
) -> SelectionRunSeed:
    """Resolve a per-selection seed without changing global random state."""
    environment = os.environ if environment is None else environment
    explicit_seed = environment.get(SELECTION_SEED_ENV, "").strip()
    if explicit_seed:
        return SelectionRunSeed(explicit_seed, "explicit")

    github_run_id = environment.get("GITHUB_RUN_ID", "").strip()
    if github_run_id:
        return SelectionRunSeed(github_run_id, "github_run")

    return SelectionRunSeed(entropy_source(16), "local")


def derive_selection_rng(run_seed: str, namespace: str) -> random.Random:
    """Build an isolated, reproducible RNG without changing global state."""
    material = f"{run_seed}\x1f{namespace}".encode("utf-8")
    stable_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")
    return random.Random(stable_seed)


def _museum_adapter_rng(
    selection_run_seed: SelectionRunSeed,
    source_id: str,
    stage_name: str,
    query: str | None,
) -> random.Random:
    """Derive one adapter-local RNG for a logical candidate-pool request."""
    namespace = f"museum:{source_id}:stage:{stage_name}:query:{query or ''}"
    return derive_selection_rng(selection_run_seed.value, namespace)


def calculate_serendipity_bonus(run_seed: str, candidate_id: str) -> float:
    """Generate an order-independent 0-5 selection bonus for one candidate."""
    return derive_selection_rng(run_seed, candidate_id).uniform(0.0, 5.0)


@dataclass(frozen=True)
class SelectionScoreBreakdown:
    """The already-calculated selection adjustments for one single-post candidate."""

    quality_score: float
    museum_adjustment: float
    visual_adjustment: float
    discovery_adjustment: float
    serendipity_adjustment: float
    regional_adjustment: float = 0.0
    single_diversity: SinglePostDiversityScore | None = None

    @property
    def selection_score(self) -> float:
        diversity_adjustment = (
            self.single_diversity.total
            if self.single_diversity is not None
            else self.visual_adjustment
        )
        return (
            self.quality_score
            + self.museum_adjustment
            + diversity_adjustment
            + self.discovery_adjustment
            + self.serendipity_adjustment
            + self.regional_adjustment
        )


@dataclass
class SelectionObservability:
    """Compact, per-selection counters for INFO summaries and DEBUG diagnostics."""

    raw_candidates: int = 0
    rights_safe: int = 0
    history_new: int = 0
    quality_pass: int = 0
    downloads: int = 0
    selected: int = 0
    images_validated: int = 0
    visually_scored: int = 0
    aic_fallback_attempted: int = 0
    aic_fallback_recovered: int = 0
    rejections: Counter = field(default_factory=Counter)

    def reject(self, reason: str) -> None:
        self.rejections[reason] += 1

    def rejection_count(self, reason: str) -> int:
        return self.rejections[reason]

    def rejection_fields(self) -> str:
        return ",".join(f"{reason}:{count}" for reason, count in sorted(self.rejections.items())) or "none"

    def record_image_validation(self, result: ImageValidationResult) -> None:
        if result.valid:
            self.images_validated += 1
        if result.aic_fallback_attempted:
            self.aic_fallback_attempted += 1
            if result.aic_fallback_recovered:
                self.aic_fallback_recovered += 1


def _percentile(values: list[float], percentile: float) -> float | None:
    """Return a deterministic linearly interpolated percentile."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return round(
        ordered[lower_index]
        + (ordered[upper_index] - ordered[lower_index]) * fraction,
        2,
    )


@dataclass(frozen=True)
class ThemedArtworkSelection:
    artworks: tuple[Dict[str, Any], ...]
    acquisition: ThemeAcquisitionResult
    set_optimization: CarouselSetOptimizationResult | None = None


def _normalized_diversity_key(value: object) -> str | None:
    return normalize_known_identity(value)


def _apply_downloaded_image_measurements(
    candidate,
    validation_result: ImageValidationResult,
    museum_weights: dict,
    preserve_selection_adjustments: bool,
) -> float:
    """Recalculate quality using dimensions decoded by the secure download."""
    previous_quality_score = candidate.quality_score
    previous_selection_score = candidate.selection_score
    candidate.image_width = validation_result.width
    candidate.image_height = validation_result.height
    candidate.quality_score = calculate_quality_score(candidate, museum_weights)
    candidate.measurement_coverage = calculate_measurement_coverage(candidate)

    breakdown = getattr(candidate, "_selection_breakdown", None)
    theme_breakdown = getattr(candidate, "_theme_candidate_breakdown", None)
    if preserve_selection_adjustments and isinstance(
        theme_breakdown,
        CarouselCandidateScoreBreakdown,
    ):
        updated_theme_breakdown = replace(
            theme_breakdown,
            technical_quality=candidate.quality_score * 0.30,
        )
        candidate._theme_candidate_breakdown = updated_theme_breakdown
        candidate.selection_score = updated_theme_breakdown.total
    elif preserve_selection_adjustments and isinstance(breakdown, SelectionScoreBreakdown):
        updated_breakdown = replace(breakdown, quality_score=candidate.quality_score)
        candidate._selection_breakdown = updated_breakdown
        candidate.selection_score = updated_breakdown.selection_score
    elif preserve_selection_adjustments and previous_selection_score is not None:
        candidate.selection_score = candidate.quality_score + (previous_selection_score - previous_quality_score)
    else:
        candidate.selection_score = candidate.quality_score

    return candidate.quality_score


def _select_carousel_candidates(
    candidates,
    count: int,
    selected,
    selected_ids: set,
    validated_paths: dict,
    artist_cap: int,
    museum_cap: int,
    region_cap: int | None,
    museum_weights: dict,
    min_score: float,
    observability: SelectionObservability,
    preserve_selection_score: bool = False,
) -> None:
    """Add validated candidates that fit the current internal diversity caps."""
    artist_counts = {}
    museum_counts = {}
    region_counts = {}
    for candidate in selected:
        artist_key = _normalized_diversity_key(candidate.artist_name)
        museum_key = _normalized_diversity_key(candidate.museum_name)
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if museum_key:
            museum_counts[museum_key] = museum_counts.get(museum_key, 0) + 1
        region = normalize_region(getattr(candidate, "region", "unknown"))
        if region != "unknown":
            region_counts[region] = region_counts.get(region, 0) + 1

    for candidate in candidates:
        if len(selected) == count:
            break

        candidate_id = candidate.canonical_id
        if candidate_id in selected_ids:
            observability.reject("duplicate_candidate")
            logger.debug("carousel_candidate_rejected candidate=%s reason=duplicate_candidate", candidate_id)
            continue

        artist_key = _normalized_diversity_key(candidate.artist_name)
        museum_key = _normalized_diversity_key(candidate.museum_name)
        region = normalize_region(getattr(candidate, "region", "unknown"))
        if artist_key and artist_counts.get(artist_key, 0) >= artist_cap:
            observability.reject("internal_artist_limit")
            logger.debug("carousel_candidate_rejected candidate=%s reason=internal_artist_limit", candidate_id)
            continue
        if museum_key and museum_counts.get(museum_key, 0) >= museum_cap:
            observability.reject("internal_museum_limit")
            logger.debug("carousel_candidate_rejected candidate=%s reason=internal_museum_limit", candidate_id)
            continue
        if region != "unknown" and region_cap is not None and region_counts.get(region, 0) >= region_cap:
            observability.reject("internal_region_limit")
            logger.debug("carousel_candidate_rejected candidate=%s reason=internal_region_limit region=%s", candidate_id, region)
            continue

        if candidate_id not in validated_paths:
            observability.downloads += 1
            local_path = os.path.join(config.DATA_DIR, f"carousel_candidate_{uuid.uuid4().hex}.jpg")
            validation_result = validate_and_download_image_with_metadata(candidate.image_url, local_path)
            if validation_result.valid:
                measured_score = _apply_downloaded_image_measurements(
                    candidate,
                    validation_result,
                    museum_weights,
                    preserve_selection_adjustments=preserve_selection_score,
                )
                if measured_score >= min_score:
                    validated_paths[candidate_id] = local_path
                else:
                    observability.reject("post_quality_below_threshold")
                    logger.info(
                        "carousel_candidate_rejected candidate=%s reason=post_quality_below_threshold measured_quality=%.1f minimum=%s",
                        candidate.canonical_id,
                        measured_score,
                        min_score,
                    )
                    os.remove(local_path)
                    validated_paths[candidate_id] = None
            else:
                observability.reject("image_validation_failed")
                logger.debug(
                    "carousel_candidate_rejected candidate=%s reason=image_validation_failed validation_reason=%s",
                    candidate_id,
                    validation_result.reason,
                )
                validated_paths[candidate_id] = None
        if validated_paths[candidate_id] is None:
            continue

        selected.append(candidate)
        selected_ids.add(candidate_id)
        observability.selected += 1
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if museum_key:
            museum_counts[museum_key] = museum_counts.get(museum_key, 0) + 1
        if region != "unknown":
            region_counts[region] = region_counts.get(region, 0) + 1

    return None

def iter_single_post_candidates(
    posted_ids: set,
    *,
    max_candidates: int,
    selection_run_seed: SelectionRunSeed | None = None,
) -> Iterator[Dict[str, Any]]:
    """
    Yield securely validated single-post candidates in deterministic score order.

    Orchestrates the Image-First pipeline:
    1. Fetches candidates from all museums.
    2. Normalizes them.
    3. Filters out duplicates.
    4. Scores them based on quality.
    5. Validates the top candidate's image.
    6. Yields validated alternatives without downloading any candidate twice.
    """
    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")

    adapters = _museum_adapters()
    
    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_score = getattr(config, "MIN_QUALITY_SCORE", 50)
    selection_run_seed = selection_run_seed or resolve_selection_run_seed()
    logger.info(
        "selection_seed source=%s fingerprint=%s",
        selection_run_seed.source,
        selection_run_seed.fingerprint,
    )
    
    observability = SelectionObservability()
    all_candidates = []
    
    # 1. Fetch from all adapters
    for adapter in adapters:
        logger.info(f"Fetching candidates from {adapter.source_id}...")
        logger.debug(
            "museum_fetch source=%s stage=single_post seed_source=%s seed_fingerprint=%s",
            adapter.source_id,
            selection_run_seed.source,
            selection_run_seed.fingerprint,
        )
        try:
            candidates = adapter.fetch_candidates(
                limit=15,
                rng=_museum_adapter_rng(selection_run_seed, adapter.source_id, "single_post", None),
            )
        except Exception:
            logger.exception(
                "Museum source %s failed; continuing with remaining sources.",
                adapter.source_id,
            )
            continue
        all_candidates.extend(candidates)

    logger.info(f"Total raw candidates fetched: {len(all_candidates)}")
    
    # 2. Filter duplicates
    observability.raw_candidates = len(all_candidates)
    new_candidates = []
    seen_candidate_ids = set()
    for candidate in all_candidates:
        if not candidate.has_confirmed_rights:
            observability.reject("rights_unconfirmed")
            logger.debug("single_candidate_rejected candidate=%s reason=rights_unconfirmed", candidate.canonical_id)
            continue
        observability.rights_safe += 1
        if candidate.canonical_id in posted_ids:
            observability.reject("history_duplicate")
            logger.debug("single_candidate_rejected candidate=%s reason=history_duplicate", candidate.canonical_id)
            continue
        if candidate.canonical_id in seen_candidate_ids:
            observability.reject("duplicate_candidate")
            logger.debug(
                "single_candidate_rejected candidate=%s reason=duplicate_candidate",
                candidate.canonical_id,
            )
            continue
        seen_candidate_ids.add(candidate.canonical_id)
        new_candidates.append(candidate)
    observability.history_new = len(new_candidates)
    logger.info(f"Candidates after duplicate filter: {len(new_candidates)}")
    
    if not new_candidates:
        logger.info(
            "selection_summary raw=%s rights_safe=%s history_new=0 quality_pass=0 downloads=0 selected=none rejections=%s",
            observability.raw_candidates,
            observability.rights_safe,
            observability.rejection_fields(),
        )
        raise RuntimeError("No new public domain artworks found across any museum!")

    # 3. Score candidates with Diversity Penalty
    recent_history = history_tracker.get_recent_history()
    recent_single_history = recent_single_publications(recent_history)
        
    scored_candidates = []
    for c in new_candidates:
        base_score = calculate_quality_score(c, museum_weights)
        c.quality_score = base_score
        c.measurement_coverage = calculate_measurement_coverage(c)

        # Calculate diversity penalties/bonuses
        museum_penalty = content_diversity.analyze_museum_diversity(
            c.museum_name,
            recent_single_history,
        )
        features = content_diversity.get_candidate_metadata_features(c)
        single_features = candidate_diversity_features(c)
        single_diversity = score_single_post_diversity(
            single_features,
            recent_single_history,
        )
        discovery_bonus = content_diversity.analyze_discovery_score(
            features,
            base_score,
            recent_single_history,
        )
        serendipity_bonus = calculate_serendipity_bonus(selection_run_seed.value, c.canonical_id)
        regional_adjustment = content_diversity.analyze_regional_diversity(
            c.region,
            recent_single_history,
        )
        
        breakdown = SelectionScoreBreakdown(
            quality_score=base_score,
            museum_adjustment=museum_penalty,
            visual_adjustment=0.0,
            discovery_adjustment=discovery_bonus,
            serendipity_adjustment=serendipity_bonus,
            regional_adjustment=regional_adjustment,
            single_diversity=single_diversity,
        )
        c.selection_score = breakdown.selection_score

        # Attach features for later use
        c._diversity_features = features
        c._single_diversity_features = single_features
        c._selection_breakdown = breakdown

        if c.quality_score >= min_score:
            scored_candidates.append(c)
        else:
            observability.reject("pre_quality_below_threshold")
            logger.debug(
                "single_candidate_rejected candidate=%s reason=pre_quality_below_threshold quality=%.2f selection=%.2f",
                c.canonical_id,
                c.quality_score,
                c.selection_score,
            )
            
    # Sort by score descending
    scored_candidates.sort(
        key=lambda candidate: (-candidate.selection_score, candidate.canonical_id)
    )
    observability.quality_pass = len(scored_candidates)
    logger.info(f"Candidates passing quality threshold ({min_score}): {len(scored_candidates)}")
    
    if not scored_candidates:
        logger.info(
            "selection_summary raw=%s rights_safe=%s history_new=%s quality_pass=0 downloads=0 selected=none rejections=%s",
            observability.raw_candidates,
            observability.rights_safe,
            observability.history_new,
            observability.rejection_fields(),
        )
        raise RuntimeError(f"No candidates passed the quality threshold of {min_score}!")
        
    # 4. Securely validate a bounded finalist pool. Most adapters do not expose
    # trustworthy display dimensions, tone, or coarse color before download, so
    # the final single ranking is calculated once from these measured files.
    validated_finalists = []
    temporary_paths: list[str] = []
    for index, best_candidate in enumerate(scored_candidates):
        if (
            index >= SINGLE_DIVERSITY_FINALIST_TARGET
            and len(validated_finalists) >= max_candidates
        ):
            break
        pre_quality_score = best_candidate.quality_score
        observability.downloads += 1
        logger.debug(
            "single_candidate_download candidate=%s pre_quality=%.2f pre_coverage=%.1f selection=%.2f",
            best_candidate.canonical_id,
            pre_quality_score,
            best_candidate.measurement_coverage,
            best_candidate.selection_score,
        )
        
        candidate_path = os.path.join(
            os.path.dirname(os.path.abspath(config.OUTPUT_RAW_IMAGE_PATH)),
            f"output_raw_{uuid.uuid4().hex}.jpg",
        )
        temporary_paths.append(candidate_path)
        validation_result = validate_and_download_image_with_metadata(
            best_candidate.image_url,
            candidate_path,
        )
        if validation_result.valid:
            measured_score = _apply_downloaded_image_measurements(
                best_candidate,
                validation_result,
                museum_weights,
                preserve_selection_adjustments=False,
            )
            if measured_score < min_score:
                observability.reject("post_quality_below_threshold")
                logger.info(
                    "single_candidate_rejected candidate=%s reason=post_quality_below_threshold pre_quality=%.2f measured_quality=%.2f coverage=%.1f",
                    best_candidate.canonical_id,
                    pre_quality_score,
                    measured_score,
                    best_candidate.measurement_coverage,
                )
                try:
                    os.remove(candidate_path)
                except FileNotFoundError:
                    pass
                continue
            try:
                visual_features = extract_visual_features(candidate_path)
                observability.visually_scored += 1
            except (OSError, SyntaxError, ValueError):
                logger.warning(
                    "single_visual_features_unavailable candidate=%s",
                    best_candidate.canonical_id,
                )
                visual_features = features_from_dimensions(
                    best_candidate.image_width,
                    best_candidate.image_height,
                )
            published_features = candidate_diversity_features(
                best_candidate,
                visual_features,
            )
            previous_breakdown = best_candidate._selection_breakdown
            breakdown = replace(
                previous_breakdown,
                quality_score=measured_score,
                discovery_adjustment=content_diversity.analyze_discovery_score(
                    getattr(best_candidate, "_diversity_features", {}),
                    measured_score,
                    recent_single_history,
                ),
                single_diversity=score_single_post_diversity(
                    published_features,
                    recent_single_history,
                ),
            )
            best_candidate._single_diversity_features = published_features
            best_candidate._selection_breakdown = breakdown
            best_candidate.selection_score = breakdown.selection_score
            diversity = breakdown.single_diversity
            logger.debug(
                "single_candidate_ranked candidate=%s source=%s region=%s pre_quality=%.2f quality=%.2f coverage=%.1f dimensions=%sx%s museum=%+.2f region_adjustment=%+.2f orientation=%+.2f artist=%+.2f visual_category=%+.2f discovery=%+.2f serendipity=%+.2f selection=%.2f",
                best_candidate.canonical_id,
                best_candidate.source,
                normalize_region(best_candidate.region),
                pre_quality_score,
                best_candidate.quality_score,
                best_candidate.measurement_coverage,
                best_candidate.image_width,
                best_candidate.image_height,
                breakdown.museum_adjustment,
                breakdown.regional_adjustment,
                diversity.orientation,
                diversity.artist,
                diversity.visual_category,
                breakdown.discovery_adjustment,
                breakdown.serendipity_adjustment,
                best_candidate.selection_score,
            )
            logger.debug(
                "single_diversity_history candidate=%s orientation_count=%s orientation_streak=%s artist_count=%s immediate_artist=%s semantic_count=%s semantic_streak=%s tone_count=%s color_count=%s fingerprint_streak=%s",
                best_candidate.canonical_id,
                diversity.orientation_count,
                diversity.orientation_streak,
                diversity.artist_count,
                diversity.immediate_artist_repeat,
                diversity.semantic_count,
                diversity.semantic_streak,
                diversity.tone_count,
                diversity.color_count,
                diversity.fingerprint_streak,
            )
            validated_finalists.append(
                (best_candidate, candidate_path, published_features)
            )
        else:
            observability.reject("image_validation_failed")
            logger.info(
                "single_candidate_rejected candidate=%s reason=image_validation_failed validation_reason=%s",
                best_candidate.canonical_id,
                validation_result.reason,
            )

    validated_finalists.sort(
        key=lambda item: (-item[0].selection_score, item[0].canonical_id)
    )
    if not validated_finalists:
        for path in temporary_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        logger.info(
            "selection_summary raw=%s rights_safe=%s history_new=%s quality_pass=%s downloads=%s selected=none rejections=%s",
            observability.raw_candidates,
            observability.rights_safe,
            observability.history_new,
            observability.quality_pass,
            observability.downloads,
            observability.rejection_fields(),
        )
        raise RuntimeError("All top candidates failed image validation (Hard Reject)!")

    # 5. Yield at most the recovery budget in final deterministic score order.
    try:
        for best_candidate, candidate_path, published_features in validated_finalists:
            os.replace(candidate_path, config.OUTPUT_RAW_IMAGE_PATH)
            breakdown = best_candidate._selection_breakdown
            diversity = breakdown.single_diversity
            observability.selected += 1
            logger.info(
                "selection_summary raw=%s rights_safe=%s history_new=%s quality_pass=%s downloads=%s selected=%s rejections=%s",
                observability.raw_candidates,
                observability.rights_safe,
                observability.history_new,
                observability.quality_pass,
                observability.downloads,
                best_candidate.canonical_id,
                observability.rejection_fields(),
            )

            # Return dict format expected by history_tracker and Gemini.
            features = getattr(best_candidate, "_diversity_features", {})
            selection_breakdown = {
                "quality": best_candidate.quality_score,
                "museum": breakdown.museum_adjustment,
                "region": breakdown.regional_adjustment,
                "orientation": diversity.orientation,
                "artist": diversity.artist,
                "visual_category": diversity.visual_category,
                "discovery": breakdown.discovery_adjustment,
                "serendipity": breakdown.serendipity_adjustment,
                "final": best_candidate.selection_score,
            }
            yield {
                "id": best_candidate.canonical_id, # Canonical ID for duplicate prevention
                "title": best_candidate.title,
                "artist": best_candidate.artist_name,
                "date": best_candidate.creation_date,
                "museum": best_candidate.museum_name,
                "image_url": best_candidate.image_url,
                "local_image_path": config.OUTPUT_RAW_IMAGE_PATH, # Pass the local path down
                "image_width": best_candidate.image_width,
                "image_height": best_candidate.image_height,
                "alt_text": f"{best_candidate.title} by {best_candidate.artist_name}, {best_candidate.creation_date}",
                # Passed down for prompt/metadata context
                "medium": best_candidate.medium,
                "classification": best_candidate.classification,
                "quality_score": best_candidate.quality_score,
                "measurement_coverage": best_candidate.measurement_coverage,
                "selection_score": best_candidate.selection_score,
                "_single_selection_breakdown": selection_breakdown,
                "description": best_candidate.description,
                # Passed down to reserve_artwork
                "visual_category": published_features.visual_category.semantic_family.value.casefold(),
                "period": features.get("period", "unknown"),
                "region": normalize_region(best_candidate.region),
                **single_diversity_history_metadata(published_features),
            }
            if observability.selected >= max_candidates:
                return
    finally:
        for path in temporary_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    if observability.selected:
        logger.info(
            "selection_candidate_pool_exhausted yielded=%s downloads=%s rejections=%s",
            observability.selected,
            observability.downloads,
            observability.rejection_fields(),
        )
        return
    return


def fetch_random_artwork(posted_ids: set) -> Dict[str, Any]:
    """Return the highest-ranked securely validated single-post candidate."""
    return next(iter_single_post_candidates(posted_ids, max_candidates=1))


def fetch_single_artwork(posted_ids: set) -> Dict[str, Any]:
    """Backward-compatible entry point for the canonical single selector."""
    return fetch_random_artwork(posted_ids)

def _fetch_legacy_themed_artworks(
    posted_ids: set,
    theme: str,
    count: int,
    color_tone: str,
    selection_run_seed: SelectionRunSeed | None = None,
) -> List[Dict[str, Any]]:
    """Return exactly ``count`` validated, internally diverse carousel artworks.

    A partial carousel is never returned: callers receive either a complete
    selection or ``CarouselSelectionError`` before any history reservation.
    """
    if not MIN_CAROUSEL_ITEMS <= count <= MAX_CAROUSEL_ITEMS:
        raise ValueError(
            f"Carousel count must be between {MIN_CAROUSEL_ITEMS} and {MAX_CAROUSEL_ITEMS}; got {count}."
        )

    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_score = getattr(config, "MIN_QUALITY_SCORE", 50)
    selection_run_seed = selection_run_seed or resolve_selection_run_seed()
    logger.info(
        "selection_seed source=%s fingerprint=%s",
        selection_run_seed.source,
        selection_run_seed.fingerprint,
    )
    candidates_by_id = {}
    selected = []
    selected_ids = set()
    validated_paths = {}
    observability = SelectionObservability()
    fallback_stages = []

    def add_candidates(query: str, limit: int, stage_name: str) -> None:
        fallback_stages.append(stage_name)
        logger.info("Carousel selection stage %s: query=%r, limit=%s", stage_name, query, limit)
        for adapter in _museum_adapters():
            logger.debug(
                "museum_fetch source=%s stage=%s seed_source=%s seed_fingerprint=%s",
                adapter.source_id,
                stage_name,
                selection_run_seed.source,
                selection_run_seed.fingerprint,
            )
            for candidate in adapter.fetch_candidates(
                limit=limit,
                query=query,
                rng=_museum_adapter_rng(selection_run_seed, adapter.source_id, stage_name, query),
            ):
                observability.raw_candidates += 1
                if candidate.canonical_id in posted_ids:
                    observability.reject("history_duplicate")
                    logger.debug("carousel_candidate_rejected candidate=%s reason=history_duplicate", candidate.canonical_id)
                    continue
                if not candidate.has_confirmed_rights:
                    observability.reject("rights_unconfirmed")
                    logger.debug("carousel_candidate_rejected candidate=%s reason=rights_unconfirmed", candidate.canonical_id)
                    continue
                observability.rights_safe += 1
                if not candidate.image_url:
                    observability.reject("missing_image_url")
                    logger.debug("carousel_candidate_rejected candidate=%s reason=missing_image_url", candidate.canonical_id)
                    continue
                candidate.quality_score = calculate_quality_score(candidate, museum_weights)
                candidate.measurement_coverage = calculate_measurement_coverage(candidate)
                candidate.selection_score = candidate.quality_score
                if candidate.quality_score < min_score:
                    observability.reject("pre_quality_below_threshold")
                    logger.debug("carousel_candidate_rejected candidate=%s reason=pre_quality_below_threshold quality=%.2f", candidate.canonical_id, candidate.quality_score)
                    continue
                observability.quality_pass += 1

                existing = candidates_by_id.get(candidate.canonical_id)
                if existing is not None:
                    observability.reject("duplicate_candidate")
                if existing is None or candidate.quality_score > existing.quality_score:
                    candidates_by_id[candidate.canonical_id] = candidate
        observability.history_new = len(candidates_by_id)

    def ranked_candidates():
        return sorted(
            candidates_by_id.values(),
            key=lambda candidate: (-candidate.quality_score, candidate.canonical_id),
        )

    strict_museum_cap = min(3, count)
    relaxed_museum_cap = min(4, count)
    strict_region_cap = min(2, count)
    relaxed_region_cap = min(3, count)

    add_candidates(f"{color_tone} {theme}", 25, "tone_and_theme")
    _select_carousel_candidates(
        ranked_candidates(), count, selected, selected_ids, validated_paths, 1, strict_museum_cap, strict_region_cap,
        museum_weights, min_score, observability,
    )

    if len(selected) < count:
        add_candidates(theme, 25, "theme_only")
        _select_carousel_candidates(
            ranked_candidates(), count, selected, selected_ids, validated_paths, 1, strict_museum_cap, strict_region_cap,
            museum_weights, min_score, observability,
        )

    if len(selected) < count:
        logger.info(
            "Carousel selection relaxing internal caps: artist<=2, museum<=%s, region<=%s",
            relaxed_museum_cap,
            relaxed_region_cap,
        )
        _select_carousel_candidates(
            ranked_candidates(), count, selected, selected_ids, validated_paths, 2, relaxed_museum_cap, relaxed_region_cap,
            museum_weights, min_score, observability,
        )

    if len(selected) < count:
        add_candidates(theme, 50, "expanded_theme")
        _select_carousel_candidates(
            ranked_candidates(), count, selected, selected_ids, validated_paths, 1, strict_museum_cap, strict_region_cap,
            museum_weights, min_score, observability,
        )
        if len(selected) < count:
            _select_carousel_candidates(
                ranked_candidates(), count, selected, selected_ids, validated_paths, 2, relaxed_museum_cap, relaxed_region_cap,
                museum_weights, min_score, observability,
            )

    if len(selected) != count:
        for local_path in validated_paths.values():
            if local_path:
                try:
                    os.remove(local_path)
                except FileNotFoundError:
                    pass
        logger.info(
            "carousel_selection_summary requested=%s selected=%s safe_candidates=%s validated_images=%s stages=%s downloads=%s rejections=%s result=carousel_insufficient_candidates",
            count,
            len(selected),
            len(candidates_by_id),
            sum(path is not None for path in validated_paths.values()),
            ",".join(fallback_stages),
            observability.downloads,
            observability.rejection_fields(),
        )
        raise CarouselSelectionError(
            "Unable to build carousel: "
            f"requested={count} safe_candidates={len(candidates_by_id)} "
            f"validated_images={sum(path is not None for path in validated_paths.values())} selected={len(selected)}"
        )

    final_artworks = []
    for index, candidate in enumerate(selected):
        local_path = os.path.join(config.DATA_DIR, f"output_raw_{index}.jpg")
        os.replace(validated_paths[candidate.canonical_id], local_path)
        validated_paths[candidate.canonical_id] = None
        features = content_diversity.get_candidate_metadata_features(candidate)
        final_artworks.append({
            "id": candidate.canonical_id,
            "title": candidate.title,
            "artist": candidate.artist_name,
            "date": candidate.creation_date,
            "museum": candidate.museum_name,
            "image_url": candidate.image_url,
            "local_image_path": local_path,
            "image_width": candidate.image_width,
            "image_height": candidate.image_height,
            "alt_text": f"{candidate.title} by {candidate.artist_name}, {candidate.creation_date}",
            "medium": candidate.medium,
            "classification": candidate.classification,
            "quality_score": candidate.quality_score,
            "measurement_coverage": candidate.measurement_coverage,
            "selection_score": candidate.selection_score,
            "description": candidate.description,
            "visual_category": features["visual_category"],
            "period": features["period"],
            "region": normalize_region(candidate.region),
        })

    source_distribution = Counter(candidate.source for candidate in selected)
    region_distribution = Counter(
        normalize_region(candidate.region) for candidate in selected
    )
    logger.info(
        "carousel_selection_summary requested=%s selected=%s safe_candidates=%s validated_images=%s stages=%s downloads=%s artists_unique=%s source_distribution=%s region_distribution=%s rejections=%s result=selected",
        count,
        len(selected),
        len(candidates_by_id),
        sum(path is not None for path in validated_paths.values()),
        ",".join(fallback_stages),
        observability.downloads,
        len({_normalized_diversity_key(candidate.artist_name) for candidate in selected if _normalized_diversity_key(candidate.artist_name)}),
        ",".join(f"{source}:{source_distribution[source]}" for source in sorted(source_distribution)) or "none",
        ",".join(f"{region}:{region_distribution[region]}" for region in sorted(region_distribution)) or "none",
        observability.rejection_fields(),
    )
    return final_artworks


def _theme_artwork_dict(candidate, local_path: str) -> Dict[str, Any]:
    features = content_diversity.get_candidate_metadata_features(candidate.artwork)
    evidence = candidate.evidence
    breakdown = evidence.relevance_breakdown
    return {
        "id": candidate.artwork.canonical_id,
        "title": candidate.artwork.title,
        "artist": candidate.artwork.artist_name,
        "date": candidate.artwork.creation_date,
        "museum": candidate.artwork.museum_name,
        "image_url": candidate.artwork.image_url,
        "local_image_path": local_path,
        "image_width": candidate.artwork.image_width,
        "image_height": candidate.artwork.image_height,
        "alt_text": (
            f"{candidate.artwork.title} by {candidate.artwork.artist_name}, "
            f"{candidate.artwork.creation_date}"
        ),
        "medium": candidate.artwork.medium,
        "classification": candidate.artwork.classification,
        "style_or_period": candidate.artwork.style_or_period,
        "quality_score": candidate.artwork.quality_score,
        "measurement_coverage": candidate.artwork.measurement_coverage,
        "selection_score": candidate.artwork.selection_score,
        "theme_relevance_score": evidence.theme_relevance_score,
        "theme_relevance_breakdown": {
            "primary_query": breakdown.primary_query,
            "secondary_query": breakdown.secondary_query,
            "repeated_queries": breakdown.repeated_queries,
            "required": breakdown.required,
            "preferred": breakdown.preferred,
            "title_evidence": breakdown.title_evidence,
            "context_evidence": breakdown.context_evidence,
            "description_evidence": breakdown.description_evidence,
            "format_target": breakdown.format_target,
            "visual_target": breakdown.visual_target,
            "visual_support": breakdown.visual_support,
            "excluded": breakdown.excluded,
            "total": breakdown.total,
        },
        "matched_queries": tuple(hit.query for hit in evidence.matched_queries),
        "required_matches": evidence.required_matches,
        "preferred_matches": evidence.preferred_matches,
        "description": candidate.artwork.description,
        "visual_category": features["visual_category"],
        "period": features["period"],
        "region": normalize_region(candidate.artwork.region),
    }


def _select_acquired_theme_artworks(
    acquisition: ThemeAcquisitionResult,
    *,
    count: int,
) -> tuple[
    list[Dict[str, Any]],
    CarouselSetOptimizationResult,
    ThemeAcquisitionResult,
]:
    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_score = getattr(config, "MIN_QUALITY_SCORE", 50)
    validated_artworks: list[Dict[str, Any]] = []
    observability = SelectionObservability(
        raw_candidates=acquisition.availability.raw_candidates,
        rights_safe=acquisition.availability.rights_eligible,
        history_new=acquisition.availability.history_eligible,
        quality_pass=acquisition.availability.quality_eligible,
    )
    final_scores: list[float] = []
    final_relevance_failures: Counter = Counter()
    qualified_at_60 = 0
    for themed_candidate in acquisition.candidates[:MAX_FINALIST_VALIDATION_ATTEMPTS]:
        artwork = themed_candidate.artwork
        candidate_path = os.path.join(
            config.DATA_DIR, f"carousel_candidate_{uuid.uuid4().hex}.jpg"
        )
        observability.downloads += 1
        validation = validate_and_download_image_with_metadata(
            artwork.image_url,
            candidate_path,
            purpose=ImageDownloadPurpose.IMAGE_ANALYSIS,
        )
        observability.record_image_validation(validation)
        if not validation.valid:
            observability.reject("image_validation_failed")
            final_relevance_failures["image_validation_failure"] += 1
            logger.debug(
                "carousel_candidate_rejected candidate=%s reason=image_validation_failed validation_reason=%s",
                artwork.canonical_id,
                validation.reason,
            )
            continue
        measured_score = _apply_downloaded_image_measurements(
            artwork,
            validation,
            museum_weights,
            preserve_selection_adjustments=True,
        )
        if measured_score < min_score:
            observability.reject("post_quality_below_threshold")
            final_relevance_failures["post_quality_below_threshold"] += 1
            try:
                os.remove(candidate_path)
            except FileNotFoundError:
                pass
            continue
        try:
            visual_features = extract_visual_features(candidate_path)
        except (OSError, ValueError):
            # Test doubles and rare decoders may provide trustworthy dimensions but
            # no readable pixels. Unknown pixel fields remain honest and deterministic.
            visual_features = features_from_dimensions(
                artwork.image_width, artwork.image_height
            )
        final_candidate = themed_candidate
        if acquisition.theme.evidence_mode is not ThemeEvidenceMode.METADATA:
            observability.visually_scored += 1
            final_evidence = evaluate_theme_relevance(
                artwork,
                acquisition.theme,
                themed_candidate.evidence.matched_queries,
                visual_features,
            )
            final_scores.append(final_evidence.theme_relevance_score)
            if (
                final_evidence.relevance_eligible
                and final_evidence.theme_relevance_score
                >= DEFAULT_MIN_THEME_RELEVANCE
            ):
                qualified_at_60 += 1
            if (
                not final_evidence.relevance_eligible
                or final_evidence.theme_relevance_score < acquisition.policy.minimum_relevance
            ):
                observability.reject("final_theme_relevance_below_threshold")
                if not final_evidence.semantic_grounded:
                    final_relevance_failures["insufficient_semantic_evidence"] += 1
                elif not final_evidence.visual_grounded:
                    final_relevance_failures["insufficient_visual_target_evidence"] += 1
                else:
                    final_relevance_failures["final_score_below_threshold"] += 1
                logger.debug(
                    "theme_relevance id=%s metadata=%.1f query=%.1f visual=%.1f "
                    "semantic_grounded=%s visual_grounded=%s final=%.1f eligible=false",
                    artwork.canonical_id,
                    final_evidence.metadata_score,
                    final_evidence.relevance_breakdown.primary_query
                    + final_evidence.relevance_breakdown.secondary_query
                    + final_evidence.relevance_breakdown.repeated_queries,
                    final_evidence.relevance_breakdown.visual_target
                    + final_evidence.relevance_breakdown.visual_support,
                    final_evidence.semantic_grounded,
                    final_evidence.visual_grounded,
                    final_evidence.theme_relevance_score,
                )
                try:
                    os.remove(candidate_path)
                except FileNotFoundError:
                    pass
                continue
            final_score = CarouselCandidateScoreBreakdown(
                theme_relevance=final_evidence.theme_relevance_score * 0.68,
                technical_quality=artwork.quality_score * 0.30,
                serendipity=themed_candidate.candidate_score.serendipity,
            )
            artwork.selection_score = final_score.total
            final_candidate = replace(
                themed_candidate,
                evidence=final_evidence,
                candidate_score=final_score,
            )
        candidate_dict = _theme_artwork_dict(final_candidate, candidate_path)
        candidate_dict["visual_features"] = visual_features
        validated_artworks.append(candidate_dict)
        if len(validated_artworks) == FINALIST_POOL_SIZE:
            break

    if acquisition.theme.evidence_mode is not ThemeEvidenceMode.METADATA:
        final_count = len(validated_artworks)
        final_sufficient = final_count >= acquisition.availability.absolute_minimum
        updated_availability = replace(
            acquisition.availability,
            relevance_eligible=final_count,
            estimated_safe_pool=final_count,
            sufficient=final_sufficient,
            failure_reason=(
                None if final_sufficient else "insufficient_final_relevance_pool"
            ),
            narrow_pool=(
                acquisition.availability.absolute_minimum
                <= final_count
                < PREFERRED_PREFLIGHT_TARGET
            ),
            pool_status=(
                "unavailable"
                if not final_sufficient
                else "narrow"
                if final_count < PREFERRED_PREFLIGHT_TARGET
                else "preferred"
            ),
            images_inspected=observability.downloads,
            images_attempted=observability.downloads,
            images_validated=observability.images_validated,
            image_validation_failed=observability.rejection_count(
                "image_validation_failed"
            ),
            visually_scored=observability.visually_scored,
            final_relevance_qualified=final_count,
            final_score_min=min(final_scores) if final_scores else None,
            final_score_p25=_percentile(final_scores, 0.25),
            final_score_median=_percentile(final_scores, 0.5),
            final_score_p75=_percentile(final_scores, 0.75),
            final_score_max=max(final_scores) if final_scores else None,
            qualified_at_60=qualified_at_60,
            final_relevance_failures=tuple(sorted(final_relevance_failures.items())),
            aic_fallback_attempted=observability.aic_fallback_attempted,
            aic_fallback_recovered=observability.aic_fallback_recovered,
            aic_fallback_failed=(
                observability.aic_fallback_attempted
                - observability.aic_fallback_recovered
            ),
        )
        acquisition = replace(acquisition, availability=updated_availability)
    logger.info(
        "theme_relevance_pipeline theme=%s mode=%s metadata_prequalified=%s "
        "images_attempted=%s images_validated=%s image_validation_failed=%s "
        "visually_scored=%s final_relevance_qualified=%s absolute_minimum=%s "
        "preferred_target=%s",
        acquisition.theme.id,
        acquisition.theme.evidence_mode.value,
        acquisition.availability.metadata_prequalified,
        observability.downloads,
        observability.images_validated,
        observability.rejection_count("image_validation_failed"),
        observability.visually_scored,
        len(validated_artworks),
        acquisition.availability.absolute_minimum,
        acquisition.availability.target,
    )
    if acquisition.theme.evidence_mode is not ThemeEvidenceMode.METADATA:
        logger.info(
            "theme_relevance_distribution theme=%s validated_images=%s "
            "final_score_min=%s p25=%s median=%s p75=%s max=%s qualified_at_60=%s "
            "failures=%s",
            acquisition.theme.id,
            observability.images_validated,
            acquisition.availability.final_score_min,
            acquisition.availability.final_score_p25,
            acquisition.availability.final_score_median,
            acquisition.availability.final_score_p75,
            acquisition.availability.final_score_max,
            acquisition.availability.qualified_at_60,
            ",".join(
                f"{reason}:{count}"
                for reason, count in acquisition.availability.final_relevance_failures
            )
            or "none",
        )

    required_validated = MIN_TOTAL_SLIDES
    if len(validated_artworks) < required_validated:
        for candidate in validated_artworks:
            try:
                os.remove(str(candidate["local_image_path"]))
            except FileNotFoundError:
                pass
        reason = (
            "insufficient_final_relevance_pool"
            if acquisition.theme.evidence_mode is not ThemeEvidenceMode.METADATA
            else "image_validation_exhausted"
        )
        raise CarouselSelectionError(
            "Unable to build themed carousel: "
            f"theme={acquisition.theme.id} requested={count} "
            f"safe_candidates={len(acquisition.candidates)} validated={len(validated_artworks)} "
            f"required={required_validated} reason={reason}",
            reason=reason,
            availability=acquisition.availability,
        )

    try:
        optimization = optimize_carousel_set(
            validated_artworks,
            theme=acquisition.theme,
            count=count,
            min_quality=min_score,
            min_relevance=acquisition.policy.minimum_relevance,
            cover_candidate_ids=tuple(
                str(artwork["id"]) for artwork in validated_artworks
            ),
        )
    except ValueError as error:
        for candidate in validated_artworks:
            try:
                os.remove(str(candidate["local_image_path"]))
            except FileNotFoundError:
                pass
        raise CarouselSelectionError(
            f"Unable to build themed carousel: theme={acquisition.theme.id} reason={error}",
            reason="diversity_constraints",
            availability=acquisition.availability,
        ) from error

    selected_ids = {str(artwork["id"]) for artwork in optimization.artworks}
    preserve_for_hybrid_cover = (
        acquisition.theme.evidence_mode is not ThemeEvidenceMode.METADATA
    )
    for candidate in validated_artworks:
        if str(candidate["id"]) not in selected_ids and not preserve_for_hybrid_cover:
            try:
                os.remove(str(candidate["local_image_path"]))
            except FileNotFoundError:
                pass

    final_artworks: list[Dict[str, Any]] = []
    for index, artwork in enumerate(optimization.artworks):
        source_path = str(artwork["local_image_path"])
        image_url = str(artwork.get("image_url") or "")
        if is_aic_iiif_url(image_url):
            final_render_path = os.path.join(
                config.DATA_DIR, f"carousel_final_{uuid.uuid4().hex}.jpg"
            )
            final_validation = validate_and_download_image_with_metadata(
                image_url,
                final_render_path,
                purpose=ImageDownloadPurpose.FINAL_RENDER,
            )
            if final_validation.valid:
                try:
                    os.remove(source_path)
                except FileNotFoundError:
                    pass
                source_path = final_render_path
                artwork["image_width"] = final_validation.width
                artwork["image_height"] = final_validation.height
            else:
                try:
                    os.remove(final_render_path)
                except FileNotFoundError:
                    pass
        local_path = os.path.join(config.DATA_DIR, f"output_raw_{index}.jpg")
        os.replace(source_path, local_path)
        finalized = dict(artwork)
        finalized["local_image_path"] = local_path
        final_artworks.append(finalized)
    optimization = replace(optimization, artworks=tuple(final_artworks))
    observability.selected = len(final_artworks)
    acquisition = replace(
        acquisition,
        validated_artworks=tuple(validated_artworks) if preserve_for_hybrid_cover else (),
    )
    return final_artworks, optimization, acquisition


def fetch_themed_artworks(
    posted_ids: set,
    theme: str,
    count: int,
    color_tone: str,
    selection_run_seed: SelectionRunSeed | None = None,
    *,
    theme_definition: CarouselThemeDefinition | None = None,
    return_acquisition: bool = False,
    acquisition_policy: ThemeAcquisitionPolicy | None = None,
    acquisition_run_state: AcquisitionRunState | None = None,
) -> List[Dict[str, Any]] | ThemedArtworkSelection:
    """Select legacy query artwork or a structured, multi-query themed carousel."""
    if theme_definition is None:
        return _fetch_legacy_themed_artworks(
            posted_ids,
            theme,
            count,
            color_tone,
            selection_run_seed=selection_run_seed,
        )
    if not MIN_FEATURED_WORKS <= count <= MAX_FEATURED_WORKS:
        raise ValueError(
            f"Editorial carousel count must be between {MIN_FEATURED_WORKS} and "
            f"{MAX_FEATURED_WORKS}; got {count}."
        )

    run_seed = selection_run_seed or resolve_selection_run_seed()
    acquisition = acquire_theme_candidates(
        theme_definition,
        posted_ids=posted_ids,
        adapters=_museum_adapters(),
        run_seed=run_seed.value,
        museum_weights=getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS),
        min_quality=getattr(config, "MIN_QUALITY_SCORE", 50),
        policy=acquisition_policy,
        run_state=acquisition_run_state,
    )
    if not acquisition.availability.sufficient:
        reason = acquisition.availability.failure_reason or "theme_unavailable"
        raise CarouselSelectionError(
            f"Theme {theme_definition.id} unavailable: reason={reason} "
            f"eligible={acquisition.availability.estimated_safe_pool} "
            f"target={acquisition.availability.target}",
            reason=reason,
            availability=acquisition.availability,
        )
    artworks, optimization, acquisition = _select_acquired_theme_artworks(
        acquisition, count=count
    )
    if return_acquisition:
        return ThemedArtworkSelection(tuple(artworks), acquisition, optimization)
    return artworks
