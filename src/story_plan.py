"""Versioned story plans: source artwork identities are independent of slide identities."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Literal

from PIL import Image, ImageFilter, ImageOps, ImageStat
from pydantic import Field, field_validator, model_validator

from src.editorial_design import CoverStyle, _validate_focus
from src.editorial_v2 import EditorialPlan, HeadlineEvidence, SourceArtwork, StrictModel
from src.quality_filter import validate_local_image_file

Narrative = Literal["single_study", "comparison", "thematic_selection"]
SlideRole = Literal["cover", "artwork", "detail", "comparison", "context", "closing"]
FocusBasis = Literal["preview_focus", "local_geometry", "model_focus"]


class StorySlide(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: SlideRole
    artwork_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    title: str = Field(default="", max_length=180)
    body: str = Field(default="", max_length=600)
    focus: tuple[float, float, float, float] | None = None
    focus_basis: FocusBasis | None = None
    evidence: tuple[HeadlineEvidence, ...] = ()

    @field_validator("focus")
    @classmethod
    def bounded_focus(cls, value):
        return None if value is None else _validate_focus(value)

    @model_validator(mode="after")
    def role_contract(self):
        expected = 2 if self.role == "comparison" else 1
        if len(self.artwork_ids) != expected or len(set(self.artwork_ids)) != expected:
            raise ValueError("Slide source count must match its role")
        if self.role == "cover" and (self.title or self.body):
            raise ValueError("Cover copy is owned by the story public title")
        if self.role == "detail":
            if self.focus is None or self.focus_basis is None:
                raise ValueError(
                    "Detail slides require real focus coordinates and provenance"
                )
        elif self.focus is not None or self.focus_basis is not None:
            raise ValueError("Only detail slides may have focus coordinates")
        return self


class StoryPlan(StrictModel):
    schema_version: Literal["artfolio-story-v1"] = "artfolio-story-v1"
    theme_id: str = Field(min_length=1, max_length=100)
    theme_title: str = Field(min_length=1, max_length=180)
    public_title: str = Field(min_length=1, max_length=180)
    editorial_angle: str = Field(default="", max_length=500)
    headline_evidence: tuple[HeadlineEvidence, ...] = ()
    headline_kind: Literal[
        "source_title", "factual_collection", "ai_proposal", "user_edit"
    ]
    narrative: Narrative
    artwork_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    cover_style: CoverStyle
    slides: tuple[StorySlide, ...] = Field(min_length=3, max_length=12)
    fallback_reasons: tuple[str, ...] = ()
    manual_review_required: Literal[True] = True

    @field_validator("public_title", "theme_title")
    @classmethod
    def meaningful_title(cls, value):
        if not value.strip():
            raise ValueError("Title must contain visible text")
        return value

    @model_validator(mode="after")
    def story_contract(self):
        count = len(self.artwork_ids)
        if len(set(self.artwork_ids)) != count:
            raise ValueError("Source artworks must have unique identities")
        if (
            self.narrative == "single_study"
            and count != 1
            or self.narrative == "comparison"
            and count != 2
            or self.narrative == "thematic_selection"
            and not 3 <= count <= 8
        ):
            raise ValueError("Source cardinality must match narrative")
        if self.slides[0].role != "cover" or self.slides[-1].role != "closing":
            raise ValueError("Stories require a first cover and final closing")
        if (
            sum(s.role == "cover" for s in self.slides) != 1
            or sum(s.role == "closing" for s in self.slides) != 1
        ):
            raise ValueError("Stories require exactly one cover and one closing")
        if len({s.id for s in self.slides}) != len(self.slides):
            raise ValueError("Slide identities must be unique")
        known = set(self.artwork_ids)
        for slide in self.slides:
            if set(slide.artwork_ids) - known:
                raise ValueError("Slide refers to unknown artwork")
            if any(e.artwork_id not in known for e in slide.evidence):
                raise ValueError("Evidence refers to unknown artwork")
        if {s.artwork_ids[0] for s in self.slides if s.role == "artwork"} != known:
            raise ValueError("Each source must have a full artwork page")
        if self.narrative == "comparison" and not any(
            s.role == "comparison" for s in self.slides
        ):
            raise ValueError("Comparison narratives require a comparison page")
        if any(e.artwork_id not in known for e in self.headline_evidence):
            raise ValueError("Headline evidence refers to unknown artwork")
        return self


def rectangle_overlap(a, b) -> float:
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return intersection / (area_a + area_b - intersection)


def usable_detail(width: int, height: int, focus) -> bool:
    left, top, right, bottom = _validate_focus(focus)
    crop_width, crop_height = (right - left) * width, (bottom - top) * height
    scale = min(920 / crop_width, 790 / crop_height)
    return min(crop_width, crop_height) >= 200 and scale <= 3


def propose_detail_regions(
    image_path: str, *, maximum: int = 3
) -> tuple[tuple[float, float, float, float], ...]:
    """Bounded edge-rich geometry proposals, not object recognition or art-history evidence."""
    if not 1 <= maximum <= 3:
        raise ValueError("maximum must be 1–3")
    path = Path(image_path)
    if path.stat().st_size > 30_000_000:
        raise ValueError("Image exceeds byte limit")
    if not validate_local_image_file(str(path)).valid:
        raise ValueError("Invalid detail source image")
    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        width, height = image.size
        image.thumbnail((360, 360))
        gray = ImageOps.grayscale(image)
        if ImageStat.Stat(gray).stddev[0] < 6:
            return ()
        # Adapt a 22% source-area window to the page frame; extreme aspect ratios
        # remain bounded inside the source instead of cutting outside its edges.
        w = min(0.88, math.sqrt(0.22 * (920 / 790) / (width / height)))
        h = min(0.88, 0.22 / w)
        w = min(0.88, 0.22 / h)
        candidates = []
        edges = gray.filter(ImageFilter.FIND_EDGES)
        for center_y in (0.25, 0.5, 0.75):
            for center_x in (0.25, 0.5, 0.75):
                left, top = (
                    min(1 - w, max(0, center_x - w / 2)),
                    min(1 - h, max(0, center_y - h / 2)),
                )
                focus = (left, top, left + w, top + h)
                if not usable_detail(width, height, focus):
                    continue
                box = tuple(
                    round(v * size)
                    for v, size in zip(
                        focus, (gray.width, gray.height, gray.width, gray.height)
                    )
                )
                score = ImageStat.Stat(edges.crop(box)).mean[0]
                if score >= 5 and ImageStat.Stat(gray.crop(box)).stddev[0] >= 6:
                    candidates.append((score, focus))
        selected = []
        for _, focus in sorted(candidates, key=lambda c: c[0], reverse=True):
            if all(rectangle_overlap(focus, previous) < 0.55 for previous in selected):
                selected.append(focus)
            if len(selected) == maximum:
                break
        return tuple(selected)


def choose_cover_style(history: list[str] | None = None) -> CoverStyle:
    recent = [
        s for s in (history or [])[-12:] if s in {style.value for style in CoverStyle}
    ]
    counts = Counter(recent)
    choices = [s for s in CoverStyle if not recent or s.value != recent[-1]]
    return min(choices, key=lambda s: counts[s.value])


def build_story_plan(
    sources: list[SourceArtwork],
    narrative: Narrative,
    *,
    detail_focus: dict[str, list[tuple[float, float, float, float]]] | None = None,
    style_history: list[str] | None = None,
    editorial: EditorialPlan | None = None,
) -> StoryPlan:
    sources = [SourceArtwork.model_validate(s.model_dump()) for s in sources]
    count = len(sources)
    if (
        narrative == "single_study"
        and count != 1
        or narrative == "comparison"
        and count != 2
        or narrative == "thematic_selection"
        and not 3 <= count <= 8
        or narrative not in {"single_study", "comparison", "thematic_selection"}
    ):
        raise ValueError("Source cardinality must match narrative")
    ids = tuple(s.artwork.canonical_id for s in sources)
    supplied = detail_focus or {}
    if set(supplied) - set(ids):
        raise ValueError("Focus refers to unknown artwork")
    slides = [StorySlide(id="cover", role="cover", artwork_ids=(ids[0],))]
    fallback = []
    for index, source in enumerate(sources):
        identity = source.artwork.canonical_id
        slides.append(
            StorySlide(
                id=f"artwork-{index + 1}",
                role="artwork",
                artwork_ids=(identity,),
                title=source.artwork.title,
            )
        )
        if narrative == "thematic_selection":
            continue
        focus = supplied.get(identity)
        basis: FocusBasis = "preview_focus" if focus is not None else "local_geometry"
        focus = (
            focus
            if focus is not None
            else propose_detail_regions(
                source.image_path, maximum=2 if narrative == "single_study" else 1
            )
        )
        for n, box in enumerate(focus[:3], 1):
            slides.append(
                StorySlide(
                    id=f"detail-{index + 1}-{n}",
                    role="detail",
                    artwork_ids=(identity,),
                    title="A closer look",
                    focus=box,
                    focus_basis=basis,
                )
            )
        if not focus:
            fallback.append("no_usable_detail")
        description = (source.artwork.description or "").strip()
        if description and narrative == "single_study":
            quote = (
                description
                if len(description) <= 350
                else description[:350].rsplit(" ", 1)[0]
            )
            if len(quote) >= 8:
                evidence = HeadlineEvidence(
                    artwork_id=identity,
                    kind="museum",
                    statement="Museum source excerpt",
                    source_quote=quote,
                )
                slides.append(
                    StorySlide(
                        id="context",
                        role="context",
                        artwork_ids=(identity,),
                        title="From the museum",
                        body=quote,
                        evidence=(evidence,),
                    )
                )
    if narrative == "comparison":
        slides.append(
            StorySlide(
                id="comparison",
                role="comparison",
                artwork_ids=ids,
                title="Look side by side",
            )
        )
    slides.append(
        StorySlide(
            id="closing",
            role="closing",
            artwork_ids=(ids[0],),
            title="Return to the whole",
            body="Look again at the complete artwork.",
        )
    )
    title = (
        sources[0].artwork.title
        if narrative == "single_study"
        else (
            "Two works, side by side"
            if narrative == "comparison"
            else f"{len(sources)} works from {sources[0].artwork.museum_name}"
        )
    )
    if (
        narrative == "thematic_selection"
        and len({s.artwork.museum_name for s in sources}) > 1
    ):
        title = f"{len(sources)} works across museum collections"
    return StoryPlan(
        theme_id=editorial.theme_id if editorial else narrative,
        theme_title=editorial.theme_title
        if editorial
        else narrative.replace("_", " ").title(),
        narrative=narrative,
        public_title=editorial.public_title if editorial else title,
        editorial_angle=editorial.editorial_angle if editorial else "",
        headline_evidence=tuple(editorial.headline_evidence) if editorial else (),
        headline_kind="ai_proposal"
        if editorial and editorial.status == "ai_selected"
        else "source_title"
        if narrative == "single_study"
        else "factual_collection",
        artwork_ids=ids,
        slides=tuple(slides),
        cover_style=editorial.cover_style
        if editorial
        else choose_cover_style(style_history),
        fallback_reasons=tuple(dict.fromkeys(fallback)),
    )
