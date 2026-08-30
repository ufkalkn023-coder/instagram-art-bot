"""Typed carousel-theme registry and deterministic editorial theme planning."""

from __future__ import annotations

import hashlib
import json
import logging
import random
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.region import REGION_UNKNOWN, REGION_VOCABULARY

logger = logging.getLogger(__name__)

DEFAULT_THEME_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "carousel_themes.json"
THEME_HISTORY_WINDOW = 12
BASE_THEME_SCORE = 50.0
MAX_SEASONAL_BOOST = 3.0
MAX_SERENDIPITY_BOOST = 5.0


class CarouselFormat(str, Enum):
    THEMATIC_COLLECTION = "THEMATIC_COLLECTION"
    COMPARATIVE = "COMPARATIVE"
    CHRONOLOGICAL = "CHRONOLOGICAL"
    COLOR_STUDY = "COLOR_STUDY"
    LIGHT_STUDY = "LIGHT_STUDY"
    REGIONAL = "REGIONAL"
    PERIOD_FOCUS = "PERIOD_FOCUS"
    MEDIUM_FOCUS = "MEDIUM_FOCUS"
    VISUAL_PATTERN = "VISUAL_PATTERN"
    ICONOGRAPHIC = "ICONOGRAPHIC"
    MONOGRAPHIC = "MONOGRAPHIC"
    MUSEUM_SPOTLIGHT = "MUSEUM_SPOTLIGHT"


class ThemeEvidenceMode(str, Enum):
    """Evidence required before an artwork is publication-eligible for a theme."""

    METADATA = "METADATA"
    HYBRID = "HYBRID"
    IMAGE = "IMAGE"


class ThemeVisualTarget(BaseModel):
    """Deterministic pixel properties that may support a visual theme."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    color_families: tuple[str, ...] = ()
    luminance_buckets: tuple[str, ...] = ()
    contrast_buckets: tuple[str, ...] = ()

    @field_validator("color_families", "luminance_buckets", "contrast_buckets", mode="before")
    @classmethod
    def validate_visual_values(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ()):
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("visual target values must be arrays")
        normalized = tuple(str(item).strip().upper() for item in value)
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("visual target values must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def validate_known_values(self) -> ThemeVisualTarget:
        allowed_colors = {
            "RED", "ORANGE", "YELLOW", "GREEN", "CYAN", "BLUE", "PURPLE",
            "NEUTRAL", "DARK", "LIGHT",
        }
        if not set(self.color_families) <= allowed_colors:
            raise ValueError("unknown visual target color family")
        if not set(self.luminance_buckets) <= {"DARK", "MID", "LIGHT"}:
            raise ValueError("unknown visual target luminance bucket")
        if not set(self.contrast_buckets) <= {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("unknown visual target contrast bucket")
        if not (self.color_families or self.luminance_buckets or self.contrast_buckets):
            raise ValueError("visual target must declare at least one measurable property")
        return self


class FormatTargetDimension(str, Enum):
    ARTIST = "artist"
    MUSEUM = "museum"
    REGION = "region"
    PERIOD = "period"
    MEDIUM = "medium"


class ComparisonDimension(str, Enum):
    ARTIST = "artist"
    MUSEUM = "museum"
    REGION = "region"
    PERIOD = "period"
    MEDIUM = "medium"
    LUMINANCE = "luminance"
    COLOR = "color"
    ORIENTATION = "orientation"


class ChronologicalDimension(str, Enum):
    CREATION_DATE = "creation_date"


class CarouselFormatTarget(BaseModel):
    """Explicit, typed identity or metadata target for one format contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artist_name: str | None = None
    museum_name: str | None = None
    region: str | None = None
    period: str | None = None
    medium_family: str | None = None
    comparison_dimension: ComparisonDimension | None = None
    chronological_dimension: ChronologicalDimension | None = None
    aliases: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()

    @field_validator(
        "artist_name",
        "museum_name",
        "region",
        "period",
        "medium_family",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not (cleaned := " ".join(value.split())):
            raise ValueError("format target values must be non-empty strings")
        return cleaned

    @field_validator("aliases", "source_ids", mode="before")
    @classmethod
    def normalize_target_arrays(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ()):
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("format target aliases/source_ids must be arrays")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not (normalized := " ".join(item.split())):
                raise ValueError("format target aliases/source_ids must contain non-empty strings")
            key = normalized.casefold()
            if key in seen:
                raise ValueError(f"duplicate format target entry: {normalized}")
            seen.add(key)
            cleaned.append(normalized)
        return tuple(cleaned)


@dataclass(frozen=True)
class CarouselFormatPolicy:
    """One testable source for format hard constraints and soft diversity behavior."""

    required_target_dimension: FormatTargetDimension | None = None
    strict_artist_cap: int | None = 1
    relaxed_artist_cap: int | None = 2
    strict_museum_cap: int | None = 3
    relaxed_museum_cap: int | None = 4
    strict_region_cap: int | None = 2
    relaxed_region_cap: int | None = 3
    penalize_artist_similarity: bool = True
    penalize_museum_similarity: bool = True
    penalize_region_similarity: bool = True
    penalize_period_similarity: bool = True
    penalize_medium_similarity: bool = True
    penalize_color_similarity: bool = True
    penalize_luminance_similarity: bool = True
    penalize_orientation_similarity: bool = True
    penalize_semantic_similarity: bool = True
    artist_diversity_weight: float = 8.0
    museum_diversity_weight: float = 4.0
    region_diversity_weight: float = 3.0
    period_diversity_weight: float = 3.0
    medium_diversity_weight: float = 2.0
    sequencing_strategy: str = "narrative"


FORMAT_POLICIES: dict[CarouselFormat, CarouselFormatPolicy] = {
    CarouselFormat.THEMATIC_COLLECTION: CarouselFormatPolicy(),
    CarouselFormat.COMPARATIVE: CarouselFormatPolicy(
        region_diversity_weight=5.0,
        period_diversity_weight=6.0,
        medium_diversity_weight=4.0,
        penalize_semantic_similarity=False,
        sequencing_strategy="comparative_contrast",
    ),
    CarouselFormat.CHRONOLOGICAL: CarouselFormatPolicy(
        penalize_period_similarity=False,
        period_diversity_weight=8.0,
        sequencing_strategy="chronological",
    ),
    CarouselFormat.COLOR_STUDY: CarouselFormatPolicy(penalize_color_similarity=False),
    CarouselFormat.LIGHT_STUDY: CarouselFormatPolicy(penalize_luminance_similarity=False),
    CarouselFormat.REGIONAL: CarouselFormatPolicy(
        strict_region_cap=None,
        relaxed_region_cap=None,
        penalize_region_similarity=False,
        region_diversity_weight=0.0,
        period_diversity_weight=5.0,
        medium_diversity_weight=3.0,
    ),
    CarouselFormat.PERIOD_FOCUS: CarouselFormatPolicy(
        penalize_period_similarity=False,
        period_diversity_weight=0.0,
        region_diversity_weight=4.0,
    ),
    CarouselFormat.MEDIUM_FOCUS: CarouselFormatPolicy(
        penalize_medium_similarity=False,
        medium_diversity_weight=0.0,
    ),
    CarouselFormat.VISUAL_PATTERN: CarouselFormatPolicy(
        penalize_color_similarity=False,
        penalize_luminance_similarity=False,
        penalize_orientation_similarity=False,
    ),
    CarouselFormat.ICONOGRAPHIC: CarouselFormatPolicy(penalize_semantic_similarity=False),
    CarouselFormat.MONOGRAPHIC: CarouselFormatPolicy(
        required_target_dimension=FormatTargetDimension.ARTIST,
        strict_artist_cap=None,
        relaxed_artist_cap=None,
        strict_museum_cap=None,
        relaxed_museum_cap=None,
        strict_region_cap=None,
        relaxed_region_cap=None,
        penalize_artist_similarity=False,
        artist_diversity_weight=0.0,
        museum_diversity_weight=5.0,
        period_diversity_weight=5.0,
        medium_diversity_weight=4.0,
        sequencing_strategy="monographic_variation",
    ),
    CarouselFormat.MUSEUM_SPOTLIGHT: CarouselFormatPolicy(
        required_target_dimension=FormatTargetDimension.MUSEUM,
        strict_museum_cap=None,
        relaxed_museum_cap=None,
        strict_region_cap=None,
        relaxed_region_cap=None,
        penalize_museum_similarity=False,
        artist_diversity_weight=10.0,
        museum_diversity_weight=0.0,
        region_diversity_weight=5.0,
        period_diversity_weight=6.0,
        medium_diversity_weight=4.0,
        sequencing_strategy="museum_variation",
    ),
}


def get_format_policy(carousel_format: CarouselFormat) -> CarouselFormatPolicy:
    return FORMAT_POLICIES[carousel_format]


class ThemeFamily(str, Enum):
    SUBJECT = "subject"
    ANIMALS = "animals"
    NATURE = "nature"
    LANDSCAPE = "landscape"
    MARITIME = "maritime"
    HUMAN_ACTIVITY = "human_activity"
    HUMAN_RELATIONSHIP = "human_relationship"
    GESTURE = "gesture"
    OBJECT = "object"
    INTERIOR = "interior"
    ARCHITECTURE = "architecture"
    CITY = "city"
    SEASON = "season"
    WEATHER = "weather"
    TIME_OF_DAY = "time_of_day"
    LIGHT = "light"
    COLOR = "color"
    ATMOSPHERE = "atmosphere"
    COMPOSITION = "composition"
    PERIOD = "period"
    MEDIUM = "medium"
    REGIONAL = "regional"
    MYTHOLOGY = "mythology"
    RELIGIOUS = "religious"
    COMPARATIVE = "comparative"
    CHRONOLOGICAL = "chronological"
    VISUAL_MOTIF = "visual_motif"


class CarouselThemeDefinition(BaseModel):
    """One validated, stable editorial theme definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=100)
    family: ThemeFamily
    format: CarouselFormat
    description: str = Field(min_length=10, max_length=300)
    primary_queries: tuple[str, ...]
    secondary_queries: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()
    required_term_groups: tuple[tuple[str, ...], ...] = ()
    preferred_terms: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    seasonal_months: tuple[int, ...] = ()
    minimum_candidate_target: int = Field(
        default=12,
        ge=9,
        le=100,
        description=(
            "Preferred metadata acquisition headroom and early-stop target; "
            "publication viability is derived from the minimum distinct 1+5 plan."
        ),
    )
    enabled: bool = True
    editorial_priority: float = Field(default=0.0, ge=-5.0, le=5.0)
    tags: tuple[str, ...] = ()
    format_target: CarouselFormatTarget | None = None
    evidence_mode: ThemeEvidenceMode = ThemeEvidenceMode.METADATA
    visual_target: ThemeVisualTarget | None = None

    @field_validator("title")
    @classmethod
    def validate_english_title(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or not value.isascii() or not any(character.isalpha() for character in value):
            raise ValueError("title must be non-empty English ASCII text")
        return value

    @field_validator(
        "primary_queries",
        "secondary_queries",
        "required_terms",
        "preferred_terms",
        "excluded_terms",
        "aliases",
        "tags",
        mode="before",
    )
    @classmethod
    def validate_text_array(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("value must be an array of strings")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not (normalized := " ".join(item.split())):
                raise ValueError("array entries must be non-empty strings")
            key = normalized.casefold()
            if key in seen:
                raise ValueError(f"duplicate array entry: {normalized}")
            seen.add(key)
            cleaned.append(normalized)
        return tuple(cleaned)

    @field_validator("required_term_groups", mode="before")
    @classmethod
    def validate_required_term_groups(cls, value: Any) -> tuple[tuple[str, ...], ...]:
        """Validate optional AND-of-OR relevance groups without changing flat terms."""
        if value in (None, ()):
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("required_term_groups must be an array of string arrays")
        groups: list[tuple[str, ...]] = []
        for group in value:
            if not isinstance(group, (list, tuple)) or not group:
                raise ValueError("required term groups must be non-empty arrays")
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in group:
                if not isinstance(item, str) or not (normalized := " ".join(item.split())):
                    raise ValueError("required term group entries must be non-empty strings")
                key = normalized.casefold()
                if key in seen:
                    raise ValueError(f"duplicate required term group entry: {normalized}")
                seen.add(key)
                cleaned.append(normalized)
            groups.append(tuple(cleaned))
        return tuple(groups)

    @field_validator("primary_queries")
    @classmethod
    def require_primary_query(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one primary query is required")
        return value

    @field_validator("seasonal_months", mode="before")
    @classmethod
    def validate_months(cls, value: Any) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("seasonal_months must be an array")
        if any(isinstance(month, bool) or not isinstance(month, int) for month in value):
            raise ValueError("seasonal months must be integers")
        months = tuple(value)
        if any(month < 1 or month > 12 for month in months):
            raise ValueError("seasonal months must be within 1..12")
        if len(set(months)) != len(months):
            raise ValueError("seasonal months must be unique")
        return months

    @model_validator(mode="after")
    def validate_format_target(self) -> CarouselThemeDefinition:
        target = self.format_target
        policy = get_format_policy(self.format)
        if self.evidence_mode is ThemeEvidenceMode.METADATA and self.visual_target is not None:
            raise ValueError("METADATA themes cannot declare a visual_target")
        if self.evidence_mode in {ThemeEvidenceMode.HYBRID, ThemeEvidenceMode.IMAGE} and self.visual_target is None:
            raise ValueError("HYBRID/IMAGE themes must declare a visual_target")
        required = policy.required_target_dimension
        if required is FormatTargetDimension.ARTIST and (target is None or not target.artist_name):
            raise ValueError("MONOGRAPHIC themes must declare format_target.artist_name")
        if required is FormatTargetDimension.MUSEUM and (target is None or not target.museum_name):
            raise ValueError("MUSEUM_SPOTLIGHT themes must declare format_target.museum_name")
        if target is None:
            return self
        if target.region and (
            target.region.casefold() not in REGION_VOCABULARY
            or target.region.casefold() == REGION_UNKNOWN
        ):
            raise ValueError("format_target.region must use the controlled region taxonomy")
        if target.medium_family and target.medium_family.casefold() not in {
            "watercolor",
            "pastel",
            "tempera",
            "photograph",
            "textile",
            "print",
            "drawing",
            "oil",
            "other",
        }:
            raise ValueError("format_target.medium_family must use a supported coarse family")
        applicable = {
            CarouselFormat.REGIONAL: target.region,
            CarouselFormat.PERIOD_FOCUS: target.period,
            CarouselFormat.MEDIUM_FOCUS: target.medium_family,
        }
        if self.format in applicable and not applicable[self.format]:
            raise ValueError(f"{self.format.value} format_target is missing its matching target field")
        if target.source_ids and not target.museum_name:
            raise ValueError("format_target.source_ids requires museum_name")
        if target.comparison_dimension and self.format is not CarouselFormat.COMPARATIVE:
            raise ValueError("comparison_dimension is only valid for COMPARATIVE themes")
        if target.chronological_dimension and self.format is not CarouselFormat.CHRONOLOGICAL:
            raise ValueError("chronological_dimension is only valid for CHRONOLOGICAL themes")
        return self


class CarouselThemeRegistry(BaseModel):
    """Validated registry with deterministic ID ordering."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    themes: tuple[CarouselThemeDefinition, ...]

    @field_validator("themes")
    @classmethod
    def validate_and_sort_themes(
        cls, themes: tuple[CarouselThemeDefinition, ...]
    ) -> tuple[CarouselThemeDefinition, ...]:
        counts = Counter(theme.id for theme in themes)
        duplicates = sorted(theme_id for theme_id, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate theme IDs: {', '.join(duplicates)}")
        if not themes:
            raise ValueError("theme registry must not be empty")
        return tuple(sorted(themes, key=lambda theme: theme.id))

    @property
    def enabled_themes(self) -> tuple[CarouselThemeDefinition, ...]:
        return tuple(theme for theme in self.themes if theme.enabled)

    def by_id(self, theme_id: str) -> CarouselThemeDefinition:
        for theme in self.themes:
            if theme.id == theme_id:
                return theme
        raise KeyError(theme_id)


class ThemeRegistryError(RuntimeError):
    """Raised when the data registry is missing, malformed, or invalid."""


@dataclass(frozen=True)
class ThemeHistorySlot:
    """One carousel publication for fatigue calculations."""

    theme_id: str | None
    theme_family: ThemeFamily | None = None
    carousel_format: CarouselFormat | None = None
    publication_id: str | None = None


@dataclass(frozen=True)
class ThemeScoreBreakdown:
    theme_id: str
    family: ThemeFamily
    base: float
    theme_fatigue: float
    family_fatigue: float
    format_fatigue: float
    seasonal: float
    editorial: float
    serendipity: float

    @property
    def total(self) -> float:
        return round(
            self.base
            + self.theme_fatigue
            + self.family_fatigue
            + self.format_fatigue
            + self.seasonal
            + self.editorial
            + self.serendipity,
            6,
        )


@dataclass(frozen=True)
class ThemePlanSelection:
    theme: CarouselThemeDefinition
    score: ThemeScoreBreakdown
    ranked_scores: tuple[ThemeScoreBreakdown, ...]

    def ranked_themes(self, registry: CarouselThemeRegistry) -> tuple[CarouselThemeDefinition, ...]:
        """Return the planner's deterministic order without duplicating score logic."""
        return tuple(registry.by_id(score.theme_id) for score in self.ranked_scores)


def parse_theme_registry(data: Mapping[str, Any]) -> CarouselThemeRegistry:
    """Validate registry data, wrapping implementation-specific validation errors."""
    try:
        return CarouselThemeRegistry.model_validate(data)
    except (TypeError, ValidationError, ValueError) as error:
        raise ThemeRegistryError(f"Invalid carousel theme registry: {error}") from error


def load_theme_registry(path: str | Path = DEFAULT_THEME_REGISTRY_PATH) -> CarouselThemeRegistry:
    registry_path = Path(path)
    try:
        with registry_path.open(encoding="utf-8") as registry_file:
            data = json.load(registry_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ThemeRegistryError(f"Unable to load carousel theme registry {registry_path}: {error}") from error
    if not isinstance(data, Mapping):
        raise ThemeRegistryError("Invalid carousel theme registry: root must be an object")
    return parse_theme_registry(data)


@lru_cache(maxsize=1)
def get_default_theme_registry() -> CarouselThemeRegistry:
    return load_theme_registry(DEFAULT_THEME_REGISTRY_PATH)


def primary_search_query(theme: CarouselThemeDefinition) -> str:
    """Return the first registry query for legacy callers and compact observability."""
    return theme.primary_queries[0]


def _coerce_history_slot(value: ThemeHistorySlot | Mapping[str, Any]) -> ThemeHistorySlot:
    if isinstance(value, ThemeHistorySlot):
        return value
    family = value.get("theme_family")
    carousel_format = value.get("carousel_format")
    try:
        parsed_family = ThemeFamily(family) if family else None
    except (TypeError, ValueError):
        parsed_family = None
    try:
        parsed_format = CarouselFormat(carousel_format) if carousel_format else None
    except (TypeError, ValueError):
        parsed_format = None
    theme_id = value.get("theme_id", value.get("theme"))
    return ThemeHistorySlot(
        theme_id=theme_id if isinstance(theme_id, str) and theme_id else None,
        theme_family=parsed_family,
        carousel_format=parsed_format,
        publication_id=value.get("publication_id") if isinstance(value.get("publication_id"), str) else None,
    )


def _theme_fatigue(count: int) -> float:
    if count == 0:
        return 0.0
    if count == 1:
        return -8.0
    if count == 2:
        return -12.0
    return -16.0


def _family_fatigue(count: int) -> float:
    if count <= 1:
        return 0.0
    if count == 2:
        return -2.0
    if count == 3:
        return -4.0
    return max(-10.0, -6.0 - ((count - 4) * 1.0))


def _format_fatigue(count: int) -> float:
    """Small soft signal; family fatigue remains the primary repetition control."""
    return -min(6.0, count * 2.0)


def _serendipity(run_seed: str, theme_id: str) -> float:
    material = f"{run_seed}\x1ftheme_planner\x1f{theme_id}".encode()
    stable_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")
    return random.Random(stable_seed).uniform(0.0, MAX_SERENDIPITY_BOOST)


def score_theme(
    theme: CarouselThemeDefinition,
    history: Sequence[ThemeHistorySlot | Mapping[str, Any]],
    *,
    run_seed: str,
    current_month: int,
) -> ThemeScoreBreakdown:
    if current_month < 1 or current_month > 12:
        raise ValueError("current_month must be within 1..12")
    recent = tuple(_coerce_history_slot(item) for item in history[-THEME_HISTORY_WINDOW:])
    same_theme_count = sum(slot.theme_id == theme.id for slot in recent)
    same_family_count = sum(slot.theme_family == theme.family for slot in recent)
    same_format_count = sum(slot.carousel_format == theme.format for slot in recent[-6:])
    return ThemeScoreBreakdown(
        theme_id=theme.id,
        family=theme.family,
        base=BASE_THEME_SCORE,
        theme_fatigue=_theme_fatigue(same_theme_count),
        family_fatigue=_family_fatigue(same_family_count),
        format_fatigue=_format_fatigue(same_format_count),
        seasonal=MAX_SEASONAL_BOOST if current_month in theme.seasonal_months else 0.0,
        editorial=theme.editorial_priority,
        serendipity=_serendipity(run_seed, theme.id),
    )


def plan_carousel_theme(
    registry: CarouselThemeRegistry,
    history: Sequence[ThemeHistorySlot | Mapping[str, Any]],
    *,
    run_seed: str,
    current_month: int,
    eligible_evidence_modes: Sequence[ThemeEvidenceMode] | None = None,
) -> ThemePlanSelection:
    """Choose the highest-scoring enabled theme without global RNG mutation."""
    ranked = rank_carousel_themes(
        registry,
        history,
        run_seed=run_seed,
        current_month=current_month,
    )
    if eligible_evidence_modes is not None:
        allowed_modes = frozenset(eligible_evidence_modes)
        ranked = tuple(
            score
            for score in ranked
            if registry.by_id(score.theme_id).evidence_mode in allowed_modes
        )
        if not ranked:
            raise ThemeRegistryError(
                "Carousel theme registry has no enabled themes for the requested evidence modes"
            )
    enabled = tuple(registry.by_id(score.theme_id) for score in ranked)
    selected_score = ranked[0]
    selected_theme = registry.by_id(selected_score.theme_id)
    if eligible_evidence_modes is None:
        logger.info(
            "theme_eligibility enabled=%s disabled=%s recent_publication_slots=%s month=%s",
            len(enabled),
            len(registry.themes) - len(enabled),
            min(len(history), THEME_HISTORY_WINDOW),
            current_month,
        )
    else:
        logger.info(
            "theme_eligibility enabled=%s disabled=%s production_eligible=%s "
            "evidence_modes=%s recent_publication_slots=%s month=%s",
            len(registry.enabled_themes),
            len(registry.themes) - len(registry.enabled_themes),
            len(enabled),
            ",".join(mode.value for mode in eligible_evidence_modes),
            min(len(history), THEME_HISTORY_WINDOW),
            current_month,
        )
    logger.info(
        "theme_selected id=%s family=%s format=%s base=%.2f fatigue=%.2f "
        "family_fatigue=%.2f format_fatigue=%.2f seasonal=%.2f editorial=%.2f "
        "serendipity=%.2f total=%.2f",
        selected_theme.id,
        selected_theme.family.value,
        selected_theme.format.value,
        selected_score.base,
        selected_score.theme_fatigue,
        selected_score.family_fatigue,
        selected_score.format_fatigue,
        selected_score.seasonal,
        selected_score.editorial,
        selected_score.serendipity,
        selected_score.total,
    )
    logger.info(
        "theme_runner_up_summary %s",
        ",".join(f"{score.theme_id}:{score.total:.2f}" for score in ranked[1:4]) or "none",
    )
    return ThemePlanSelection(selected_theme, selected_score, ranked)


def rank_carousel_themes(
    registry: CarouselThemeRegistry,
    history: Sequence[ThemeHistorySlot | Mapping[str, Any]],
    *,
    run_seed: str,
    current_month: int,
) -> tuple[ThemeScoreBreakdown, ...]:
    """Return every enabled theme in deterministic planner order."""
    enabled = registry.enabled_themes
    if not enabled:
        raise ThemeRegistryError("Carousel theme registry has no enabled themes")
    return tuple(
        sorted(
            (
                score_theme(theme, history, run_seed=run_seed, current_month=current_month)
                for theme in enabled
            ),
            key=lambda score: (-score.total, score.theme_id),
        )
    )
