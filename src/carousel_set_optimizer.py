"""Deterministic, format-aware set curation for featured carousel artworks."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    DominantColorFamily,
    LuminanceBucket,
    features_from_dimensions,
)
from src.artwork_metadata import (
    ArtworkDateInfo,
    normalize_artist_identity,
    normalize_medium_family,
    normalize_museum_identity,
    parse_artwork_date,
    period_bucket,
)
from src.carousel_themes import (
    FORMAT_POLICIES,
    CarouselFormat,
    CarouselFormatPolicy,
    CarouselThemeDefinition,
    ComparisonDimension,
)
from src.format_contracts import qualify_artwork_mapping
from src.region import normalize_region
from src.theme_acquisition import DEFAULT_MIN_THEME_RELEVANCE, normalize_theme_text
from src.carousel_policy import (
    ADAPTIVE_CAROUSEL_SIZE_POLICY,
    MAX_FEATURED_WORKS,
    MIN_FEATURED_WORKS,
    CarouselSizePolicy,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.engagement_learning import EngagementModel

FINALIST_POOL_SIZE = 24
SET_BEAM_WIDTH = 64
MAX_SWAP_ITERATIONS = 32


@dataclass(frozen=True)
class ArtworkSelectionFeatures:
    artwork: Mapping[str, object]
    canonical_id: str
    theme_relevance: float
    quality: float
    candidate_score: float
    artist_key: str | None
    museum_key: str | None
    region: str | None
    period_bucket: str | None
    creation_year: int | None
    date_info: ArtworkDateInfo | None
    medium_family: str | None
    semantic_tokens: frozenset[str]
    visual: ArtworkVisualFeatures

    @property
    def individual_strength(self) -> float:
        if "learned_score" in self.artwork:
            return round(max(0.0, min(100.0, self.candidate_score)), 4)
        return round(
            0.70 * self.theme_relevance
            + 0.25 * self.quality
            + 0.05 * min(100.0, self.candidate_score),
            4,
        )


@dataclass(frozen=True)
class PairwiseRedundancyBreakdown:
    same_artist: float = 0.0
    same_museum: float = 0.0
    same_region: float = 0.0
    same_period: float = 0.0
    same_medium: float = 0.0
    same_orientation: float = 0.0
    similar_luminance: float = 0.0
    same_color: float = 0.0
    semantic_overlap: float = 0.0

    @property
    def visual_total(self) -> float:
        return round(self.same_orientation + self.similar_luminance + self.same_color, 4)

    @property
    def semantic_total(self) -> float:
        return round(
            self.same_artist
            + self.same_museum
            + self.same_region
            + self.same_period
            + self.same_medium
            + self.semantic_overlap,
            4,
        )

    @property
    def total(self) -> float:
        return round(self.visual_total + self.semantic_total, 4)


@dataclass(frozen=True)
class CarouselSetScoreBreakdown:
    individual_strength: float
    artist_diversity: float
    museum_diversity: float
    region_diversity: float
    period_diversity: float
    medium_diversity: float
    orientation_balance: float
    luminance_balance: float
    visual_redundancy_penalty: float
    semantic_redundancy_penalty: float
    format_adjustments: float
    engagement_prediction_adjustment: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.individual_strength
            + self.artist_diversity
            + self.museum_diversity
            + self.region_diversity
            + self.period_diversity
            + self.medium_diversity
            + self.orientation_balance
            + self.luminance_balance
            + self.format_adjustments
            + self.engagement_prediction_adjustment
            - self.visual_redundancy_penalty
            - self.semantic_redundancy_penalty,
            4,
        )


@dataclass(frozen=True)
class CarouselSetOptimizationResult:
    artworks: tuple[Mapping[str, object], ...]
    set_score: float
    breakdown: CarouselSetScoreBreakdown
    finalist_count: int
    hard_constraint_profile: str
    beam_width: int
    swap_iterations: int
    optimizer_size_decision: str
    marginal_diagnostics: tuple["CarouselSizeDiagnostic", ...]


@dataclass(frozen=True)
class CarouselSizeDiagnostic:
    """One deterministic adaptive-cardinality decision point."""

    featured_count: int
    normalized_editorial_utility: float
    marginal_inclusion_utility: float | None
    marginal_artwork_id: str | None
    hard_constraint_profile: str
    accepted: bool
    reason: str


@dataclass(frozen=True)
class _HardCaps:
    name: str
    artist: int | None
    museum: int | None
    region: int | None


def normalize_known_identity(value: object) -> str | None:
    return normalize_artist_identity(value)


def parse_creation_year(value: object) -> int | None:
    info = parse_artwork_date(value)
    return info.representative_year if info else None


def medium_family(medium: object, classification: object = None) -> str | None:
    return normalize_medium_family(medium, classification)


def _theme_signal_tokens(theme: CarouselThemeDefinition) -> frozenset[str]:
    signals = (
        *theme.required_terms,
        *(signal for group in theme.required_term_groups for signal in group),
        *theme.preferred_terms,
        *theme.aliases,
        *theme.primary_queries,
        *theme.secondary_queries,
    )
    return frozenset(token for signal in signals for token in normalize_theme_text(signal))


def _semantic_tokens(artwork: Mapping[str, object], excluded: frozenset[str]) -> frozenset[str]:
    tokens = set()
    for field in ("title", "classification", "description"):
        tokens.update(token for token in normalize_theme_text(artwork.get(field)) if len(token) >= 4)
    return frozenset(tokens - excluded)


def _visual_features(artwork: Mapping[str, object]) -> ArtworkVisualFeatures:
    features = artwork.get("visual_features")
    if isinstance(features, ArtworkVisualFeatures):
        return features
    return features_from_dimensions(
        _optional_int(artwork.get("image_width")),
        _optional_int(artwork.get("image_height")),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def build_selection_features(
    artwork: Mapping[str, object], theme: CarouselThemeDefinition
) -> ArtworkSelectionFeatures:
    quality = float(artwork.get("quality_score") or 0.0)
    candidate_score = float(artwork.get("selection_score") or quality)
    relevance = float(artwork.get("theme_relevance_score") or candidate_score or quality)
    region = normalize_region(artwork.get("region"))
    date_info = parse_artwork_date(artwork.get("date"))
    return ArtworkSelectionFeatures(
        artwork=artwork,
        canonical_id=str(artwork["id"]),
        theme_relevance=relevance,
        quality=quality,
        candidate_score=candidate_score,
        artist_key=normalize_artist_identity(artwork.get("artist")),
        museum_key=normalize_museum_identity(artwork.get("museum")),
        region=region if region != "unknown" else None,
        period_bucket=period_bucket(artwork.get("date")),
        creation_year=date_info.representative_year if date_info else None,
        date_info=date_info,
        medium_family=medium_family(artwork.get("medium"), artwork.get("classification")),
        semantic_tokens=_semantic_tokens(artwork, _theme_signal_tokens(theme)),
        visual=_visual_features(artwork),
    )


def pairwise_redundancy(
    first: ArtworkSelectionFeatures,
    second: ArtworkSelectionFeatures,
    policy: CarouselFormatPolicy,
) -> PairwiseRedundancyBreakdown:
    """Return bounded, explainable similarity penalties for one pair."""
    semantic_overlap = 0.0
    if policy.penalize_semantic_similarity and first.semantic_tokens and second.semantic_tokens:
        union = first.semantic_tokens | second.semantic_tokens
        similarity = len(first.semantic_tokens & second.semantic_tokens) / len(union)
        semantic_overlap = round(max(0.0, similarity - 0.25) / 0.75 * 1.5, 4)

    return PairwiseRedundancyBreakdown(
        same_artist=12.0
        if policy.penalize_artist_similarity
        and first.artist_key
        and first.artist_key == second.artist_key
        else 0.0,
        same_museum=2.5
        if policy.penalize_museum_similarity
        and first.museum_key
        and first.museum_key == second.museum_key
        else 0.0,
        same_region=2.0
        if policy.penalize_region_similarity and first.region and first.region == second.region
        else 0.0,
        same_period=1.5
        if policy.penalize_period_similarity
        and first.period_bucket
        and first.period_bucket == second.period_bucket
        else 0.0,
        same_medium=1.0
        if policy.penalize_medium_similarity
        and first.medium_family
        and first.medium_family == second.medium_family
        else 0.0,
        same_orientation=0.5
        if policy.penalize_orientation_similarity
        and first.visual.orientation is not ArtworkOrientation.UNKNOWN
        and first.visual.orientation is second.visual.orientation
        else 0.0,
        similar_luminance=0.6
        if policy.penalize_luminance_similarity
        and first.visual.luminance_bucket is not LuminanceBucket.UNKNOWN
        and first.visual.luminance_bucket is second.visual.luminance_bucket
        else 0.0,
        same_color=0.6
        if policy.penalize_color_similarity
        and first.visual.dominant_color_family is not DominantColorFamily.UNKNOWN
        and first.visual.dominant_color_family is second.visual.dominant_color_family
        else 0.0,
        semantic_overlap=semantic_overlap,
    )


def _diversity_score(values: Sequence[str | None], weight: float) -> float:
    known = [value for value in values if value is not None]
    if not known or weight == 0:
        return 0.0
    return round(weight * len(set(known)) / len(known), 4)


def _chronological_adjustment(features: Sequence[ArtworkSelectionFeatures]) -> float:
    known = sorted(feature.creation_year for feature in features if feature.creation_year is not None)
    if not known:
        return 0.0
    known_coverage = 2.0 * len(known) / len(features)
    if len(known) < 2:
        return known_coverage
    span_score = min(6.0, (known[-1] - known[0]) / 75.0)
    distinct_bucket_score = 2.0 * len(
        {feature.period_bucket for feature in features if feature.period_bucket}
    ) / len(features)
    return round(known_coverage + span_score + distinct_bucket_score, 4)


def _comparison_dimension_score(
    features: Sequence[ArtworkSelectionFeatures], dimension: ComparisonDimension
) -> float:
    values: list[str | None]
    if dimension is ComparisonDimension.ARTIST:
        values = [feature.artist_key for feature in features]
    elif dimension is ComparisonDimension.MUSEUM:
        values = [feature.museum_key for feature in features]
    elif dimension is ComparisonDimension.REGION:
        values = [feature.region for feature in features]
    elif dimension is ComparisonDimension.PERIOD:
        values = [feature.period_bucket for feature in features]
    elif dimension is ComparisonDimension.MEDIUM:
        values = [feature.medium_family for feature in features]
    elif dimension is ComparisonDimension.LUMINANCE:
        values = [
            None
            if feature.visual.luminance_bucket is LuminanceBucket.UNKNOWN
            else feature.visual.luminance_bucket.value
            for feature in features
        ]
    elif dimension is ComparisonDimension.COLOR:
        values = [
            None
            if feature.visual.dominant_color_family is DominantColorFamily.UNKNOWN
            else feature.visual.dominant_color_family.value
            for feature in features
        ]
    else:
        values = [
            None
            if feature.visual.orientation is ArtworkOrientation.UNKNOWN
            else feature.visual.orientation.value
            for feature in features
        ]
    return _diversity_score(values, 5.0)


def score_carousel_set(
    features: Sequence[ArtworkSelectionFeatures],
    theme: CarouselThemeDefinition,
    *,
    engagement_model: "EngagementModel | None" = None,
    engagement_context: Mapping[str, object] | None = None,
) -> CarouselSetScoreBreakdown:
    if not features:
        return CarouselSetScoreBreakdown(*(0.0 for _ in range(11)))
    policy = FORMAT_POLICIES[theme.format]
    pair_breakdowns = [
        pairwise_redundancy(features[index], features[other], policy)
        for index in range(len(features))
        for other in range(index + 1, len(features))
    ]
    penalty_divisor = max(1.0, len(features))
    visual_penalty = sum(pair.visual_total for pair in pair_breakdowns) / penalty_divisor
    semantic_penalty = sum(pair.semantic_total for pair in pair_breakdowns) / penalty_divisor

    format_adjustment = 0.0
    if theme.format is CarouselFormat.CHRONOLOGICAL:
        format_adjustment = _chronological_adjustment(features)
    elif (
        theme.format is CarouselFormat.COMPARATIVE
        and theme.format_target
        and theme.format_target.comparison_dimension
    ):
        format_adjustment = _comparison_dimension_score(
            features, theme.format_target.comparison_dimension
        )

    engagement_adjustment = 0.0
    if engagement_model is not None:
        prediction = engagement_model.score_set(
            [feature.artwork for feature in features],
            engagement_context or {},
        )
        engagement_adjustment = max(
            -5.0,
            min(
                5.0,
                (prediction.score - 50.0) / 10.0 * engagement_model.confidence,
            ),
        )

    return CarouselSetScoreBreakdown(
        individual_strength=round(
            sum(feature.individual_strength for feature in features) / len(features), 4
        ),
        artist_diversity=_diversity_score(
            [feature.artist_key for feature in features], policy.artist_diversity_weight
        ),
        museum_diversity=_diversity_score(
            [feature.museum_key for feature in features], policy.museum_diversity_weight
        ),
        region_diversity=_diversity_score(
            [feature.region for feature in features], policy.region_diversity_weight
        ),
        period_diversity=_diversity_score(
            [feature.period_bucket for feature in features], policy.period_diversity_weight
        ),
        medium_diversity=_diversity_score(
            [feature.medium_family for feature in features], policy.medium_diversity_weight
        ),
        orientation_balance=_diversity_score(
            [
                None if feature.visual.orientation is ArtworkOrientation.UNKNOWN else feature.visual.orientation.value
                for feature in features
            ],
            1.5,
        ),
        luminance_balance=_diversity_score(
            [
                None
                if feature.visual.luminance_bucket is LuminanceBucket.UNKNOWN
                else feature.visual.luminance_bucket.value
                for feature in features
            ],
            1.5,
        ),
        visual_redundancy_penalty=round(visual_penalty, 4),
        semantic_redundancy_penalty=round(semantic_penalty, 4),
        format_adjustments=round(format_adjustment, 4),
        engagement_prediction_adjustment=round(engagement_adjustment, 4),
    )


def _within_caps(features: Sequence[ArtworkSelectionFeatures], caps: _HardCaps) -> bool:
    artist_counts = Counter(feature.artist_key for feature in features if feature.artist_key)
    museum_counts = Counter(feature.museum_key for feature in features if feature.museum_key)
    region_counts = Counter(feature.region for feature in features if feature.region)
    return (
        (caps.artist is None or max(artist_counts.values(), default=0) <= caps.artist)
        and (caps.museum is None or max(museum_counts.values(), default=0) <= caps.museum)
        and (caps.region is None or max(region_counts.values(), default=0) <= caps.region)
    )


def _beam_construct(
    finalists: Sequence[ArtworkSelectionFeatures],
    *,
    count: int,
    theme: CarouselThemeDefinition,
    caps: _HardCaps,
    cover_candidate_ids: frozenset[str],
    engagement_model: "EngagementModel | None" = None,
    engagement_context: Mapping[str, object] | None = None,
) -> tuple[ArtworkSelectionFeatures, ...] | None:
    beam: list[tuple[ArtworkSelectionFeatures, ...]] = [()]
    for _depth in range(count):
        expanded: dict[tuple[str, ...], tuple[ArtworkSelectionFeatures, ...]] = {}
        for current in beam:
            current_ids = {feature.canonical_id for feature in current}
            for candidate in finalists:
                if candidate.canonical_id in current_ids:
                    continue
                proposed = (*current, candidate)
                if not _within_caps(proposed, caps):
                    continue
                if cover_candidate_ids and not cover_candidate_ids.difference(
                    feature.canonical_id for feature in proposed
                ):
                    continue
                key = tuple(sorted(feature.canonical_id for feature in proposed))
                expanded.setdefault(key, proposed)
        if not expanded:
            return None
        beam = sorted(
            expanded.values(),
            key=lambda item: (
                -score_carousel_set(
                    item,
                    theme,
                    engagement_model=engagement_model,
                    engagement_context=engagement_context,
                ).total,
                tuple(sorted(feature.canonical_id for feature in item)),
            ),
        )[:SET_BEAM_WIDTH]
    return beam[0] if beam else None


def _profiles_for_size(
    theme: CarouselThemeDefinition, count: int
) -> tuple[_HardCaps, _HardCaps]:
    policy = FORMAT_POLICIES[theme.format]
    return (
        _HardCaps(
            "strict",
            artist=policy.strict_artist_cap,
            museum=(
                min(policy.strict_museum_cap, count)
                if policy.strict_museum_cap is not None
                else None
            ),
            region=policy.strict_region_cap,
        ),
        _HardCaps(
            "relaxed",
            artist=policy.relaxed_artist_cap,
            museum=(
                min(policy.relaxed_museum_cap, count)
                if policy.relaxed_museum_cap is not None
                else None
            ),
            region=policy.relaxed_region_cap,
        ),
    )


def _optimize_for_size(
    finalists: Sequence[ArtworkSelectionFeatures],
    *,
    count: int,
    theme: CarouselThemeDefinition,
    cover_candidate_ids: frozenset[str],
    engagement_model: "EngagementModel | None" = None,
    engagement_context: Mapping[str, object] | None = None,
) -> tuple[
    tuple[ArtworkSelectionFeatures, ...],
    CarouselSetScoreBreakdown,
    _HardCaps,
    int,
] | None:
    selected = None
    active_caps = None
    for caps in _profiles_for_size(theme, count):
        selected = _beam_construct(
            finalists,
            count=count,
            theme=theme,
            caps=caps,
            cover_candidate_ids=cover_candidate_ids,
            engagement_model=engagement_model,
            engagement_context=engagement_context,
        )
        if selected is not None:
            active_caps = caps
            break
    if selected is None or active_caps is None:
        return None

    swap_iterations = 0
    while swap_iterations < MAX_SWAP_ITERATIONS:
        current_breakdown = score_carousel_set(
            selected,
            theme,
            engagement_model=engagement_model,
            engagement_context=engagement_context,
        )
        selected_ids = {feature.canonical_id for feature in selected}
        best = None
        for index in range(len(selected)):
            for candidate in finalists:
                if candidate.canonical_id in selected_ids:
                    continue
                proposed = (*selected[:index], candidate, *selected[index + 1 :])
                if not _within_caps(proposed, active_caps):
                    continue
                if cover_candidate_ids and not cover_candidate_ids.difference(
                    feature.canonical_id for feature in proposed
                ):
                    continue
                breakdown = score_carousel_set(
                    proposed,
                    theme,
                    engagement_model=engagement_model,
                    engagement_context=engagement_context,
                )
                key = (
                    -breakdown.total,
                    tuple(sorted(feature.canonical_id for feature in proposed)),
                )
                if breakdown.total > current_breakdown.total + 1e-6 and (
                    best is None or key < best[0]
                ):
                    best = (key, proposed, breakdown)
        if best is None:
            break
        _, selected, accepted_breakdown = best
        swap_iterations += 1
        logger.debug(
            "carousel_set_swap size=%s iteration=%s set_score=%.4f ids=%s",
            count,
            swap_iterations,
            accepted_breakdown.total,
            ",".join(feature.canonical_id for feature in selected),
        )

    selected = tuple(
        sorted(selected, key=lambda feature: (-feature.individual_strength, feature.canonical_id))
    )
    return (
        selected,
        score_carousel_set(
            selected,
            theme,
            engagement_model=engagement_model,
            engagement_context=engagement_context,
        ),
        active_caps,
        swap_iterations,
    )


def _marginal_inclusion_utility(
    selected: Sequence[ArtworkSelectionFeatures],
    *,
    theme: CarouselThemeDefinition,
    profile_name: str,
    size_policy: CarouselSizePolicy,
    engagement_model: "EngagementModel | None" = None,
    engagement_context: Mapping[str, object] | None = None,
) -> tuple[float, str]:
    """Score the least worthwhile member using existing set and item utilities."""
    full_utility = score_carousel_set(
        selected,
        theme,
        engagement_model=engagement_model,
        engagement_context=engagement_context,
    ).total
    weakest: tuple[float, str] | None = None
    for index, feature in enumerate(selected):
        reduced = (*selected[:index], *selected[index + 1 :])
        set_effect = full_utility - score_carousel_set(
            reduced,
            theme,
            engagement_model=engagement_model,
            engagement_context=engagement_context,
        ).total
        bounded_effect = max(
            -size_policy.max_set_effect,
            min(size_policy.max_set_effect, set_effect),
        )
        utility = feature.individual_strength + size_policy.set_effect_weight * bounded_effect
        if profile_name == "relaxed":
            utility -= size_policy.relaxed_constraint_penalty
        candidate = (round(utility, 4), feature.canonical_id)
        if weakest is None or candidate < weakest:
            weakest = candidate
    assert weakest is not None
    return weakest


def optimize_carousel_set(
    artworks: Sequence[Mapping[str, object]],
    *,
    theme: CarouselThemeDefinition,
    count: int = MAX_FEATURED_WORKS,
    min_quality: float = 50.0,
    min_relevance: float = DEFAULT_MIN_THEME_RELEVANCE,
    cover_candidate_ids: Sequence[str] = (),
    size_policy: CarouselSizePolicy = ADAPTIVE_CAROUSEL_SIZE_POLICY,
    engagement_model: "EngagementModel | None" = None,
    engagement_context: Mapping[str, object] | None = None,
) -> CarouselSetOptimizationResult:
    """Choose the strongest feasible 5–8-work set using bounded marginal utility."""
    if not MIN_FEATURED_WORKS <= count <= MAX_FEATURED_WORKS:
        raise ValueError(
            f"Carousel maximum must be between {MIN_FEATURED_WORKS} and "
            f"{MAX_FEATURED_WORKS}; got {count}"
        )
    by_id: dict[str, ArtworkSelectionFeatures] = {}
    for artwork in artworks:
        target_match, _ = qualify_artwork_mapping(artwork, theme)
        if not target_match:
            continue
        feature = build_selection_features(artwork, theme)
        if feature.quality < min_quality or feature.theme_relevance < min_relevance:
            continue
        previous = by_id.get(feature.canonical_id)
        if previous is None or feature.individual_strength > previous.individual_strength:
            by_id[feature.canonical_id] = feature
    finalists = sorted(
        by_id.values(), key=lambda feature: (-feature.individual_strength, feature.canonical_id)
    )[:FINALIST_POOL_SIZE]
    if len(finalists) < MIN_FEATURED_WORKS:
        raise ValueError(
            f"Set optimizer requires {MIN_FEATURED_WORKS} eligible unique finalists; "
            f"got {len(finalists)}"
        )
    cover_ids = frozenset(str(value) for value in cover_candidate_ids)
    viable_sizes: list[int] = []
    diagnostics: list[CarouselSizeDiagnostic] = []
    optimized_by_size: dict[
        int,
        tuple[
            tuple[ArtworkSelectionFeatures, ...],
            CarouselSetScoreBreakdown,
            _HardCaps,
            int,
        ],
    ] = {}
    maximum = min(count, len(finalists))
    for featured_count in range(MIN_FEATURED_WORKS, maximum + 1):
        optimized = _optimize_for_size(
            finalists,
            count=featured_count,
            theme=theme,
            cover_candidate_ids=cover_ids,
            engagement_model=engagement_model,
            engagement_context=engagement_context,
        )
        if optimized is None:
            logger.debug(
                "carousel_size_candidate theme=%s size=%s viable=false",
                theme.id,
                featured_count,
            )
            continue
        selected_for_size, breakdown_for_size, caps_for_size, iterations_for_size = optimized
        optimized_by_size[featured_count] = optimized
        viable_sizes.append(featured_count)
        marginal = marginal_id = None
        accepted = featured_count == MIN_FEATURED_WORKS
        decision_reason = "minimum_valid_product" if accepted else "marginal_utility_threshold"
        if featured_count > MIN_FEATURED_WORKS:
            marginal, marginal_id = _marginal_inclusion_utility(
                selected_for_size,
                theme=theme,
                profile_name=caps_for_size.name,
                size_policy=size_policy,
                engagement_model=engagement_model,
                engagement_context=engagement_context,
            )
            accepted = marginal >= size_policy.marginal_inclusion_threshold
        diagnostics.append(
            CarouselSizeDiagnostic(
                featured_count=featured_count,
                normalized_editorial_utility=breakdown_for_size.total,
                marginal_inclusion_utility=marginal,
                marginal_artwork_id=marginal_id,
                hard_constraint_profile=caps_for_size.name,
                accepted=accepted,
                reason=decision_reason,
            )
        )
        logger.debug(
            "carousel_size_candidate theme=%s size=%s utility=%.4f marginal=%s "
            "threshold=%.2f profile=%s accepted=%s",
            theme.id,
            featured_count,
            breakdown_for_size.total,
            marginal,
            size_policy.marginal_inclusion_threshold,
            caps_for_size.name,
            accepted,
        )
    chosen = optimized_by_size.get(MIN_FEATURED_WORKS)
    if chosen is None:
        raise ValueError("Hard artist/museum/region/cover constraints prevent a minimum carousel set")
    reason = "no_feasible_larger_set"
    diagnostics_by_size = {item.featured_count: item for item in diagnostics}
    for featured_count in range(MIN_FEATURED_WORKS + 1, maximum + 1):
        candidate = optimized_by_size.get(featured_count)
        diagnostic = diagnostics_by_size.get(featured_count)
        if candidate is None or diagnostic is None:
            reason = "no_feasible_larger_set"
            break
        if not diagnostic.accepted:
            reason = "marginal_utility_threshold"
            break
        chosen = candidate
        reason = (
            "maximum_featured_reached"
            if featured_count == maximum
            else "marginal_utility_threshold"
        )

    selected, breakdown, active_caps, swap_iterations = chosen
    artists = len({feature.artist_key for feature in selected if feature.artist_key})
    museums = len({feature.museum_key for feature in selected if feature.museum_key})
    regions = len({feature.region for feature in selected if feature.region})
    periods = len({feature.period_bucket for feature in selected if feature.period_bucket})
    orientations = len(
        {
            feature.visual.orientation
            for feature in selected
            if feature.visual.orientation is not ArtworkOrientation.UNKNOWN
        }
    )
    logger.info(
        "carousel_size_decision theme=%s viable_sizes=%s selected_featured_count=%s "
        "total_slides=%s reason=%s",
        theme.id,
        ",".join(str(value) for value in viable_sizes),
        len(selected),
        len(selected) + 1,
        reason,
    )
    logger.info(
        "carousel_set_selected theme=%s format=%s candidates=%s set_score=%.2f "
        "avg_relevance=%.2f avg_quality=%.2f artists=%s museums=%s regions=%s "
        "periods=%s orientations=%s redundancy_penalty=%.2f engagement_adjustment=%+.2f",
        theme.id,
        theme.format.value,
        len(finalists),
        breakdown.total,
        sum(feature.theme_relevance for feature in selected) / len(selected),
        sum(feature.quality for feature in selected) / len(selected),
        artists,
        museums,
        regions,
        periods,
        orientations,
        breakdown.visual_redundancy_penalty + breakdown.semantic_redundancy_penalty,
        breakdown.engagement_prediction_adjustment,
    )
    logger.debug(
        "carousel_set_breakdown theme=%s individual=%.4f artist=%.4f museum=%.4f "
        "region=%.4f period=%.4f medium=%.4f orientation=%.4f luminance=%.4f "
        "visual_penalty=%.4f semantic_penalty=%.4f format=%.4f engagement=%.4f total=%.4f",
        theme.id,
        breakdown.individual_strength,
        breakdown.artist_diversity,
        breakdown.museum_diversity,
        breakdown.region_diversity,
        breakdown.period_diversity,
        breakdown.medium_diversity,
        breakdown.orientation_balance,
        breakdown.luminance_balance,
        breakdown.visual_redundancy_penalty,
        breakdown.semantic_redundancy_penalty,
        breakdown.format_adjustments,
        breakdown.engagement_prediction_adjustment,
        breakdown.total,
    )
    return CarouselSetOptimizationResult(
        artworks=tuple(feature.artwork for feature in selected),
        set_score=breakdown.total,
        breakdown=breakdown,
        finalist_count=len(finalists),
        hard_constraint_profile=active_caps.name,
        beam_width=SET_BEAM_WIDTH,
        swap_iterations=swap_iterations,
        optimizer_size_decision=reason,
        marginal_diagnostics=tuple(diagnostics),
    )
