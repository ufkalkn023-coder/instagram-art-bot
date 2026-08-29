"""Typed product policy for adaptive editorial carousel cardinality."""

from dataclasses import dataclass
from typing import Final


MIN_FEATURED_WORKS: Final[int] = 5
MAX_FEATURED_WORKS: Final[int] = 8
MIN_TOTAL_SLIDES: Final[int] = 6
MAX_TOTAL_SLIDES: Final[int] = 9


@dataclass(frozen=True)
class CarouselSizePolicy:
    """Bounds and explainable marginal-inclusion calibration."""

    min_featured: int = MIN_FEATURED_WORKS
    max_featured: int = MAX_FEATURED_WORKS
    min_total_slides: int = MIN_TOTAL_SLIDES
    max_total_slides: int = MAX_TOTAL_SLIDES
    marginal_inclusion_threshold: float = 70.0
    set_effect_weight: float = 2.0
    max_set_effect: float = 5.0
    relaxed_constraint_penalty: float = 4.0

    def __post_init__(self) -> None:
        if self.min_total_slides != self.min_featured + 1:
            raise ValueError("Minimum total slides must include one distinct cover")
        if self.max_total_slides != self.max_featured + 1:
            raise ValueError("Maximum total slides must include one distinct cover")


ADAPTIVE_CAROUSEL_SIZE_POLICY: Final[CarouselSizePolicy] = CarouselSizePolicy()
