"""Reusable local Feed content, independent of publication authorization/state."""

from dataclasses import dataclass, field
from typing import Any, Literal
import hashlib
import json

from src.story_delivery import StoryDelivery


@dataclass(frozen=True)
class PreparedFeedContent:
    publication_format: Literal["single", "carousel"]
    artworks: tuple[dict[str, Any], ...]
    media_paths: tuple[str, ...]
    caption: str
    alt_text: str | None = None
    publication_metadata: dict[str, Any] = field(default_factory=dict)
    theme_id: str | None = None
    theme_family: str | None = None
    carousel_format: str | None = None

    def __post_init__(self) -> None:
        count = len(self.artworks)
        if (self.publication_format == "single" and count != 1
                or self.publication_format == "carousel" and not 6 <= count <= 9
                or self.publication_format not in {"single", "carousel"}
                or len(self.media_paths) != count):
            raise ValueError("Prepared content must match the Feed format cardinality")
        if not isinstance(self.caption, str) or not self.caption.strip() or len(self.caption) > 2200:
            raise ValueError("Prepared Feed caption is empty or too long")

    @property
    def publication_ids(self) -> tuple[str, ...]:
        return tuple(art["id"] for art in self.artworks)

    @property
    def featured_ids(self) -> tuple[str, ...]:
        return self.publication_ids[1:]


def artwork_digest(artwork: dict) -> str:
    return hashlib.sha256(json.dumps(artwork, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PreparedStoryContent(PreparedFeedContent):
    """Reviewed carousel pages with unique sources; legacy cardinality is unchanged."""

    story_delivery: StoryDelivery | None = None

    def __post_init__(self) -> None:
        if self.publication_format != "carousel" or not isinstance(self.story_delivery, StoryDelivery):
            raise ValueError("Story content requires a reviewed carousel delivery")
        # Revalidate even model_copy/constructed values at this trust boundary.
        delivery = StoryDelivery.model_validate(self.story_delivery.model_dump(mode="json"))
        delivery.require_sources(self.publication_ids)
        if len(self.media_paths) != len(delivery.pages):
            raise ValueError("Story media count does not match reviewed pages")
        if (not isinstance(self.caption, str) or not self.caption.strip() or len(self.caption) > 2200
                or hashlib.sha256(self.caption.encode()).hexdigest() != delivery.caption_sha256):
            raise ValueError("Story caption differs from reviewed delivery")
        if any(artwork_digest(art) != delivery.artwork_sha256[art["id"]] for art in self.artworks):
            raise ValueError("Story source metadata differs from reviewed delivery")
        if self.publication_metadata.get("story_delivery") != delivery.model_dump(mode="json"):
            raise ValueError("Story publication metadata differs from reviewed delivery")
