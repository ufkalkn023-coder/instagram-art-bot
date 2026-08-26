"""Deterministic normalization for artwork identities and coarse metadata."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class ArtworkDateCertainty(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    RANGE = "range"
    CENTURY = "century"


@dataclass(frozen=True)
class ArtworkDateInfo:
    earliest_year: int
    latest_year: int
    representative_year: int
    certainty: ArtworkDateCertainty
    earliest_approximate: bool = False
    latest_approximate: bool = False


def _plain_identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    if not normalized:
        return None
    characters = [
        character if character.isalnum() or unicodedata.category(character).startswith("M") else " "
        for character in normalized
    ]
    result = " ".join("".join(characters).split())
    return result or None


def normalize_artist_identity(value: object) -> str | None:
    """Normalize an explicit artist name without fuzzy or inferred matching."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    initial_result = _plain_identity(normalized)
    if initial_result in {
        "unknown",
        "unknown artist",
        "artist unknown",
        "unknown maker",
        "maker unknown",
        "unidentified artist",
        "unidentified maker",
        "anonymous",
        "anonymous artist",
        "artist anonymous",
    }:
        return None
    if normalized.count(",") == 1:
        surname, given = normalized.split(",", 1)
        if surname.strip() and given.strip():
            normalized = f"{given.strip()} {surname.strip()}"
    result = _plain_identity(normalized)
    return result


def normalize_museum_identity(value: object) -> str | None:
    """Normalize explicit museum metadata; aliases remain registry-controlled."""
    result = _plain_identity(value)
    if result in {"unknown", "unknown museum", "museum unknown"}:
        return None
    return result


def identity_keys(
    canonical_name: str | None,
    aliases: tuple[str, ...],
    *,
    museum: bool = False,
) -> frozenset[str]:
    normalize = normalize_museum_identity if museum else normalize_artist_identity
    return frozenset(
        key for value in (canonical_name, *aliases) if (key := normalize(value)) is not None
    )


def parse_artwork_date(value: object) -> ArtworkDateInfo | None:
    """Parse only common, unambiguous CE date forms and preserve their uncertainty."""
    raw = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    if not raw:
        return None
    raw = raw.replace("–", "-").replace("—", "-")

    century = re.fullmatch(r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\s+century", raw)
    if century:
        number = int(century.group(1))
        if 6 <= number <= 21:
            earliest = (number - 1) * 100
            latest = earliest + 99
            return ArtworkDateInfo(
                earliest,
                latest,
                (earliest + latest) // 2,
                ArtworkDateCertainty.CENTURY,
            )

    date_range = re.fullmatch(
        r"(?:(c|ca|circa)\.?\s*)?(\d{3,4})\s*-\s*(\d{3,4})",
        raw,
    )
    if date_range:
        approximate = date_range.group(1) is not None
        earliest, latest = (int(date_range.group(2)), int(date_range.group(3)))
        if 500 <= earliest <= latest <= 2100:
            return ArtworkDateInfo(
                earliest,
                latest,
                (earliest + latest) // 2,
                ArtworkDateCertainty.RANGE,
                earliest_approximate=approximate,
                latest_approximate=approximate,
            )

    abbreviated_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{2})", raw)
    if abbreviated_range:
        earliest = int(abbreviated_range.group(1))
        latest = (earliest // 100) * 100 + int(abbreviated_range.group(2))
        if 500 <= earliest <= latest <= 2100:
            return ArtworkDateInfo(
                earliest,
                latest,
                (earliest + latest) // 2,
                ArtworkDateCertainty.RANGE,
            )

    point = re.fullmatch(r"(?:(c|ca|circa)\.?\s*)?(\d{3,4})", raw)
    if point:
        year = int(point.group(2))
        if 500 <= year <= 2100:
            certainty = (
                ArtworkDateCertainty.APPROXIMATE
                if point.group(1)
                else ArtworkDateCertainty.EXACT
            )
            approximate = certainty is ArtworkDateCertainty.APPROXIMATE
            return ArtworkDateInfo(
                year,
                year,
                year,
                certainty,
                earliest_approximate=approximate,
                latest_approximate=approximate,
            )
    return None


def period_bucket(value: object) -> str | None:
    info = parse_artwork_date(value)
    if info is None:
        return None
    year = info.representative_year
    if year < 1500:
        return "pre-1500"
    if year < 1600:
        return "1500-1599"
    if year < 1700:
        return "1600-1699"
    if year < 1800:
        return "1700-1799"
    if year < 1850:
        return "1800-1849"
    if year < 1900:
        return "1850-1899"
    if year < 1950:
        return "1900-1949"
    return "1950+"


def normalize_medium_family(medium: object, classification: object = None) -> str | None:
    """Normalize explicit medium metadata into stable, coarse families."""
    value = " ".join((str(medium or ""), str(classification or ""))).casefold()
    if not value.strip():
        return None
    families = (
        ("watercolor", ("watercolor", "watercolour")),
        ("pastel", ("pastel",)),
        ("tempera", ("tempera",)),
        ("photograph", ("photograph", "gelatin silver", "albumen")),
        ("textile", ("textile", "tapestry", "embroidery", "woven")),
        ("print", ("print", "etching", "engraving", "lithograph", "woodcut", "screenprint")),
        ("drawing", ("drawing", "graphite", "pencil", "charcoal", "chalk", "ink on paper")),
        ("oil", ("oil",)),
    )
    for family, markers in families:
        if any(marker in value for marker in markers):
            return family
    return "other"
