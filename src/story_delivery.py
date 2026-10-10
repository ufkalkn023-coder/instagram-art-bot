"""Explicit source membership and ordered presentation for a reviewed Feed story."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: Literal["cover", "artwork", "detail", "comparison", "context", "closing"]
    artwork_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StoryDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["artfolio-story-delivery-v1"] = "artfolio-story-delivery-v1"
    reviewed_revision: int = Field(ge=1, strict=True)
    reviewed_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    source_sha256: dict[str, str]
    artwork_sha256: dict[str, str]
    caption_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    public_title: str = Field(min_length=1, max_length=180)
    theme_id: str = Field(min_length=1, max_length=100)
    theme_title: str = Field(min_length=1, max_length=180)
    narrative: Literal["single_study", "comparison", "thematic_selection"]
    cover_variant: Literal["museum_journal", "artwork_first", "detail_study"]
    headline_kind: Literal[
        "source_title", "factual_collection", "ai_proposal", "user_edit"
    ]
    pages: tuple[StoryPage, ...] = Field(min_length=3, max_length=10)

    @field_validator("source_sha256", "artwork_sha256")
    @classmethod
    def valid_hashes(cls, value):
        import re

        if any(
            not isinstance(h, str) or not re.fullmatch(r"[a-f0-9]{64}", h)
            for h in value.values()
        ):
            raise ValueError("Invalid story source digest")
        return value

    @model_validator(mode="after")
    def coherent_delivery(self):
        from src.models import require_canonical_artwork_id

        ids = self.source_ids
        for identity in ids:
            require_canonical_artwork_id(identity)
        known = set(ids)
        count = len(ids)
        if (
            count != len(known)
            or set(self.source_sha256) != known
            or set(self.artwork_sha256) != known
            or self.narrative == "single_study"
            and count != 1
            or self.narrative == "comparison"
            and count != 2
            or self.narrative == "thematic_selection"
            and not 3 <= count <= 8
        ):
            raise ValueError("Story source registry does not match narrative")
        if (
            self.pages[0].role != "cover"
            or self.pages[-1].role != "closing"
            or len({p.id for p in self.pages}) != len(self.pages)
            or sum(p.role == "cover" for p in self.pages) != 1
            or sum(p.role == "closing" for p in self.pages) != 1
        ):
            raise ValueError("Story page order/identity is invalid")
        for page in self.pages:
            expected = 2 if page.role == "comparison" else 1
            if (
                len(page.artwork_ids) != expected
                or len(set(page.artwork_ids)) != expected
                or set(page.artwork_ids) - known
            ):
                raise ValueError("Story page refers to invalid source membership")
        if {p.artwork_ids[0] for p in self.pages if p.role == "artwork"} != known:
            raise ValueError("Story requires a full page for every source")
        if self.pages[0].artwork_ids != ids[:1]:
            raise ValueError("Story cover must match first reserved source")
        if self.narrative == "comparison" and not any(
            p.role == "comparison" for p in self.pages
        ):
            raise ValueError("Comparison requires a comparison page")
        return self

    def require_sources(self, ids) -> None:
        if tuple(ids) != self.source_ids:
            raise ValueError("Story delivery source membership/order mismatch")
