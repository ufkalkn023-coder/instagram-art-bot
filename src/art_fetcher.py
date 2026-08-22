import logging
import random
import os
import uuid
import hashlib
import secrets
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Dict, Any, List
from src.museums import AICAdapter, ClevelandAdapter, MetAdapter, RijksmuseumAdapter
from src.quality_filter import (
    ImageValidationResult,
    calculate_measurement_coverage,
    calculate_quality_score,
    validate_and_download_image_with_metadata,
)
from src import history_tracker
from src import content_diversity
import config

logger = logging.getLogger(__name__)

# Default weights if not specified in config
DEFAULT_WEIGHTS = {
    "aic": 15,
    "rijksmuseum": 15,
    "met": 15,
    "cleveland": 15
}

MIN_CAROUSEL_ITEMS = 2
MAX_CAROUSEL_ITEMS = 10
SELECTION_SEED_ENV = "ARTFOLIO_SELECTION_SEED"


class CarouselSelectionError(RuntimeError):
    """Raised when a complete, validated carousel cannot be assembled."""


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

    @property
    def selection_score(self) -> float:
        return (
            self.quality_score
            + self.museum_adjustment
            + self.visual_adjustment
            + self.discovery_adjustment
            + self.serendipity_adjustment
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
    rejections: Counter = field(default_factory=Counter)

    def reject(self, reason: str) -> None:
        self.rejections[reason] += 1

    def rejection_count(self, reason: str) -> int:
        return self.rejections[reason]

    def rejection_fields(self) -> str:
        return ",".join(f"{reason}:{count}" for reason, count in sorted(self.rejections.items())) or "none"


def _normalized_diversity_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    if normalized in {"", "unknown", "unknown artist"}:
        return None
    return normalized


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
    if preserve_selection_adjustments and isinstance(breakdown, SelectionScoreBreakdown):
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
    museum_weights: dict,
    min_score: float,
    observability: SelectionObservability,
) -> None:
    """Add validated candidates that fit the current internal diversity caps."""
    artist_counts = {}
    museum_counts = {}
    for candidate in selected:
        artist_key = _normalized_diversity_key(candidate.artist_name)
        museum_key = _normalized_diversity_key(candidate.museum_name)
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if museum_key:
            museum_counts[museum_key] = museum_counts.get(museum_key, 0) + 1

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
        if artist_key and artist_counts.get(artist_key, 0) >= artist_cap:
            observability.reject("internal_artist_limit")
            logger.debug("carousel_candidate_rejected candidate=%s reason=internal_artist_limit", candidate_id)
            continue
        if museum_key and museum_counts.get(museum_key, 0) >= museum_cap:
            observability.reject("internal_museum_limit")
            logger.debug("carousel_candidate_rejected candidate=%s reason=internal_museum_limit", candidate_id)
            continue

        if candidate_id not in validated_paths:
            observability.downloads += 1
            local_path = os.path.join(config.DATA_DIR, f"carousel_candidate_{uuid.uuid4().hex}.jpg")
            validation_result = validate_and_download_image_with_metadata(candidate.image_url, local_path)
            if validation_result.valid:
                measured_score = _apply_downloaded_image_measurements(
                    candidate, validation_result, museum_weights, preserve_selection_adjustments=False
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

    return None

def fetch_random_artwork(posted_ids: set) -> Dict[str, Any]:
    """
    Orchestrates the new Image-First pipeline:
    1. Fetches candidates from all museums.
    2. Normalizes them.
    3. Filters out duplicates.
    4. Scores them based on quality.
    5. Validates the top candidate's image.
    6. Returns the best valid artwork as a dictionary compatible with the rest of the pipeline.
    """
    adapters = [
        AICAdapter(),
        ClevelandAdapter(),
        MetAdapter(),
        RijksmuseumAdapter()
    ]
    
    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_score = getattr(config, "MIN_QUALITY_SCORE", 50)
    selection_run_seed = resolve_selection_run_seed()
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
        candidates = adapter.fetch_candidates(
            limit=15,
            rng=_museum_adapter_rng(selection_run_seed, adapter.source_id, "single_post", None),
        )
        all_candidates.extend(candidates)
        
    logger.info(f"Total raw candidates fetched: {len(all_candidates)}")
    
    # 2. Filter duplicates
    observability.raw_candidates = len(all_candidates)
    new_candidates = []
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
        
    scored_candidates = []
    for c in new_candidates:
        base_score = calculate_quality_score(c, museum_weights)
        c.quality_score = base_score
        c.measurement_coverage = calculate_measurement_coverage(c)
        
        # Calculate diversity penalties/bonuses
        museum_penalty = content_diversity.analyze_museum_diversity(c.museum_name, recent_history)
        features = content_diversity.get_candidate_metadata_features(c)
        visual_bonus = content_diversity.analyze_visual_diversity(features, recent_history)
        discovery_bonus = content_diversity.analyze_discovery_score(features, base_score, recent_history)
        serendipity_bonus = calculate_serendipity_bonus(selection_run_seed.value, c.canonical_id)
        
        breakdown = SelectionScoreBreakdown(
            quality_score=base_score,
            museum_adjustment=museum_penalty,
            visual_adjustment=visual_bonus,
            discovery_adjustment=discovery_bonus,
            serendipity_adjustment=serendipity_bonus,
        )
        c.selection_score = breakdown.selection_score

        # Attach features for later use
        c._diversity_features = features
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
    scored_candidates.sort(key=lambda x: x.selection_score, reverse=True)
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
        
    # 4. Image Validation Loop (Try best scoring artworks first)
    for best_candidate in scored_candidates:
        pre_quality_score = best_candidate.quality_score
        observability.downloads += 1
        logger.debug(
            "single_candidate_download candidate=%s pre_quality=%.2f pre_coverage=%.1f selection=%.2f",
            best_candidate.canonical_id,
            pre_quality_score,
            best_candidate.measurement_coverage,
            best_candidate.selection_score,
        )
        
        validation_result = validate_and_download_image_with_metadata(
            best_candidate.image_url, config.OUTPUT_RAW_IMAGE_PATH
        )
        if validation_result.valid:
            measured_score = _apply_downloaded_image_measurements(
                best_candidate, validation_result, museum_weights, preserve_selection_adjustments=True
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
                os.remove(config.OUTPUT_RAW_IMAGE_PATH)
                continue
            observability.selected += 1
            breakdown = best_candidate._selection_breakdown
            logger.info(
                "selection_selected candidate=%s source=%s pre_quality=%.2f quality=%.2f coverage=%.1f dimensions=%sx%s museum=%+.2f visual=%+.2f discovery=%+.2f serendipity=%+.2f selection=%.2f",
                best_candidate.canonical_id,
                best_candidate.source,
                pre_quality_score,
                best_candidate.quality_score,
                best_candidate.measurement_coverage,
                best_candidate.image_width,
                best_candidate.image_height,
                breakdown.museum_adjustment,
                breakdown.visual_adjustment,
                breakdown.discovery_adjustment,
                breakdown.serendipity_adjustment,
                best_candidate.selection_score,
            )
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
            
            # 5. Return dict format expected by history_tracker and Gemini
            features = getattr(best_candidate, "_diversity_features", {})
            return {
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
                "description": best_candidate.description,
                # Passed down to reserve_artwork
                "visual_category": features.get("visual_category", "other"),
                "period": features.get("period", "unknown"),
            }
        else:
            observability.reject("image_validation_failed")
            logger.info(
                "single_candidate_rejected candidate=%s reason=image_validation_failed validation_reason=%s",
                best_candidate.canonical_id,
                validation_result.reason,
            )
            
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

def fetch_themed_artworks(posted_ids: set, theme: str, count: int, color_tone: str) -> List[Dict[str, Any]]:
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
    selection_run_seed = resolve_selection_run_seed()
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
        adapters = [AICAdapter(), ClevelandAdapter(), MetAdapter(), RijksmuseumAdapter()]
        for adapter in adapters:
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

    add_candidates(f"{color_tone} {theme}", 25, "tone_and_theme")
    _select_carousel_candidates(
        ranked_candidates(), count, selected, selected_ids, validated_paths, 1, strict_museum_cap,
        museum_weights, min_score, observability,
    )

    if len(selected) < count:
        add_candidates(theme, 25, "theme_only")
        _select_carousel_candidates(
            ranked_candidates(), count, selected, selected_ids, validated_paths, 1, strict_museum_cap,
            museum_weights, min_score, observability,
        )

    if len(selected) < count:
        logger.info("Carousel selection relaxing internal caps: artist<=2, museum<=%s", relaxed_museum_cap)
        _select_carousel_candidates(
            ranked_candidates(), count, selected, selected_ids, validated_paths, 2, relaxed_museum_cap,
            museum_weights, min_score, observability,
        )

    if len(selected) < count:
        add_candidates(theme, 50, "expanded_theme")
        _select_carousel_candidates(
            ranked_candidates(), count, selected, selected_ids, validated_paths, 1, strict_museum_cap,
            museum_weights, min_score, observability,
        )
        if len(selected) < count:
            _select_carousel_candidates(
                ranked_candidates(), count, selected, selected_ids, validated_paths, 2, relaxed_museum_cap,
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
        })

    source_distribution = Counter(candidate.source for candidate in selected)
    logger.info(
        "carousel_selection_summary requested=%s selected=%s safe_candidates=%s validated_images=%s stages=%s downloads=%s artists_unique=%s source_distribution=%s rejections=%s result=selected",
        count,
        len(selected),
        len(candidates_by_id),
        sum(path is not None for path in validated_paths.values()),
        ",".join(fallback_stages),
        observability.downloads,
        len({_normalized_diversity_key(candidate.artist_name) for candidate in selected if _normalized_diversity_key(candidate.artist_name)}),
        ",".join(f"{source}:{source_distribution[source]}" for source in sorted(source_distribution)) or "none",
        observability.rejection_fields(),
    )
    return final_artworks
