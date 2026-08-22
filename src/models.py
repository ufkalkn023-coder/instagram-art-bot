from typing import Optional
from pydantic import BaseModel, Field


LEGACY_ARTWORK_ID_PREFIXES = {
    "artic_": "aic_",
    "cma_": "cleveland_",
}
CONFIRMED_RIGHTS_STATUSES = {
    "CONFIRMED_PUBLIC_DOMAIN",
    "CONFIRMED_OPEN_ACCESS",
}
MAX_IMAGE_DIMENSION = 100_000


def normalize_artwork_id(artwork_id: str) -> str:
    """Return the canonical ID while preserving unknown ID formats."""
    for legacy_prefix, canonical_prefix in LEGACY_ARTWORK_ID_PREFIXES.items():
        if artwork_id.startswith(legacy_prefix):
            return canonical_prefix + artwork_id[len(legacy_prefix):]
    return artwork_id


def normalize_image_dimensions(width: object, height: object) -> tuple[int | None, int | None]:
    """Return a trustworthy positive pixel-dimension pair, or no dimensions."""
    def parse_dimension(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.isdigit():
            parsed = int(value)
        else:
            return None
        return parsed if 0 < parsed <= MAX_IMAGE_DIMENSION else None

    normalized_width = parse_dimension(width)
    normalized_height = parse_dimension(height)
    if normalized_width is None or normalized_height is None:
        return None, None
    return normalized_width, normalized_height


class NormalizedArtwork(BaseModel):
    """
    Centralized internal model representing a normalized artwork from any museum.
    This model guarantees that downstream processes (Quality Filter, Gemini, Video Generation)
    do not need to know about the source API schema.
    """
    
    # Canonical Identity
    source: str = Field(..., description="The source museum, e.g., 'met', 'aic', 'cleveland', 'rijksmuseum'")
    source_id: str = Field(..., description="The unique ID from the source API")
    
    # Core Metadata
    title: str = Field(default="Untitled", description="Title of the artwork")
    artist_name: str = Field(default="Unknown Artist", description="Primary name of the artist")
    artist_display_name: Optional[str] = None
    artist_birth_year: Optional[str] = None
    artist_death_year: Optional[str] = None
    
    creation_date: Optional[str] = Field(default="Unknown Date", description="Date or period of creation")
    creation_date_display: Optional[str] = None
    
    medium: Optional[str] = None
    dimensions: Optional[str] = None
    
    # Classification & Context
    culture: Optional[str] = None
    department: Optional[str] = None
    classification: Optional[str] = None
    style_or_period: Optional[str] = None
    description: Optional[str] = None
    
    # Provenance / Source Info
    museum_name: str = Field(..., description="Full display name of the museum")
    museum_url: Optional[str] = None
    artwork_url: Optional[str] = None
    credit_line: Optional[str] = None
    license: Optional[str] = None
    is_public_domain: bool = Field(default=False)
    rights_status: Optional[str] = None
    rights_text: Optional[str] = None
    
    # Media
    image_url: Optional[str] = Field(None, description="Direct URL to the high-res image")
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    
    # Internal Pipeline Metadata
    quality_score: Optional[float] = Field(
        default=None,
        description="Deterministic 0-100 metadata/source/image quality score",
    )
    measurement_coverage: Optional[float] = Field(
        default=None,
        description="Fraction of deterministic quality signals backed by measurements (0-1)",
    )
    selection_score: Optional[float] = Field(
        default=None,
        description="Post-gate ranking score including diversity/discovery/serendipity adjustments",
    )
    
    @property
    def canonical_id(self) -> str:
        """Globally unique identifier for duplicate detection."""
        return normalize_artwork_id(f"{self.source}_{self.source_id}")

    @property
    def has_confirmed_rights(self) -> bool:
        """Whether the adapter supplied explicit, publishable rights metadata."""
        return self.is_public_domain and self.rights_status in CONFIRMED_RIGHTS_STATUSES
