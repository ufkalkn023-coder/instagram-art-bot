"""Canonical train/serve feature contract for engagement learning."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SpacingBucket = Literal["under_3h", "3h_to_6h", "6h_to_12h", "12h_plus"]

_UNKNOWN_VALUES = frozenset(
    {
        "",
        "unknown",
        "unknown artist",
        "artist unknown",
        "unknown museum",
        "museum unknown",
        "none",
        "n/a",
    }
)
_CANDIDATE_FEATURE_FIELDS = (
    "artist",
    "artist_group",
    "region",
    "period_or_style",
    "semantic_family",
    "museum",
    "source",
    "dominant_color",
    "luminance_bucket",
    "orientation",
)
_CONTEXT_FEATURE_FIELDS = (
    "theme",
    "format",
    "featured_count",
    "cover_variant",
    "caption_hook",
    "publish_slot",
    "weekday",
    "previous_post_spacing_bucket",
)
_SPACING_BUCKETS = frozenset({"under_3h", "3h_to_6h", "6h_to_12h", "12h_plus"})


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _known_text(value: object) -> str | None:
    value = _enum_value(value)
    normalized = " ".join(str(value or "").split())
    return normalized if normalized.casefold() not in _UNKNOWN_VALUES else None


def _first_known(*values: object) -> str | None:
    return next((known for value in values if (known := _known_text(value))), None)


def _nested_features(value: object) -> Mapping[str, object]:
    if isinstance(value, EngagementFeatureVector):
        return value.model_dump(exclude_none=True)
    return value if isinstance(value, Mapping) else {}


def _featured_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= 8:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if 1 <= parsed <= 8 else None
    return None


def previous_post_spacing_bucket(minutes: float | None) -> SpacingBucket | None:
    """Bucket a non-negative gap using the historical learning boundaries."""
    if minutes is None or isinstance(minutes, bool) or minutes < 0:
        return None
    if minutes < 180:
        return "under_3h"
    if minutes < 360:
        return "3h_to_6h"
    if minutes < 720:
        return "6h_to_12h"
    return "12h_plus"


class EngagementFeatureVector(BaseModel):
    """One normalized representation shared by serving and model rebuilds."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    artist: str | None = None
    artist_group: str | None = None
    region: str | None = None
    period_or_style: str | None = None
    semantic_family: str | None = None
    museum: str | None = None
    source: str | None = None
    dominant_color: str | None = None
    luminance_bucket: str | None = None
    orientation: str | None = None
    theme: str | None = None
    format: str | None = None
    featured_count: int | None = Field(default=None, ge=1, le=8)
    cover_variant: str | None = None
    caption_hook: str | None = None
    publish_slot: str | None = None
    weekday: str | None = None
    previous_post_spacing_bucket: SpacingBucket | None = None

    @field_validator(
        "artist",
        "artist_group",
        "region",
        "period_or_style",
        "semantic_family",
        "museum",
        "source",
        "dominant_color",
        "luminance_bucket",
        "orientation",
        "theme",
        "format",
        "cover_variant",
        "caption_hook",
        "publish_slot",
        "weekday",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        return _known_text(value)

    @classmethod
    def from_candidate(cls, artwork: Mapping[str, object]) -> "EngagementFeatureVector":
        canonical = _nested_features(artwork.get("engagement_features"))
        visual = artwork.get("visual_features")
        visual_color = getattr(visual, "dominant_color_family", None)
        visual_luminance = getattr(visual, "luminance_bucket", None)
        visual_orientation = getattr(visual, "orientation", None)
        artist = _first_known(
            canonical.get("artist"), artwork.get("artist"), artwork.get("artist_name")
        )
        artist_group = _first_known(
            canonical.get("artist_group"),
            artwork.get("artist_group"),
            artwork.get("normalized_artist_key"),
        )
        source = _first_known(canonical.get("source"), artwork.get("source"))
        if source is None:
            identifier = artwork.get("id")
            if isinstance(identifier, str) and "_" in identifier:
                source = _known_text(identifier.split("_", 1)[0])
        return cls(
            artist=artist,
            artist_group=artist_group,
            region=_first_known(canonical.get("region"), artwork.get("region")),
            period_or_style=_first_known(
                canonical.get("period_or_style"),
                artwork.get("period_or_style"),
                artwork.get("style_or_period"),
                artwork.get("period"),
            ),
            semantic_family=_first_known(
                canonical.get("semantic_family"),
                artwork.get("semantic_family"),
                artwork.get("visual_category"),
            ),
            museum=_first_known(
                canonical.get("museum"),
                artwork.get("museum"),
                artwork.get("museum_name"),
            ),
            source=source,
            dominant_color=_first_known(
                canonical.get("dominant_color"),
                visual_color,
                artwork.get("dominant_color"),
                artwork.get("visual_color_family"),
            ),
            luminance_bucket=_first_known(
                canonical.get("luminance_bucket"),
                visual_luminance,
                artwork.get("luminance_bucket"),
                artwork.get("visual_tone"),
            ),
            orientation=_first_known(
                canonical.get("orientation"),
                visual_orientation,
                artwork.get("orientation"),
                artwork.get("published_orientation"),
            ),
        )

    @classmethod
    def from_context(cls, context: Mapping[str, object]) -> "EngagementFeatureVector":
        canonical = _nested_features(context.get("engagement_features"))
        raw_minutes = context.get("preceding_post_distance_minutes")
        minutes = (
            float(raw_minutes)
            if isinstance(raw_minutes, (int, float)) and not isinstance(raw_minutes, bool)
            else None
        )
        raw_spacing = _first_known(
            canonical.get("previous_post_spacing_bucket"),
            context.get("previous_post_spacing_bucket"),
            context.get("preceding_distance_bucket"),
            previous_post_spacing_bucket(minutes),
        )
        spacing = raw_spacing if raw_spacing in _SPACING_BUCKETS else None
        return cls(
            theme=_first_known(
                canonical.get("theme"),
                context.get("carousel_theme"),
                context.get("theme"),
            ),
            format=_first_known(
                canonical.get("format"), context.get("carousel_format"), context.get("format")
            ),
            featured_count=_featured_count(
                canonical.get("featured_count") or context.get("featured_count")
            ),
            cover_variant=_first_known(
                canonical.get("cover_variant"), context.get("cover_variant")
            ),
            caption_hook=_first_known(
                canonical.get("caption_hook"),
                context.get("caption_hook"),
                context.get("caption_hook_type"),
            ),
            publish_slot=_first_known(
                canonical.get("publish_slot"), context.get("publish_slot")
            ),
            weekday=_first_known(
                canonical.get("weekday"),
                context.get("weekday"),
                context.get("publication_weekday"),
            ),
            previous_post_spacing_bucket=spacing,
        )

    def candidate_feature_keys(self) -> tuple[str, ...]:
        return self._feature_keys(_CANDIDATE_FEATURE_FIELDS)

    def context_feature_keys(self) -> tuple[str, ...]:
        return self._feature_keys(_CONTEXT_FEATURE_FIELDS)

    def _feature_keys(self, fields: tuple[str, ...]) -> tuple[str, ...]:
        keys = []
        for name in fields:
            value = getattr(self, name)
            normalized = _known_text(value)
            if normalized is not None:
                keys.append(f"{name}:{normalized.casefold()}")
        return tuple(keys)
