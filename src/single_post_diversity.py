"""Deterministic, bounded recent-history diversity scoring for single posts."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from src.artwork_metadata import normalize_artist_identity
from src.artwork_visual_features import (
    ArtworkVisualFeatures,
    DominantColorFamily,
    LuminanceBucket,
)


# Twelve single publications cover the visible feed neighborhood without making
# an artist or visual choice linger indefinitely. Publication rows from carousels
# are excluded before this limit is applied.
SINGLE_DIVERSITY_HISTORY_WINDOW = 12
SQUARE_RELATIVE_TOLERANCE = 0.02


class SinglePostOrientation(str, Enum):
    PORTRAIT = "PORTRAIT"
    SQUARE = "SQUARE"
    LANDSCAPE = "LANDSCAPE"
    UNKNOWN = "UNKNOWN"


class SemanticFamily(str, Enum):
    LANDSCAPE = "LANDSCAPE"
    PORTRAITURE = "PORTRAITURE"
    STILL_LIFE = "STILL_LIFE"
    RELIGIOUS = "RELIGIOUS"
    ABSTRACT = "ABSTRACT"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VisualCategoryFingerprint:
    """Small honest fingerprint; orientation is deliberately scored elsewhere."""

    semantic_family: SemanticFamily = SemanticFamily.UNKNOWN
    tone: LuminanceBucket = LuminanceBucket.UNKNOWN
    color_family: DominantColorFamily = DominantColorFamily.UNKNOWN


@dataclass(frozen=True)
class SinglePostDiversityFeatures:
    orientation: SinglePostOrientation
    artist_key: str | None
    visual_category: VisualCategoryFingerprint


@dataclass(frozen=True)
class SinglePostDiversityScore:
    """Explainable selection-only adjustments for one single-post candidate."""

    orientation: float
    artist: float
    visual_category: float
    orientation_count: int = 0
    orientation_streak: int = 0
    artist_count: int = 0
    immediate_artist_repeat: bool = False
    semantic_count: int = 0
    semantic_streak: int = 0
    tone_count: int = 0
    color_count: int = 0
    fingerprint_streak: int = 0

    @property
    def total(self) -> float:
        return round(self.orientation + self.artist + self.visual_category, 4)


def classify_orientation(
    width: object,
    height: object,
) -> SinglePostOrientation:
    """Classify displayed dimensions with a two-percent square tolerance."""
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        return SinglePostOrientation.UNKNOWN
    if abs(width - height) / max(width, height) <= SQUARE_RELATIVE_TOLERANCE:
        return SinglePostOrientation.SQUARE
    return (
        SinglePostOrientation.PORTRAIT
        if width < height
        else SinglePostOrientation.LANDSCAPE
    )


def normalized_artist_key(value: object) -> str | None:
    """Reuse canonical exact identity normalization; unknowns have no identity."""
    return normalize_artist_identity(value)


def _normalized_metadata_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = [
        character
        if character.isalnum() or unicodedata.category(character).startswith("M")
        else " "
        for character in normalized
    ]
    return " ".join("".join(characters).split())


_SEMANTIC_PHRASES: tuple[tuple[SemanticFamily, tuple[str, ...]], ...] = (
    (SemanticFamily.STILL_LIFE, ("still life", "still lifes", "nature morte")),
    (
        SemanticFamily.PORTRAITURE,
        ("portrait", "portraits", "portraiture", "self portrait"),
    ),
    (
        SemanticFamily.LANDSCAPE,
        ("landscape", "landscapes", "seascape", "cityscape"),
    ),
    (
        SemanticFamily.RELIGIOUS,
        ("religious art", "religious painting", "sacred art", "devotional image"),
    ),
    (
        SemanticFamily.ABSTRACT,
        ("abstract", "abstract art", "abstract painting", "nonobjective art"),
    ),
)


def _family_from_evidence(value: object) -> SemanticFamily | None:
    normalized = _normalized_metadata_text(value)
    if not normalized:
        return None
    for family, phrases in _SEMANTIC_PHRASES:
        for phrase in phrases:
            if re.search(rf"(?:^| )({re.escape(phrase)})(?: |$)", normalized):
                return family
    return None


def infer_semantic_family(candidate: object) -> SemanticFamily:
    """Infer only from explicit museum metadata, never title, artist, or pixels."""
    # Evidence precedence is intentionally stable and conservative.
    for attribute in ("classification", "department", "style_or_period", "medium"):
        if family := _family_from_evidence(getattr(candidate, attribute, None)):
            return family
    return SemanticFamily.UNKNOWN


def _visual_fingerprint(
    candidate: object,
    visual_features: ArtworkVisualFeatures | None,
) -> VisualCategoryFingerprint:
    return VisualCategoryFingerprint(
        semantic_family=infer_semantic_family(candidate),
        tone=(
            visual_features.luminance_bucket
            if visual_features is not None
            else LuminanceBucket.UNKNOWN
        ),
        color_family=(
            visual_features.dominant_color_family
            if visual_features is not None
            else DominantColorFamily.UNKNOWN
        ),
    )


def candidate_diversity_features(
    candidate: object,
    visual_features: ArtworkVisualFeatures | None = None,
) -> SinglePostDiversityFeatures:
    width = (
        visual_features.width
        if visual_features is not None
        else getattr(candidate, "image_width", None)
    )
    height = (
        visual_features.height
        if visual_features is not None
        else getattr(candidate, "image_height", None)
    )
    return SinglePostDiversityFeatures(
        orientation=classify_orientation(width, height),
        artist_key=normalized_artist_key(getattr(candidate, "artist_name", None)),
        visual_category=_visual_fingerprint(candidate, visual_features),
    )


def recent_single_publications(
    history: Sequence[Mapping[str, object]],
    limit: int = SINGLE_DIVERSITY_HISTORY_WINDOW,
) -> list[Mapping[str, object]]:
    """Return recent single publication events, excluding carousel/reel rows."""
    if limit < 1:
        raise ValueError("Single diversity history limit must be positive")

    publications: list[Mapping[str, object]] = []
    seen_publication_ids: set[str] = set()
    for record in history:
        if not isinstance(record, Mapping):
            continue
        publication_type = str(record.get("publication_type", "")).strip().upper()
        content_type = str(record.get("content_type", "")).strip().upper()
        if publication_type in {"CAROUSEL", "REEL"}:
            continue
        if content_type.startswith(("CAROUSEL", "REEL")):
            continue
        if record.get("publication_role") or record.get("theme_id") or record.get("theme"):
            continue

        publication_id = record.get("publication_id")
        if isinstance(publication_id, str) and publication_id:
            if publication_id in seen_publication_ids:
                continue
            seen_publication_ids.add(publication_id)
        publications.append(record)

    return publications[-limit:]


def _enum_value(enum_type, value: object, default):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _history_orientation(record: Mapping[str, object]) -> SinglePostOrientation:
    stored = _enum_value(
        SinglePostOrientation,
        record.get("published_orientation"),
        SinglePostOrientation.UNKNOWN,
    )
    if stored is not SinglePostOrientation.UNKNOWN:
        return stored

    for width_key, height_key in (
        ("published_width", "published_height"),
        ("source_width", "source_height"),
        ("image_width", "image_height"),
    ):
        width = record.get(width_key)
        height = record.get(height_key)
        if (
            width_key == "image_width"
            and record.get("exif_orientation") in {5, 6, 7, 8}
        ):
            width, height = height, width
        orientation = classify_orientation(width, height)
        if orientation is not SinglePostOrientation.UNKNOWN:
            return orientation
    return SinglePostOrientation.UNKNOWN


_LEGACY_SEMANTIC_FAMILIES = {
    "portrait": SemanticFamily.PORTRAITURE,
    "portraiture": SemanticFamily.PORTRAITURE,
    "landscape": SemanticFamily.LANDSCAPE,
    "seascape": SemanticFamily.LANDSCAPE,
    "still_life": SemanticFamily.STILL_LIFE,
    "still life": SemanticFamily.STILL_LIFE,
    "religious": SemanticFamily.RELIGIOUS,
    "abstract": SemanticFamily.ABSTRACT,
}


def _history_fingerprint(record: Mapping[str, object]) -> VisualCategoryFingerprint:
    semantic = _enum_value(
        SemanticFamily,
        record.get("semantic_family"),
        SemanticFamily.UNKNOWN,
    )
    if semantic is SemanticFamily.UNKNOWN:
        legacy = record.get("visual_category")
        if isinstance(legacy, str):
            semantic = _LEGACY_SEMANTIC_FAMILIES.get(
                legacy.strip().casefold(),
                SemanticFamily.UNKNOWN,
            )
    return VisualCategoryFingerprint(
        semantic_family=semantic,
        tone=_enum_value(
            LuminanceBucket,
            record.get("visual_tone"),
            LuminanceBucket.UNKNOWN,
        ),
        color_family=_enum_value(
            DominantColorFamily,
            record.get("visual_color_family"),
            DominantColorFamily.UNKNOWN,
        ),
    )


def _history_artist(record: Mapping[str, object]) -> str | None:
    stored = record.get("normalized_artist_key")
    if isinstance(stored, str) and stored.strip():
        return normalized_artist_key(stored)
    return normalized_artist_key(record.get("artist_name", record.get("artist")))


def _trailing_streak(values: Sequence[object], target: object) -> int:
    streak = 0
    for value in reversed(values):
        if value != target:
            break
        streak += 1
    return streak


def _orientation_adjustment(count: int, streak: int) -> float:
    frequency = 0.0
    if count == 2:
        frequency = -0.75
    elif count == 3:
        frequency = -1.5
    elif count == 4:
        frequency = -2.5
    elif count == 5:
        frequency = -3.5
    elif count >= 6:
        frequency = -4.5

    streak_penalty = 0.0
    if streak == 2:
        streak_penalty = -1.0
    elif streak == 3:
        streak_penalty = -2.0
    elif streak >= 4:
        streak_penalty = -3.0
    return round(max(-7.5, frequency + streak_penalty), 2)


def _artist_adjustment(count: int, immediate_repeat: bool) -> float:
    frequency = 0.0
    if count == 1:
        frequency = -1.0
    elif count == 2:
        frequency = -2.5
    elif count >= 3:
        frequency = -4.0
    return round(max(-6.0, frequency - (2.0 if immediate_repeat else 0.0)), 2)


def _frequency_penalty(count: int, tiers: tuple[tuple[int, float], ...]) -> float:
    penalty = 0.0
    for threshold, value in tiers:
        if count >= threshold:
            penalty = value
    return penalty


def _known_fingerprint_match(
    left: VisualCategoryFingerprint,
    right: VisualCategoryFingerprint,
) -> bool:
    pairs = (
        (left.semantic_family, right.semantic_family, SemanticFamily.UNKNOWN),
        (left.tone, right.tone, LuminanceBucket.UNKNOWN),
        (left.color_family, right.color_family, DominantColorFamily.UNKNOWN),
    )
    comparable = [
        (a, b) for a, b, unknown in pairs if a is not unknown and b is not unknown
    ]
    return len(comparable) >= 2 and all(a == b for a, b in comparable)


def _visual_adjustment(
    candidate: VisualCategoryFingerprint,
    history: Sequence[VisualCategoryFingerprint],
) -> tuple[float, int, int, int, int, int]:
    semantic_values = [item.semantic_family for item in history]
    tone_values = [item.tone for item in history]
    color_values = [item.color_family for item in history]

    semantic_count = 0
    semantic_streak = 0
    if candidate.semantic_family is not SemanticFamily.UNKNOWN:
        semantic_count = semantic_values.count(candidate.semantic_family)
        semantic_streak = _trailing_streak(semantic_values, candidate.semantic_family)
    tone_count = (
        tone_values.count(candidate.tone)
        if candidate.tone is not LuminanceBucket.UNKNOWN
        else 0
    )
    color_count = (
        color_values.count(candidate.color_family)
        if candidate.color_family is not DominantColorFamily.UNKNOWN
        else 0
    )

    fingerprint_streak = 0
    for item in reversed(history):
        if not _known_fingerprint_match(candidate, item):
            break
        fingerprint_streak += 1

    penalty = _frequency_penalty(
        semantic_count,
        ((1, -0.5), (2, -1.0), (3, -1.5)),
    )
    if semantic_streak == 1:
        penalty -= 0.5
    elif semantic_streak >= 2:
        penalty -= 0.75
    penalty += _frequency_penalty(tone_count, ((2, -0.25), (3, -0.5), (5, -0.75)))
    penalty += _frequency_penalty(color_count, ((2, -0.25), (3, -0.5), (5, -0.75)))
    if fingerprint_streak == 1:
        penalty -= 1.0
    elif fingerprint_streak >= 2:
        penalty -= 1.5
    return (
        round(max(-5.0, penalty), 2),
        semantic_count,
        semantic_streak,
        tone_count,
        color_count,
        fingerprint_streak,
    )


def score_single_post_diversity(
    features: SinglePostDiversityFeatures,
    recent_history: Sequence[Mapping[str, object]],
) -> SinglePostDiversityScore:
    """Score one candidate against the bounded single-publication event window."""
    history = recent_single_publications(recent_history)
    orientations = [_history_orientation(record) for record in history]
    orientation_count = 0
    orientation_streak = 0
    if features.orientation is not SinglePostOrientation.UNKNOWN:
        orientation_count = orientations.count(features.orientation)
        orientation_streak = _trailing_streak(orientations, features.orientation)

    artists = [_history_artist(record) for record in history]
    artist_count = (
        artists.count(features.artist_key) if features.artist_key is not None else 0
    )
    immediate_artist_repeat = bool(
        features.artist_key is not None and artists and artists[-1] == features.artist_key
    )

    (
        visual_adjustment,
        semantic_count,
        semantic_streak,
        tone_count,
        color_count,
        fingerprint_streak,
    ) = _visual_adjustment(
        features.visual_category,
        [_history_fingerprint(record) for record in history],
    )

    return SinglePostDiversityScore(
        orientation=_orientation_adjustment(orientation_count, orientation_streak),
        artist=(
            _artist_adjustment(artist_count, immediate_artist_repeat)
            if features.artist_key is not None
            else 0.0
        ),
        visual_category=visual_adjustment,
        orientation_count=orientation_count,
        orientation_streak=orientation_streak,
        artist_count=artist_count,
        immediate_artist_repeat=immediate_artist_repeat,
        semantic_count=semantic_count,
        semantic_streak=semantic_streak,
        tone_count=tone_count,
        color_count=color_count,
        fingerprint_streak=fingerprint_streak,
    )


def history_metadata(
    features: SinglePostDiversityFeatures,
) -> dict[str, str | None]:
    """Return the minimum stable fields needed by future single scoring."""
    return {
        "published_orientation": features.orientation.value,
        "normalized_artist_key": features.artist_key,
        "semantic_family": features.visual_category.semantic_family.value,
        "visual_tone": features.visual_category.tone.value,
        "visual_color_family": features.visual_category.color_family.value,
    }
