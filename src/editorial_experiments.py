"""Small deterministic taxonomies for learnable carousel experiments."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum

from src.carousel_themes import CarouselFormat, CarouselThemeDefinition


SELECTION_MODEL_VERSION = "carousel_learning_v1"
ENGAGEMENT_MODEL_VERSION = "engagement_rates_v1"


class CoverVariant(str, Enum):
    """Current cover treatment plus a stable seam for future visual variants."""

    EDITORIAL = "editorial"


class CaptionHookType(str, Enum):
    VISUAL_DETAIL = "visual_detail"
    CURIOSITY = "curiosity"
    CONTRAST = "contrast"
    ARTIST_FOCUS = "artist_focus"
    HISTORICAL_CONTEXT = "historical_context"
    QUESTION = "question"
    COMPOSITION = "composition"


PUBLISH_SLOT_HOURS = {
    "slot_1": 5,
    "slot_2": 10,
    "slot_3": 15,
    "slot_4": 20,
}


def canonical_publish_slot(value: datetime | None = None) -> str:
    """Map a run to the nearest canonical UTC slot, including delayed schedules."""
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Publish time must be timezone-aware")
    utc_value = timestamp.astimezone(timezone.utc)
    minute_of_day = utc_value.hour * 60 + utc_value.minute
    return min(
        PUBLISH_SLOT_HOURS,
        key=lambda slot: (
            min(
                abs(minute_of_day - PUBLISH_SLOT_HOURS[slot] * 60),
                1440 - abs(minute_of_day - PUBLISH_SLOT_HOURS[slot] * 60),
            ),
            slot,
        ),
    )


def select_caption_hook_type(
    theme: CarouselThemeDefinition,
    *,
    run_seed: str,
) -> CaptionHookType:
    """Choose a grounded hook taxonomy without mutating global random state."""
    fixed = {
        CarouselFormat.COMPARATIVE: CaptionHookType.CONTRAST,
        CarouselFormat.MONOGRAPHIC: CaptionHookType.ARTIST_FOCUS,
        CarouselFormat.CHRONOLOGICAL: CaptionHookType.HISTORICAL_CONTEXT,
        CarouselFormat.PERIOD_FOCUS: CaptionHookType.HISTORICAL_CONTEXT,
        CarouselFormat.COLOR_STUDY: CaptionHookType.VISUAL_DETAIL,
        CarouselFormat.LIGHT_STUDY: CaptionHookType.COMPOSITION,
        CarouselFormat.VISUAL_PATTERN: CaptionHookType.COMPOSITION,
    }
    if theme.format in fixed:
        return fixed[theme.format]
    choices = (
        CaptionHookType.VISUAL_DETAIL,
        CaptionHookType.CURIOSITY,
        CaptionHookType.QUESTION,
        CaptionHookType.COMPOSITION,
    )
    material = f"{run_seed}\x1fcaption_hook\x1f{theme.id}".encode()
    index = int.from_bytes(hashlib.sha256(material).digest(), "big") % len(choices)
    return choices[index]
