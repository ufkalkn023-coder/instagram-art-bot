"""Deterministic regional normalization for artwork metadata.

This intentionally uses only structured museum metadata.  Artwork titles are
not accepted as an inference source because they are too ambiguous for a
selection policy.
"""

from collections.abc import Iterable
import re
from typing import Any


REGION_UNKNOWN = "unknown"
REGION_VOCABULARY = frozenset({
    "europe",
    "east_asia",
    "south_southeast_asia",
    "middle_east_north_africa",
    "sub_saharan_africa",
    "north_america",
    "latin_america_caribbean",
    "oceania",
    "other",
    REGION_UNKNOWN,
})

# These are intentionally conservative, strong cultural/geographic signals.
# The same controlled mapping is used for culture, place, nationality, and
# reliable department/style metadata; inference order is handled separately.
_REGION_SIGNALS = (
    ("east_asia", (
        "east asian", "japan", "japanese", "china", "chinese", "korea", "korean", "edo period",
    )),
    ("south_southeast_asia", (
        "south asian", "south asia", "southeast asia", "south east asia", "india", "indian", "pakistan",
        "pakistani", "bangladesh", "bangladeshi", "nepal", "nepalese", "sri lanka", "sri lankan",
        "thailand", "thai", "vietnam", "vietnamese", "cambodia", "cambodian", "indonesia", "indonesian",
        "philippines", "filipino", "malaysia", "malaysian", "myanmar", "burma", "laos", "singapore",
    )),
    ("middle_east_north_africa", (
        "middle east", "north africa", "egypt", "egyptian", "morocco", "moroccan", "algeria", "algerian",
        "tunisia", "tunisian", "libya", "libyan", "sudan", "sudanese", "turkey", "turkish", "ottoman",
        "iran", "iranian", "persia", "persian", "iraq", "iraqi", "syria", "syrian", "lebanon", "lebanese",
        "palestine", "palestinian", "israel", "israeli", "jordan", "jordanian", "saudi", "arabian", "yemen",
        "emirati", "qatar", "kuwait", "oman", "islamic art",
    )),
    ("sub_saharan_africa", (
        "sub-saharan", "sub saharan", "west africa", "east africa", "central africa", "southern africa",
        "nigeria", "nigerian", "ghana", "ghanaian", "senegal", "senegalese", "mali", "malian", "ethiopia",
        "ethiopian", "kenya", "kenyan", "tanzania", "tanzanian", "congo", "congolese", "angola", "angolan",
        "south africa", "south african", "zimbabwe", "zambian", "uganda", "ugandan",
    )),
    ("europe", (
        "europe", "french", "france", "italian", "italy", "dutch", "netherlands", "german", "germany",
        "british", "england", "english", "scottish", "irish", "spain", "spanish", "portugal", "portuguese",
        "belgium", "belgian", "swiss", "switzerland", "austrian", "austria", "poland", "polish", "russia",
        "russian", "ukraine", "ukrainian", "greece", "greek", "norway", "norwegian", "sweden", "swedish",
        "denmark", "danish", "finland", "finnish", "iceland", "hungary", "hungarian", "czech", "romania",
        "romanian", "balkan", "croatia", "croatian", "serbia", "serbian",
    )),
    ("north_america", (
        "north america", "united states", "american", "canada", "canadian",
    )),
    ("latin_america_caribbean", (
        "latin america", "caribbean", "mexico", "mexican", "brazil", "brazilian", "argentina", "argentine",
        "chile", "chilean", "peru", "peruvian", "colombia", "colombian", "cuba", "cuban", "haiti", "haitian",
        "jamaica", "jamaican", "puerto rico", "venezuelan", "uruguay", "uruguayan", "bolivia", "bolivian",
    )),
    ("oceania", (
        "oceania", "australia", "australian", "new zealand", "maori", "melanesia", "micronesia", "polynesia",
        "papua new guinea", "fiji", "fijian", "samoa", "samoan", "tonga", "tongan",
    )),
)


def metadata_text(value: Any) -> str | None:
    """Safely flatten API metadata into a displayable text value."""
    if isinstance(value, str):
        normalized = " ".join(value.split())
        return normalized or None
    if isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        values = [text for item in value if (text := metadata_text(item))]
        return "; ".join(values) or None
    return None


def _region_from_metadata(value: Any) -> str | None:
    text = metadata_text(value)
    if not text:
        return None
    normalized = text.casefold()
    for region, signals in _REGION_SIGNALS:
        if any(
            re.search(rf"(?<!\w){re.escape(signal)}(?!\w)", normalized)
            for signal in signals
        ):
            return region
    return None


def normalize_region(value: object) -> str:
    """Return a valid region value, treating absent or invalid values as unknown."""
    if not isinstance(value, str):
        return REGION_UNKNOWN
    normalized = value.strip().casefold()
    return normalized if normalized in REGION_VOCABULARY else REGION_UNKNOWN


def infer_region(
    *,
    culture: Any = None,
    geography: Any = None,
    artist_nationality: Any = None,
    department: Any = None,
    style_or_period: Any = None,
) -> str:
    """Infer one controlled region using the documented metadata hierarchy."""
    for metadata in (culture, geography, artist_nationality, style_or_period, department):
        if region := _region_from_metadata(metadata):
            return region
    return REGION_UNKNOWN
