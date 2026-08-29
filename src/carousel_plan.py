"""Explicit data model for one editorial carousel publication."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from src.carousel_themes import CarouselThemeDefinition, primary_search_query
from src.artwork_visual_features import ArtworkVisualFeatures
from src.carousel_sequence import CarouselSequenceResult
from src.carousel_set_optimizer import CarouselSetOptimizationResult
from src.carousel_policy import MAX_FEATURED_WORKS, MIN_FEATURED_WORKS
from src.carousel_editorial import CarouselEditorialFacts


class CoverMode(str, Enum):
    """Supported editorial presentations of a rights-policy-eligible cover."""

    FULL_ARTWORK = "FULL_ARTWORK"
    DETAIL_CROP = "DETAIL_CROP"


@dataclass(frozen=True)
class CoverScoreBreakdown:
    """Explainable inputs to the deterministic editorial-cover score."""

    theme_relevance: float
    technical_quality: float
    resolution: float
    composition_suitability: float
    crop_flexibility: float
    visual_readability: float
    aspect_ratio: float

    @property
    def total(self) -> float:
        return round(
            self.theme_relevance
            + self.technical_quality
            + self.resolution
            + self.composition_suitability
            + self.crop_flexibility
            + self.visual_readability
            + self.aspect_ratio,
            2,
        )


@dataclass(frozen=True)
class CoverAsset:
    """The selected source artwork and its local editorial presentation."""

    artwork: Mapping[str, Any]
    local_image_path: str
    mode: CoverMode
    cover_score: float
    score_breakdown: CoverScoreBreakdown
    visual_features: ArtworkVisualFeatures | None = None

    @property
    def canonical_id(self) -> str:
        return str(self.artwork["id"])


@dataclass(frozen=True)
class CarouselPlan:
    """Everything needed to render, publish, and record one carousel."""

    theme: CarouselThemeDefinition
    editorial_title: str
    editorial_subtitle: str
    cover_micro_facts: tuple[str, ...]
    cover: CoverAsset
    featured_artworks: tuple[Mapping[str, Any], ...]
    caption: str
    cover_variant: str = "editorial"
    caption_hook_type: str = "curiosity"
    editorial_facts: CarouselEditorialFacts | None = None
    caption_intro: str = ""
    set_optimization: CarouselSetOptimizationResult | None = None
    sequence: CarouselSequenceResult | None = None

    def __post_init__(self) -> None:
        if not MIN_FEATURED_WORKS <= len(self.featured_artworks) <= MAX_FEATURED_WORKS:
            raise ValueError(
                "An Artfolio editorial carousel requires between "
                f"{MIN_FEATURED_WORKS} and {MAX_FEATURED_WORKS} featured artworks"
            )

        featured_ids = self.featured_ids
        if len(set(featured_ids)) != len(featured_ids):
            raise ValueError("Featured artwork canonical IDs must be unique")
        if self.cover.canonical_id in featured_ids:
            raise ValueError("Editorial cover artwork must differ from every featured artwork")

    @property
    def featured_ids(self) -> tuple[str, ...]:
        return tuple(str(artwork["id"]) for artwork in self.featured_artworks)

    @property
    def theme_id(self) -> str:
        return self.theme.id

    @property
    def search_query(self) -> str:
        return primary_search_query(self.theme)

    @property
    def publication_ids(self) -> tuple[str, ...]:
        """Canonical IDs in exact Instagram media order."""
        return (self.cover.canonical_id, *self.featured_ids)

    @classmethod
    def build(
        cls,
        *,
        theme: CarouselThemeDefinition,
        editorial_title: str,
        editorial_subtitle: str,
        cover_micro_facts: Sequence[str],
        editorial_facts: CarouselEditorialFacts | None = None,
        caption_intro: str = "",
        cover: CoverAsset,
        featured_artworks: Sequence[Mapping[str, Any]],
        caption: str,
        cover_variant: str = "editorial",
        caption_hook_type: str = "curiosity",
        set_optimization: CarouselSetOptimizationResult | None = None,
        sequence: CarouselSequenceResult | None = None,
    ) -> "CarouselPlan":
        return cls(
            theme=theme,
            editorial_title=editorial_title,
            editorial_subtitle=editorial_subtitle,
            cover_micro_facts=tuple(cover_micro_facts),
            editorial_facts=editorial_facts,
            caption_intro=caption_intro,
            cover=cover,
            featured_artworks=tuple(featured_artworks),
            caption=caption,
            cover_variant=cover_variant,
            caption_hook_type=caption_hook_type,
            set_optimization=set_optimization,
            sequence=sequence,
        )
