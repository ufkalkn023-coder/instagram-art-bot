from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Optional
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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


REEL_RELEASE_FILE_HASH_KEYS = {
    "reel.mp4",
    "caption.txt",
    "metadata.json",
    "qc/contact-sheet.png",
}


def _require_aware_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("must include a timezone")
    return value


def _require_https_url(value: str) -> str:
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


def _require_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _require_uuid(value: str) -> str:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("must be a UUID") from exc
    return value


def _require_nonempty_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be empty")
    return normalized


class ReelPublicationStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    AMBIGUOUS = "AMBIGUOUS"
    EXPIRED = "EXPIRED"


class ReelReleaseIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["artfolio-release-v1"]
    reel_id: str
    created_at: str
    manifest_sha256: str
    files_sha256: dict[str, str]

    @field_validator("reel_id")
    @classmethod
    def require_reel_id(cls, value: str) -> str:
        return _require_nonempty_text(value)

    @field_validator("created_at")
    @classmethod
    def require_created_at(cls, value: str) -> str:
        return _require_aware_timestamp(value)

    @field_validator("manifest_sha256")
    @classmethod
    def require_manifest_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("files_sha256")
    @classmethod
    def require_release_file_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != REEL_RELEASE_FILE_HASH_KEYS:
            raise ValueError("must contain exactly the required release file hashes")
        for filename, digest in value.items():
            if not isinstance(digest, str):
                raise ValueError(f"{filename} must be a SHA-256 digest")
            _require_sha256(digest)
        return value


class ReelStagingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_key: str
    public_url: str
    staged_at: str

    @field_validator("object_key")
    @classmethod
    def require_object_key(cls, value: str) -> str:
        return _require_nonempty_text(value)

    @field_validator("public_url")
    @classmethod
    def require_public_url(cls, value: str) -> str:
        return _require_https_url(value)

    @field_validator("staged_at")
    @classmethod
    def require_staged_at(cls, value: str) -> str:
        return _require_aware_timestamp(value)


class ReelReservationRecord(BaseModel):
    """One immutable-identity Reel lifecycle reservation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_id: str
    artwork_id: str
    status: ReelPublicationStatus
    reserved_at: str
    release_identity: ReelReleaseIdentity
    staging: ReelStagingRecord | None = None
    container_id: str | None = None
    publish_started_at: str | None = None
    publish_response_media_id: str | None = None
    media_id: str | None = None
    posted_at: str | None = None
    permalink: str | None = None
    ambiguous_at: str | None = None
    ambiguity_reason: str | None = None
    expired_at: str | None = None
    expiration_reason: str | None = None
    last_reconciled_at: str | None = None
    reconciliation_attempt_count: int | None = None
    reconciliation_result: str | None = None
    reconciliation_evidence: str | None = None

    @field_validator("publication_id")
    @classmethod
    def require_publication_id(cls, value: str) -> str:
        return _require_uuid(value)

    @field_validator("artwork_id", "container_id", "publish_response_media_id", "media_id", "ambiguity_reason", "expiration_reason", "reconciliation_result", "reconciliation_evidence")
    @classmethod
    def require_optional_nonempty_text(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonempty_text(value)

    @field_validator("reserved_at", "publish_started_at", "posted_at", "ambiguous_at", "expired_at", "last_reconciled_at")
    @classmethod
    def require_optional_aware_timestamp(cls, value: str | None) -> str | None:
        return None if value is None else _require_aware_timestamp(value)

    @field_validator("permalink")
    @classmethod
    def require_optional_permalink(cls, value: str | None) -> str | None:
        return None if value is None else _require_https_url(value)

    @field_validator("reconciliation_attempt_count")
    @classmethod
    def require_reconciliation_attempt_count(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 0):
            raise ValueError("must be a non-negative integer")
        return value

    @model_validator(mode="after")
    def require_lifecycle_shape(self):
        if self.release_identity.reel_id != normalize_artwork_id(self.artwork_id):
            raise ValueError("release_identity.reel_id must equal the canonical artwork_id")

        has_publish_boundary = self.container_id is not None or self.publish_started_at is not None
        if has_publish_boundary and not (self.container_id and self.publish_started_at):
            raise ValueError("container_id and publish_started_at must appear together")

        success_fields = (self.media_id, self.posted_at, self.permalink)
        terminal_fields = (self.publish_response_media_id, *success_fields)
        ambiguity_fields = (self.ambiguous_at, self.ambiguity_reason)
        expiry_fields = (self.expired_at, self.expiration_reason)
        if (self.ambiguous_at is None) != (self.ambiguity_reason is None):
            raise ValueError("ambiguous_at and ambiguity_reason must appear together")
        if (self.expired_at is None) != (self.expiration_reason is None):
            raise ValueError("expired_at and expiration_reason must appear together")

        if self.status is ReelPublicationStatus.PENDING:
            if has_publish_boundary or any(terminal_fields) or any(ambiguity_fields) or any(expiry_fields):
                raise ValueError("PENDING reservations may not contain terminal or publish-boundary fields")
        elif self.status is ReelPublicationStatus.PUBLISHING:
            if not (self.staging and self.container_id and self.publish_started_at):
                raise ValueError("PUBLISHING reservations require staging and publish-boundary fields")
            if any(success_fields) or any(ambiguity_fields) or any(expiry_fields):
                raise ValueError("PUBLISHING reservations may not contain terminal fields")
        elif self.status is ReelPublicationStatus.PUBLISHED:
            if not (self.staging and self.container_id and self.publish_started_at and self.media_id and self.posted_at):
                raise ValueError("PUBLISHED reservations require staging, boundary, media, and posted fields")
            if any(ambiguity_fields) or any(expiry_fields):
                raise ValueError("PUBLISHED reservations may not contain ambiguous or expiry fields")
        elif self.status is ReelPublicationStatus.AMBIGUOUS:
            if not (self.staging and self.container_id and self.publish_started_at and all(ambiguity_fields)):
                raise ValueError("AMBIGUOUS reservations require staging, boundary, and ambiguity fields")
            if any(success_fields) or any(expiry_fields):
                raise ValueError("AMBIGUOUS reservations may not contain final or expiry fields")
        elif self.status is ReelPublicationStatus.EXPIRED:
            if not all(expiry_fields) or any(terminal_fields) or any(ambiguity_fields):
                raise ValueError("EXPIRED reservations require only expiry terminal fields")
        return self


class ReelPublicationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    artwork_id: str
    media_id: str
    posted_at: str
    permalink: str | None = None
    release_identity: ReelReleaseIdentity

    @field_validator("id")
    @classmethod
    def require_id(cls, value: str) -> str:
        return _require_uuid(value)

    @field_validator("artwork_id", "media_id")
    @classmethod
    def require_text(cls, value: str) -> str:
        return _require_nonempty_text(value)

    @field_validator("posted_at")
    @classmethod
    def require_posted_at(cls, value: str) -> str:
        return _require_aware_timestamp(value)

    @field_validator("permalink")
    @classmethod
    def require_permalink(cls, value: str | None) -> str | None:
        return None if value is None else _require_https_url(value)

    @model_validator(mode="after")
    def require_identity_artwork_match(self):
        if self.release_identity.reel_id != normalize_artwork_id(self.artwork_id):
            raise ValueError("release_identity.reel_id must equal the canonical artwork_id")
        return self


class ReelCleanupQueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_id: str
    eligible_at: str
    reason: str

    @field_validator("publication_id")
    @classmethod
    def require_entry_publication_id(cls, value: str) -> str:
        return _require_uuid(value)

    @field_validator("eligible_at")
    @classmethod
    def require_eligible_at(cls, value: str) -> str:
        return _require_aware_timestamp(value)

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        return _require_nonempty_text(value)


@dataclass(frozen=True)
class ValidatedReelHistory:
    reservations: tuple[ReelReservationRecord, ...]
    publications: tuple[ReelPublicationRecord, ...]
    publication_count: int
    cleanup_queue: tuple[ReelCleanupQueueEntry, ...]


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
