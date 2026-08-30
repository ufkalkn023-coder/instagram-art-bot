"""Grounded editorial facts and deterministic copy for adaptive carousels."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from src.artwork_metadata import (
    ArtworkDateCertainty,
    normalize_artist_identity,
    normalize_medium_family,
    normalize_museum_identity,
    parse_artwork_date,
    period_bucket,
)
from src.artwork_visual_features import ArtworkOrientation, ArtworkVisualFeatures


_UNKNOWN_VALUES = frozenset({"", "unknown", "none", "n/a", "not known"})


def format_count(count: int, singular: str, plural: str | None = None) -> str:
    """Return a grammatically correct, centralized count label."""
    if count < 0:
        raise ValueError("Count cannot be negative")
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def display_theme_title(theme_id: str, registry_title: str | None) -> str:
    """Keep a clean registry title while preventing internal IDs from leaking."""
    title = " ".join(str(registry_title or "").split())
    if not title or title.casefold() == str(theme_id).casefold():
        title = " ".join(str(theme_id).replace("_", " ").replace("-", " ").split()).title()
    return title


def _known_text(value: object) -> str | None:
    text = " ".join(str(value or "").split())
    return text if text.casefold() not in _UNKNOWN_VALUES else None


def _orientation(artwork: Mapping[str, object]) -> ArtworkOrientation | None:
    visual = artwork.get("visual_features")
    if isinstance(visual, ArtworkVisualFeatures):
        orientation = visual.orientation
        return orientation if orientation is not ArtworkOrientation.UNKNOWN else None
    value = visual.get("orientation") if isinstance(visual, Mapping) else None
    value = value or artwork.get("published_orientation") or artwork.get("orientation")
    text = _known_text(value)
    if not text:
        return None
    try:
        orientation = ArtworkOrientation(text.upper())
    except ValueError:
        return None
    return orientation if orientation is not ArtworkOrientation.UNKNOWN else None


@dataclass(frozen=True)
class CarouselEditorialFacts:
    """Facts safe to expose about the final ordered featured set."""

    featured_count: int
    total_slide_count: int
    distinct_artist_count: int
    distinct_museum_count: int
    museum_names: tuple[str, ...]
    distinct_region_count: int
    distinct_medium_count: int
    distinct_period_count: int
    distinct_orientation_count: int
    known_date_count: int
    earliest_year: int | None
    earliest_approximate: bool
    latest_year: int | None
    latest_approximate: bool
    date_span_label: str | None
    artist_metadata_complete: bool
    museum_metadata_complete: bool
    date_metadata_complete: bool
    all_same_museum: bool
    all_same_artist: bool
    format: str
    theme_id: str
    theme_title: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def derive_carousel_editorial_facts(
    featured_artworks: Sequence[Mapping[str, object]],
    *,
    theme_id: str,
    theme_title: str,
    carousel_format: object,
) -> CarouselEditorialFacts:
    """Derive facts only from the final ordered featured artworks."""
    artworks = tuple(featured_artworks)
    artists = [normalize_artist_identity(artwork.get("artist")) for artwork in artworks]
    museums = [normalize_museum_identity(artwork.get("museum")) for artwork in artworks]
    dates = [parse_artwork_date(artwork.get("date")) for artwork in artworks]
    regions = [
        value
        for artwork in artworks
        if (value := _known_text(artwork.get("region")))
        and value.casefold() != "unknown"
    ]
    media = [
        value
        for artwork in artworks
        if (value := normalize_medium_family(
            artwork.get("medium"), artwork.get("classification")
        ))
    ]
    periods = [
        value
        for artwork in artworks
        if (value := period_bucket(artwork.get("date")))
    ]
    orientations = [value for artwork in artworks if (value := _orientation(artwork))]

    museum_names_by_key: dict[str, str] = {}
    for artwork, key in zip(artworks, museums):
        if key is not None:
            museum_names_by_key.setdefault(key, _known_text(artwork.get("museum")) or key)

    known_dates = [date for date in dates if date is not None]
    earliest_year = min((date.earliest_year for date in known_dates), default=None)
    latest_year = max((date.latest_year for date in known_dates), default=None)
    earliest_approximate = bool(
        earliest_year is not None
        and any(
            date.earliest_year == earliest_year and date.earliest_approximate
            for date in known_dates
        )
    )
    latest_approximate = bool(
        latest_year is not None
        and any(
            date.latest_year == latest_year and date.latest_approximate
            for date in known_dates
        )
    )
    dates_complete = bool(artworks) and len(known_dates) == len(artworks)
    has_century_date = any(
        date.certainty is ArtworkDateCertainty.CENTURY for date in known_dates
    )
    date_span_label = None
    if dates_complete and not has_century_date and earliest_year is not None and latest_year is not None:
        earliest_label = f"{'c. ' if earliest_approximate else ''}{earliest_year}"
        latest_label = f"{'c. ' if latest_approximate else ''}{latest_year}"
        date_span_label = (
            earliest_label
            if earliest_year == latest_year
            else f"{earliest_label}\u2013{latest_label}"
        )

    artist_keys = {value for value in artists if value is not None}
    museum_keys = set(museum_names_by_key)
    format_value = getattr(carousel_format, "value", carousel_format)
    return CarouselEditorialFacts(
        featured_count=len(artworks),
        total_slide_count=len(artworks) + 1,
        distinct_artist_count=len(artist_keys),
        distinct_museum_count=len(museum_keys),
        museum_names=tuple(museum_names_by_key.values()),
        distinct_region_count=len({value.casefold() for value in regions}),
        distinct_medium_count=len(set(media)),
        distinct_period_count=len(set(periods)),
        distinct_orientation_count=len(set(orientations)),
        known_date_count=len(known_dates),
        earliest_year=earliest_year,
        earliest_approximate=earliest_approximate,
        latest_year=latest_year,
        latest_approximate=latest_approximate,
        date_span_label=date_span_label,
        artist_metadata_complete=bool(artworks) and all(value is not None for value in artists),
        museum_metadata_complete=bool(artworks) and all(value is not None for value in museums),
        date_metadata_complete=dates_complete,
        all_same_museum=bool(artworks) and len(museum_keys) == 1 and all(value is not None for value in museums),
        all_same_artist=bool(artworks) and len(artist_keys) == 1 and all(value is not None for value in artists),
        format=str(format_value),
        theme_id=theme_id,
        theme_title=display_theme_title(theme_id, theme_title),
    )


def fallback_editorial_subtitle(facts: CarouselEditorialFacts) -> str:
    """Write a grounded cover deck without strengthening the registry label."""
    works = format_count(facts.featured_count, "work")
    if facts.theme_id == "artfolio_selection":
        return f"{works.capitalize()}, selected by Artfolio."
    if facts.all_same_museum:
        museum_name = facts.museum_names[0]
        article = "" if museum_name.casefold().startswith("the ") else "the "
        return f"{works} from {article}{museum_name}, selected around {facts.theme_title}."
    if facts.museum_metadata_complete and facts.distinct_museum_count > 1:
        collections = format_count(facts.distinct_museum_count, "museum collection")
        return f"{works} across {collections}, selected around {facts.theme_title}."
    return f"{works} selected around {facts.theme_title}."


def derive_cover_micro_facts(facts: CarouselEditorialFacts) -> tuple[str, ...]:
    """Choose at most three non-redundant, grounded cover facts by priority."""
    result = [format_count(facts.featured_count, "work")]
    if facts.artist_metadata_complete:
        result.append(format_count(facts.distinct_artist_count, "artist"))
    if facts.museum_metadata_complete and facts.distinct_museum_count > 1:
        result.append(format_count(facts.distinct_museum_count, "collection"))
    elif facts.date_span_label:
        result.append(f"Works from {facts.date_span_label}")
    return tuple(result[:3])


def fallback_carousel_intro(facts: CarouselEditorialFacts, target_name: str | None = None) -> str:
    """Write a compact publishable intro using only final-set facts."""
    works = format_count(facts.featured_count, "work")
    if facts.theme_id == "artfolio_selection":
        opening = f"{works.capitalize()} selected by Artfolio."
    elif facts.format == "MONOGRAPHIC" and target_name:
        opening = f"{works.capitalize()} by {target_name} are brought together around {facts.theme_title}."
    elif facts.format == "MUSEUM_SPOTLIGHT" and target_name:
        opening = f"{works.capitalize()} held by {target_name} are brought together around {facts.theme_title}."
    elif facts.all_same_museum:
        museum_name = facts.museum_names[0]
        article = "" if museum_name.casefold().startswith("the ") else "the "
        opening = (
            f"{works.capitalize()} from {article}{museum_name} are brought together "
            f"around {facts.theme_title}."
        )
    elif facts.museum_metadata_complete and facts.distinct_museum_count > 1:
        collections = format_count(facts.distinct_museum_count, "museum collection")
        opening = (
            f"{works.capitalize()} from {collections} are brought together around "
            f"{facts.theme_title}."
        )
    else:
        opening = f"{works.capitalize()} are brought together around {facts.theme_title}."

    artists = None
    if facts.artist_metadata_complete and facts.distinct_artist_count > 1:
        artists = format_count(facts.distinct_artist_count, "artist")
    if facts.date_span_label and artists:
        return f"{opening} Dated {facts.date_span_label}, they offer perspectives from {artists}."
    if facts.date_span_label:
        return f"{opening} The works date from {facts.date_span_label}."
    if artists and facts.theme_id != "artfolio_selection":
        return f"{opening} Across {artists}, the selection offers different approaches to a shared theme."
    if artists:
        return f"{opening} The selection includes {artists}."
    return opening


_UNSAFE_GENERATED_FACT = re.compile(
    r"(?:\b\d+\b|\b(?:one|two|three|four|five|six|seven|eight)\b|"
    r"\bacross\s+(?:museum\s+)?collections?\b|\bfrom\s+\d+\s+museums?\b)",
    re.IGNORECASE,
)


def grounded_gemini_intro(value: object, facts: CarouselEditorialFacts, fallback: str) -> str:
    """Accept stylistic prose only when it contains no application-owned set facts."""
    text = " ".join(str(value or "").split())
    if not text or _UNSAFE_GENERATED_FACT.search(text):
        return fallback
    if facts.distinct_museum_count <= 1 and re.search(
        r"\b(?:different|multiple|several|varied)\s+(?:museum\s+)?collections?\b|\bacross\s+museums?\b",
        text,
        re.IGNORECASE,
    ):
        return fallback
    if facts.distinct_artist_count <= 1 and re.search(
        r"\b(?:different|multiple|several|varied)\s+artists?\b", text, re.IGNORECASE
    ):
        return fallback
    if not facts.date_metadata_complete and re.search(
        r"\b(?:dates?|chronolog(?:y|ies|ical)|periods?)\b", text, re.IGNORECASE
    ):
        return fallback
    theme_terms = [
        re.escape(term)
        for term in facts.theme_title.casefold().split()
        if len(term) >= 5
    ]
    if theme_terms and re.search(
        rf"\b(?:{'|'.join(theme_terms)})\s+(?:works|artworks|paintings|canvases)\b",
        text,
        re.IGNORECASE,
    ):
        return fallback
    return text
