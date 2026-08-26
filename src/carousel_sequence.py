"""Deterministic narrative ordering for an already-selected featured artwork set."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    DominantColorFamily,
    LuminanceBucket,
)
from src.carousel_set_optimizer import (
    ArtworkSelectionFeatures,
    build_selection_features,
    pairwise_redundancy,
)
from src.carousel_themes import FORMAT_POLICIES, CarouselFormat, CarouselThemeDefinition
from src.carousel_policy import MAX_FEATURED_WORKS, MIN_FEATURED_WORKS

logger = logging.getLogger(__name__)

SEQUENCE_BEAM_WIDTH = 128


@dataclass(frozen=True)
class CarouselSequenceBreakdown:
    position_strength: float
    transition_quality: float
    cover_transition: float
    comparative_contrast: float
    chronology: float

    @property
    def total(self) -> float:
        return round(
            self.position_strength
            + self.transition_quality
            + self.cover_transition
            + self.comparative_contrast
            + self.chronology,
            4,
        )


@dataclass(frozen=True)
class CarouselSequenceResult:
    ordered_artworks: tuple[Mapping[str, object], ...]
    transition_score: float
    sequence_score: float
    breakdown: CarouselSequenceBreakdown
    beam_width: int


# Highest-strength works are deliberately distributed across opener, middle,
# penultimate, and closure rather than consumed in descending-score order.
POSITION_STRENGTH_WEIGHTS = (0.105, 0.025, 0.02, 0.06, 0.025, 0.02, 0.075, 0.09)


def _cover_transition_score(
    candidate: ArtworkSelectionFeatures,
    cover: ArtworkVisualFeatures | None,
) -> float:
    if cover is None:
        return 0.0
    penalty = 0.0
    visual = candidate.visual
    if (
        cover.orientation is not ArtworkOrientation.UNKNOWN
        and visual.orientation is cover.orientation
    ):
        penalty += 0.25
    if (
        cover.luminance_bucket is not LuminanceBucket.UNKNOWN
        and visual.luminance_bucket is cover.luminance_bucket
    ):
        penalty += 0.4
    if (
        cover.dominant_color_family is not DominantColorFamily.UNKNOWN
        and visual.dominant_color_family is cover.dominant_color_family
    ):
        penalty += 0.35
    return -penalty


def _comparative_contrast(
    previous: ArtworkSelectionFeatures,
    candidate: ArtworkSelectionFeatures,
    theme: CarouselThemeDefinition,
) -> float:
    if theme.format is not CarouselFormat.COMPARATIVE:
        return 0.0
    differences = {
        "artist": bool(previous.artist_key and candidate.artist_key and previous.artist_key != candidate.artist_key),
        "museum": bool(previous.museum_key and candidate.museum_key and previous.museum_key != candidate.museum_key),
        "period": bool(
            previous.period_bucket
            and candidate.period_bucket
            and previous.period_bucket != candidate.period_bucket
        ),
        "region": bool(previous.region and candidate.region and previous.region != candidate.region),
        "medium": bool(
            previous.medium_family
            and candidate.medium_family
            and previous.medium_family != candidate.medium_family
        ),
        "orientation": bool(
            previous.visual.orientation is not ArtworkOrientation.UNKNOWN
            and candidate.visual.orientation is not ArtworkOrientation.UNKNOWN
            and previous.visual.orientation is not candidate.visual.orientation
        ),
        "luminance": bool(
            previous.visual.luminance_bucket is not LuminanceBucket.UNKNOWN
            and candidate.visual.luminance_bucket is not LuminanceBucket.UNKNOWN
            and previous.visual.luminance_bucket is not candidate.visual.luminance_bucket
        ),
        "color": bool(
            previous.visual.dominant_color_family is not DominantColorFamily.UNKNOWN
            and candidate.visual.dominant_color_family is not DominantColorFamily.UNKNOWN
            and previous.visual.dominant_color_family is not candidate.visual.dominant_color_family
        ),
    }
    score = 0.45 * sum(differences.values())
    target = theme.format_target
    if target and target.comparison_dimension and differences[target.comparison_dimension.value]:
        score += 1.25
    return score


def _transition_quality(
    previous: ArtworkSelectionFeatures,
    candidate: ArtworkSelectionFeatures,
    theme: CarouselThemeDefinition,
) -> float:
    redundancy = pairwise_redundancy(previous, candidate, FORMAT_POLICIES[theme.format])
    score = -0.65 * redundancy.total
    if (
        previous.visual.luminance_bucket is not LuminanceBucket.UNKNOWN
        and candidate.visual.luminance_bucket is not LuminanceBucket.UNKNOWN
        and previous.visual.luminance_bucket is not candidate.visual.luminance_bucket
    ):
        score += 0.5
    if (
        previous.visual.orientation is not ArtworkOrientation.UNKNOWN
        and candidate.visual.orientation is not ArtworkOrientation.UNKNOWN
        and previous.visual.orientation is not candidate.visual.orientation
    ):
        score += 0.25
    if (
        previous.visual.mean_luminance is not None
        and candidate.visual.mean_luminance is not None
        and abs(previous.visual.mean_luminance - candidate.visual.mean_luminance) > 140
    ):
        score -= 0.2
    return score


def _position_score(
    feature: ArtworkSelectionFeatures, position: int, total_positions: int
) -> float:
    if position == 0:
        # Slide 2 establishes the theme; relevance remains the leading signal.
        return 0.35 * feature.theme_relevance + 0.04 * feature.quality + 0.01 * feature.individual_strength
    # Preserve the existing eight-work narrative curve while mapping shorter
    # sequences across its full opener-to-closure span.
    last_position = max(1, len(POSITION_STRENGTH_WEIGHTS) - 1)
    scaled_position = round(position * last_position / max(1, total_positions - 1))
    weight = POSITION_STRENGTH_WEIGHTS[scaled_position]
    return weight * feature.individual_strength


def _breakdown(
    ordered: Sequence[ArtworkSelectionFeatures],
    theme: CarouselThemeDefinition,
    cover: ArtworkVisualFeatures | None,
    chronology: float = 0.0,
) -> CarouselSequenceBreakdown:
    position_strength = sum(
        _position_score(feature, index, len(ordered))
        for index, feature in enumerate(ordered)
    )
    transition_quality = sum(
        _transition_quality(ordered[index - 1], ordered[index], theme)
        for index in range(1, len(ordered))
    )
    comparative = sum(
        _comparative_contrast(ordered[index - 1], ordered[index], theme)
        for index in range(1, len(ordered))
    )
    cover_transition = _cover_transition_score(ordered[0], cover) if ordered else 0.0
    return CarouselSequenceBreakdown(
        position_strength=round(position_strength, 4),
        transition_quality=round(transition_quality, 4),
        cover_transition=round(cover_transition, 4),
        comparative_contrast=round(comparative, 4),
        chronology=round(chronology, 4),
    )


def _chronological_sequence(
    features: Sequence[ArtworkSelectionFeatures],
    theme: CarouselThemeDefinition,
    cover: ArtworkVisualFeatures | None,
) -> CarouselSequenceResult:
    ordered = tuple(
        sorted(
            features,
            key=lambda feature: (
                feature.creation_year is None,
                feature.creation_year if feature.creation_year is not None else 0,
                feature.canonical_id,
            ),
        )
    )
    known_count = sum(feature.creation_year is not None for feature in ordered)
    breakdown = _breakdown(ordered, theme, cover, chronology=float(known_count))
    return CarouselSequenceResult(
        ordered_artworks=tuple(feature.artwork for feature in ordered),
        transition_score=round(breakdown.transition_quality, 4),
        sequence_score=breakdown.total,
        breakdown=breakdown,
        beam_width=1,
    )


def sequence_carousel_artworks(
    artworks: Sequence[Mapping[str, object]],
    *,
    theme: CarouselThemeDefinition,
    cover_visual_features: ArtworkVisualFeatures | None = None,
) -> CarouselSequenceResult:
    """Order only the final featured set; the editorial cover remains outside it."""
    if not MIN_FEATURED_WORKS <= len(artworks) <= MAX_FEATURED_WORKS:
        raise ValueError(
            "Carousel sequencing requires between "
            f"{MIN_FEATURED_WORKS} and {MAX_FEATURED_WORKS} featured artworks"
        )
    features = tuple(build_selection_features(artwork, theme) for artwork in artworks)
    if theme.format is CarouselFormat.CHRONOLOGICAL:
        result = _chronological_sequence(features, theme, cover_visual_features)
    else:
        beam: list[tuple[tuple[ArtworkSelectionFeatures, ...], float]] = [((), 0.0)]
        for position in range(len(features)):
            expanded: list[tuple[tuple[ArtworkSelectionFeatures, ...], float]] = []
            for current, score in beam:
                used = {feature.canonical_id for feature in current}
                for candidate in features:
                    if candidate.canonical_id in used:
                        continue
                    increment = _position_score(candidate, position, len(features))
                    if position == 0:
                        increment += _cover_transition_score(candidate, cover_visual_features)
                    else:
                        increment += _transition_quality(current[-1], candidate, theme)
                        increment += _comparative_contrast(current[-1], candidate, theme)
                    expanded.append(((*current, candidate), score + increment))
            beam = sorted(
                expanded,
                key=lambda item: (
                    -item[1],
                    tuple(feature.canonical_id for feature in item[0]),
                ),
            )[:SEQUENCE_BEAM_WIDTH]
        ordered = beam[0][0]
        breakdown = _breakdown(ordered, theme, cover_visual_features)
        result = CarouselSequenceResult(
            ordered_artworks=tuple(feature.artwork for feature in ordered),
            transition_score=round(
                breakdown.transition_quality + breakdown.comparative_contrast, 4
            ),
            sequence_score=breakdown.total,
            breakdown=breakdown,
            beam_width=SEQUENCE_BEAM_WIDTH,
        )

    logger.info(
        "carousel_sequence theme=%s order=%s transition_score=%.2f",
        theme.id,
        ",".join(str(artwork["id"]) for artwork in result.ordered_artworks),
        result.transition_score,
    )
    return result
