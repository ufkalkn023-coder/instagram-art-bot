"""Hard local rendering gates and explicit review requirements for story projects."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps, ImageStat
from pydantic import ValidationError

from src.editorial_v2 import SourceArtwork, StrictModel, _normalized, _repeated
from src.quality_filter import validate_local_image_file
from src.story_plan import StoryPlan, rectangle_overlap, usable_detail


ISSUE_MESSAGES = {
    "manual_review_required": "Yayın öncesi görsel ve içerik incelemesi gerekiyor",
    "geometry_needs_visual_review": "Otomatik önerilen detay alanını görsel olarak kontrol edin",
    "duplicate_source": "Aynı eser kaynak listesinde iki kez yer alıyor",
    "source_rights": "Eserin kullanım hakkı doğrulanamadı",
    "invalid_image": "Kaynak görsel eksik veya geçersiz",
    "source_resolution": "Kaynak görselin çözünürlüğü yetersiz",
    "source_registry_mismatch": "Sayfalar ile kaynak eserler eşleşmiyor",
    "repeated_title": "Bu başlık geçmiş başlıklara fazla benziyor",
    "generic_title": "Başlık daha özgün ve esere bağlı olmalı",
    "unsupported_headline_evidence": "Başlığın kaynak kanıtı doğrulanamadı",
    "unknown_source": "Sayfanın kaynak eseri bulunamadı",
    "detail_resolution": "Detay alanının çözünürlüğü yetersiz",
    "empty_detail": "Detay alanında yeterli görsel içerik yok",
    "repeated_detail": "Bu detay başka bir sayfanın kırpmasını tekrarlıyor",
    "unsupported_context": "Müze notu kaynak alıntısıyla birebir eşleşmiyor",
    "layout_capacity": "Metin okunabilir boyutta sayfaya sığmıyor; kısaltın",
}


class StoryIssue(StrictModel):
    severity: Literal["critical", "warning"]
    code: str
    slide_id: str | None = None
    artwork_id: str | None = None


class StoryQuality(StrictModel):
    can_render: bool
    publication_ready: Literal[False] = False
    issues: tuple[StoryIssue, ...]


def assess_story(
    plan: StoryPlan,
    sources: list[SourceArtwork],
    *,
    headline_history: list[str] | None = None,
) -> StoryQuality:
    issues = [StoryIssue(severity="warning", code="manual_review_required")]

    def critical(code, *, slide=None, artwork=None):
        issues.append(
            StoryIssue(
                severity="critical", code=code, slide_id=slide, artwork_id=artwork
            )
        )

    registry = {}
    dimensions = {}
    for source in sources:
        identity = source.artwork.canonical_id
        if identity in registry:
            critical("duplicate_source", artwork=identity)
        registry[identity] = source
        try:
            SourceArtwork.model_validate(source.model_dump())
        except ValidationError:
            critical("source_rights", artwork=identity)
        path = Path(source.image_path)
        try:
            valid_size = path.stat().st_size <= 30_000_000
        except OSError:
            valid_size = False
        result = validate_local_image_file(str(path)) if valid_size else None
        if result is None or not result.valid:
            critical("invalid_image", artwork=identity)
            continue
        with Image.open(path) as image:
            dimensions[identity] = ImageOps.exif_transpose(image).size
        if min(dimensions[identity]) < 300:
            critical("source_resolution", artwork=identity)
    if set(registry) != set(plan.artwork_ids):
        critical("source_registry_mismatch")
    if _repeated(plan.public_title, headline_history or []):
        critical("repeated_title")
    if any(
        phrase in _normalized(plan.public_title)
        for phrase in (
            "timeless beauty",
            "a visual symphony",
            "a journey through",
            "the beauty of art",
        )
    ):
        critical("generic_title")
    for evidence in plan.headline_evidence:
        source = registry.get(evidence.artwork_id)
        if source is None or (
            evidence.kind == "museum"
            and (
                not evidence.source_quote
                or len(_normalized(evidence.source_quote)) < 8
                or _normalized(evidence.source_quote)
                not in _normalized(source.artwork.description or "")
            )
        ):
            critical("unsupported_headline_evidence", artwork=evidence.artwork_id)
    seen_details = {}
    for slide in plan.slides:
        identity = slide.artwork_ids[0]
        source = registry.get(identity)
        if source is None:
            critical("unknown_source", slide=slide.id, artwork=identity)
            continue
        if slide.role == "detail":
            if identity in dimensions:
                if not usable_detail(*dimensions[identity], slide.focus):
                    critical("detail_resolution", slide=slide.id, artwork=identity)
                with Image.open(source.image_path) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    image.thumbnail((360, 360))
                    box = tuple(
                        round(v * size)
                        for v, size in zip(
                            slide.focus,
                            (image.width, image.height, image.width, image.height),
                        )
                    )
                    if (
                        ImageStat.Stat(ImageOps.grayscale(image.crop(box))).stddev[0]
                        < 6
                    ):
                        critical("empty_detail", slide=slide.id, artwork=identity)
            for previous in seen_details.get(identity, []):
                if rectangle_overlap(slide.focus, previous) >= 0.75:
                    critical("repeated_detail", slide=slide.id, artwork=identity)
            seen_details.setdefault(identity, []).append(slide.focus)
            if slide.focus_basis == "local_geometry":
                issues.append(
                    StoryIssue(
                        severity="warning",
                        code="geometry_needs_visual_review",
                        slide_id=slide.id,
                        artwork_id=identity,
                    )
                )
        elif slide.role == "context":
            quotes = [
                e
                for e in slide.evidence
                if e.kind == "museum" and e.artwork_id == identity
            ]
            if (
                not slide.body
                or not quotes
                or not any(e.source_quote == slide.body for e in quotes)
                or slide.body not in (source.artwork.description or "")
            ):
                critical("unsupported_context", slide=slide.id, artwork=identity)
    return StoryQuality(
        can_render=not any(i.severity == "critical" for i in issues),
        issues=tuple(issues),
    )
