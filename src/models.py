from typing import Optional
from pydantic import BaseModel, Field

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
    
    # Media
    image_url: Optional[str] = Field(None, description="Direct URL to the high-res image")
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    
    # Internal Pipeline Metadata
    quality_score: Optional[int] = Field(default=None, description="0-100 visual/metadata quality score")
    
    @property
    def canonical_id(self) -> str:
        """Globally unique identifier for duplicate detection."""
        return f"{self.source}_{self.source_id}"
