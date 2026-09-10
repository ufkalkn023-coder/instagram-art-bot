from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from src.engagement_features import EngagementFeatureVector, SpacingBucket
from src.region import REGION_UNKNOWN, normalize_region


LEGACY_ARTWORK_ID_PREFIXES = {
    "artic_": "aic_",
    "cma_": "cleveland_",
}
CONFIRMED_RIGHTS_STATUSES = {
    "CONFIRMED_PUBLIC_DOMAIN",
    "CONFIRMED_OPEN_ACCESS",
}
MAX_IMAGE_DIMENSION = 100_000


class CarouselExperimentMetadata(BaseModel):
    """Compact, publication-level explanation of one carousel strategy."""

    selection_model_version: str = Field(..., min_length=1)
    engagement_model_version: str = Field(..., min_length=1)
    carousel_theme: str = Field(..., min_length=1)
    carousel_format: str = Field(..., min_length=1)
    featured_count: int = Field(..., ge=5, le=8)
    cover_variant: str = Field(..., min_length=1)
    caption_hook_type: str = Field(..., min_length=1)
    publish_slot: Literal["slot_1", "slot_2", "slot_3", "slot_4"]
    exploration_selected: bool
    learned_score: float = Field(..., ge=0, le=100)
    engagement_confidence: float = Field(..., ge=0, le=1)
    quality_component: float = Field(..., ge=0, le=100)
    engagement_component: float = Field(..., ge=0, le=100)
    diversity_component: float = Field(..., ge=-10, le=10)
    exploration_component: float = Field(..., ge=0, le=100)
    preceding_post_distance_minutes: Optional[float] = Field(default=None, ge=0)
    previous_post_spacing_bucket: Optional[SpacingBucket] = None
    engagement_features: Optional[EngagementFeatureVector] = None


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


class PublicationRecord(BaseModel):
    """One proven Instagram feed publication, separate from artwork locks."""

    id: str = Field(..., min_length=1)
    type: Literal["single", "carousel"]
    media_id: str = Field(..., min_length=1)
    artwork_ids: list[str] = Field(..., min_length=1)
    posted_at: str = Field(..., min_length=1)
    theme: Optional[str] = None
    content_type: Optional[str] = None
    permalink: Optional[str] = None
    selection_model_version: Optional[str] = None
    engagement_model_version: Optional[str] = None
    carousel_theme: Optional[str] = None
    carousel_format: Optional[str] = None
    featured_count: Optional[int] = Field(default=None, ge=1, le=8)
    cover_variant: Optional[str] = None
    caption_hook_type: Optional[str] = None
    publish_slot: Optional[str] = None
    exploration_selected: Optional[bool] = None
    learned_score: Optional[float] = Field(default=None, ge=0, le=100)
    engagement_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    quality_component: Optional[float] = Field(default=None, ge=0, le=100)
    engagement_component: Optional[float] = Field(default=None, ge=0, le=100)
    diversity_component: Optional[float] = Field(default=None, ge=-10, le=10)
    exploration_component: Optional[float] = Field(default=None, ge=0, le=100)
    preceding_post_distance_minutes: Optional[float] = Field(default=None, ge=0)
    previous_post_spacing_bucket: Optional[SpacingBucket] = None
    engagement_features: Optional[EngagementFeatureVector] = None

    @field_validator("id", "media_id", "posted_at")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("permalink")
    @classmethod
    def require_https_permalink(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value != value.strip():
            raise ValueError("must be a trimmed HTTPS URL")
        try:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("must be an HTTPS URL")
            parsed.port
        except ValueError as exc:
            raise ValueError("must be an HTTPS URL") from exc
        return value

    @field_validator("posted_at")
    @classmethod
    def require_aware_timestamp(cls, value: str) -> str:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value

    @field_validator("artwork_ids")
    @classmethod
    def require_unique_canonical_artwork_ids(cls, artwork_ids: list[str]) -> list[str]:
        normalized_ids = []
        for artwork_id in artwork_ids:
            if not isinstance(artwork_id, str) or not artwork_id.strip():
                raise ValueError("artwork IDs must be nonempty strings")
            normalized_ids.append(normalize_artwork_id(artwork_id.strip()))
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("artwork IDs must be unique")
        return normalized_ids

    @model_validator(mode="after")
    def require_type_appropriate_artwork_count(self):
        if self.type == "single" and len(self.artwork_ids) != 1:
            raise ValueError("single publications require exactly one artwork")
        if self.type == "carousel" and len(self.artwork_ids) < 2:
            raise ValueError("carousel publications require at least two artworks")
        return self


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
    # culture is preserved as source metadata; region is the controlled value
    # used by selection diversity policy.
    culture: Optional[str] = None
    geographic_origin: Optional[str] = None
    artist_nationality: Optional[str] = None
    region: str = Field(default=REGION_UNKNOWN, description="Normalized editorial region")
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
    copyright_notice: Optional[str] = None
    
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

    @field_validator("region", mode="before")
    @classmethod
    def normalize_region_value(cls, value: object) -> str:
        return normalize_region(value)
    
    @property
    def canonical_id(self) -> str:
        """Globally unique identifier for duplicate detection."""
        return normalize_artwork_id(f"{self.source}_{self.source_id}")

    @property
    def has_confirmed_rights(self) -> bool:
        """Whether the adapter supplied explicit, publishable rights metadata."""
        return self.is_public_domain and self.rights_status in CONFIRMED_RIGHTS_STATUSES
