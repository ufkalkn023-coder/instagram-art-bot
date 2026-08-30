"""Hard format qualification shared by acquisition, selection, and cover stages."""

from __future__ import annotations

from collections.abc import Mapping

from src.artwork_metadata import (
    identity_keys,
    normalize_artist_identity,
    normalize_medium_family,
    normalize_museum_identity,
    parse_artwork_date,
)
from src.carousel_themes import CarouselFormat, CarouselThemeDefinition
from src.models import NormalizedArtwork
from src.region import normalize_region


def target_query_terms(theme: CarouselThemeDefinition) -> tuple[str, ...]:
    target = theme.format_target
    if target is None:
        return ()
    canonical = target.artist_name or target.museum_name
    return tuple(dict.fromkeys(value for value in (canonical, *target.aliases) if value))


def constrained_adapter_source_ids(theme: CarouselThemeDefinition) -> frozenset[str]:
    target = theme.format_target
    return frozenset(value.casefold() for value in target.source_ids) if target else frozenset()


def _period_matches(value: object, style_or_period: object, target: str) -> bool:
    target_tokens = " ".join(str(target).casefold().split())
    metadata_tokens = " ".join(str(style_or_period or "").casefold().split())
    if target_tokens and target_tokens in metadata_tokens:
        return True
    target_date = parse_artwork_date(target)
    artwork_date = parse_artwork_date(value)
    if target_date is None or artwork_date is None:
        return False
    return not (
        artwork_date.latest_year < target_date.earliest_year
        or artwork_date.earliest_year > target_date.latest_year
    )


def _target_matches_values(
    theme: CarouselThemeDefinition,
    *,
    artist: object,
    museum: object,
    region: object,
    date: object,
    style_or_period: object,
    medium: object,
    classification: object,
) -> tuple[bool, str | None]:
    target = theme.format_target
    if target is None:
        return True, None
    if theme.format is CarouselFormat.MONOGRAPHIC:
        accepted = identity_keys(target.artist_name, target.aliases)
        if normalize_artist_identity(artist) not in accepted:
            return False, "target_artist_mismatch"
    elif theme.format is CarouselFormat.MUSEUM_SPOTLIGHT:
        accepted = identity_keys(target.museum_name, target.aliases, museum=True)
        if normalize_museum_identity(museum) not in accepted:
            return False, "target_museum_mismatch"
    elif theme.format is CarouselFormat.REGIONAL and target.region:
        if normalize_region(region) != normalize_region(target.region):
            return False, "target_region_mismatch"
    elif theme.format is CarouselFormat.PERIOD_FOCUS and target.period:
        if not _period_matches(date, style_or_period, target.period):
            return False, "target_period_mismatch"
    elif theme.format is CarouselFormat.MEDIUM_FOCUS and target.medium_family:
        if normalize_medium_family(medium, classification) != target.medium_family.casefold():
            return False, "target_medium_mismatch"
    return True, None


def _qualify_values(
    theme: CarouselThemeDefinition,
    **metadata: object,
) -> tuple[bool, str | None]:
    """Enforce only intrinsic artist and museum format identities."""
    matches, reason = _target_matches_values(theme, **metadata)
    if theme.format in {
        CarouselFormat.MONOGRAPHIC,
        CarouselFormat.MUSEUM_SPOTLIGHT,
    }:
        return matches, reason
    return True, None


def qualify_normalized_artwork(
    artwork: NormalizedArtwork,
    theme: CarouselThemeDefinition,
) -> tuple[bool, str | None]:
    return _qualify_values(
        theme,
        artist=artwork.artist_name,
        museum=artwork.museum_name,
        region=artwork.region,
        date=artwork.creation_date,
        style_or_period=artwork.style_or_period,
        medium=artwork.medium,
        classification=artwork.classification,
    )


def matches_normalized_format_target(
    artwork: NormalizedArtwork,
    theme: CarouselThemeDefinition,
) -> bool:
    """Return target evidence for scoring without making soft formats hard gates."""
    matches, _ = _target_matches_values(
        theme,
        artist=artwork.artist_name,
        museum=artwork.museum_name,
        region=artwork.region,
        date=artwork.creation_date,
        style_or_period=artwork.style_or_period,
        medium=artwork.medium,
        classification=artwork.classification,
    )
    return matches


def qualify_artwork_mapping(
    artwork: Mapping[str, object],
    theme: CarouselThemeDefinition,
) -> tuple[bool, str | None]:
    return _qualify_values(
        theme,
        artist=artwork.get("artist"),
        museum=artwork.get("museum"),
        region=artwork.get("region"),
        date=artwork.get("date"),
        style_or_period=artwork.get("style_or_period", artwork.get("period")),
        medium=artwork.get("medium"),
        classification=artwork.get("classification"),
    )
