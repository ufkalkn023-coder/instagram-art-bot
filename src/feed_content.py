"""Reusable local Feed content, independent of publication authorization/state."""

from dataclasses import dataclass, field
from typing import Any, Literal


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
