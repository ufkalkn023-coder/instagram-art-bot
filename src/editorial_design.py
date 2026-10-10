"""Local editorial cover layouts for Artfolio's review gallery."""

from __future__ import annotations

import math
from enum import Enum
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageOps

from src.carousel_cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    _fit_cover_copy,
    _load_font,
    create_carousel_editorial_cover,
)
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown


class CoverStyle(str, Enum):
    """Supported cover compositions."""

    MUSEUM_JOURNAL = "museum_journal"
    ARTWORK_FIRST = "artwork_first"
    DETAIL_STUDY = "detail_study"


FIELD = (242, 239, 231)
INK = (37, 37, 34)


def _validate_focus(
    focus: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if len(focus) != 4:
        raise ValueError(
            "Focus must contain normalized left, top, right, bottom coordinates"
        )
    values = tuple(float(value) for value in focus)
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        raise ValueError("Focus coordinates must be finite values within [0, 1]")
    left, top, right, bottom = values
    area = (right - left) * (bottom - top)
    if right <= left or bottom <= top or area < 0.08 or area > 0.85:
        raise ValueError(
            "Focus area must be between 0.08 and 0.85 with positive dimensions"
        )
    return values


def _draw_copy(
    canvas: Image.Image,
    title: str,
    subtitle: str,
    *,
    title_size: int = 68,
    caption_top: int = 880,
) -> None:
    draw = ImageDraw.Draw(canvas)
    margin = 84
    width = COVER_WIDTH - margin * 2
    title_block = _fit_cover_copy(
        draw,
        title,
        size=title_size,
        minimum_size=38,
        max_width=width,
        max_height=250,
        serif=True,
    )
    deck_block = _fit_cover_copy(
        draw,
        subtitle,
        size=30,
        minimum_size=22,
        max_width=width,
        max_height=80,
    )
    blocks = (title_block, deck_block)
    block_height = sum(len(lines) * line_height for _, lines, line_height in blocks)
    block_height += 14
    caption_bottom = COVER_HEIGHT - 30
    y = caption_bottom - block_height
    if y < caption_top:
        raise ValueError("Editorial cover copy exceeds the readable layout capacity")
    for font, lines, line_height in blocks:
        for line in lines:
            draw.text((margin, y), line, font=font, fill=INK, anchor="lt")
            y += line_height
        y += 14


def _artwork_first(image: Image.Image, title: str, subtitle: str) -> Image.Image:
    canvas = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), FIELD)
    source = ImageOps.exif_transpose(image).convert("RGB")
    contained = ImageOps.contain(source, (920, 760), Image.Resampling.LANCZOS)
    canvas.paste(
        contained,
        ((COVER_WIDTH - contained.width) // 2, 90 + (760 - contained.height) // 2),
    )
    _draw_copy(canvas, title, subtitle)
    return canvas


def _detail_study(
    image: Image.Image,
    title: str,
    subtitle: str,
    focus: tuple[float, float, float, float] | None,
) -> tuple[Image.Image, str]:
    source = ImageOps.exif_transpose(image).convert("RGB")
    basis = "model_focus"
    if focus is None:
        crop = source
        basis = "missing_focus_full_artwork"
    else:
        left, top, right, bottom = _validate_focus(focus)
        crop = source.crop(
            (
                round(left * source.width),
                round(top * source.height),
                round(right * source.width),
                round(bottom * source.height),
            )
        )
    canvas = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), FIELD)
    contained = ImageOps.contain(crop, (920, 790), Image.Resampling.LANCZOS)
    canvas.paste(
        contained,
        ((COVER_WIDTH - contained.width) // 2, 105 + (790 - contained.height) // 2),
    )
    label = "DETAIL STUDY" if focus is not None else "FULL ARTWORK"
    ImageDraw.Draw(canvas).text(
        (84, 940), label, font=_load_font(20), fill=INK, anchor="lt"
    )
    _draw_copy(canvas, title, subtitle, caption_top=980)
    return canvas, basis


def render_editorial_cover(
    image_path: str,
    public_title: str,
    subtitle: str,
    style: CoverStyle,
    output_path: str,
    focus: tuple[float, float, float, float] | None = None,
    *,
    focus_basis: Literal["model_focus", "preview_focus"] = "model_focus",
) -> dict[str, str]:
    """Render a 1080×1350 JPEG and report the artwork crop basis."""
    style = CoverStyle(style)
    if focus_basis not in {"model_focus", "preview_focus"}:
        raise ValueError("Unsupported focus provenance")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if style is CoverStyle.MUSEUM_JOURNAL:
        score = CoverScoreBreakdown(0, 0, 0, 0, 0, 0, 0)
        cover = CoverAsset(
            {"id": "local-preview"}, image_path, CoverMode.FULL_ARTWORK, 0, score
        )
        create_carousel_editorial_cover(
            cover=cover,
            editorial_title=public_title,
            editorial_subtitle=subtitle,
            output_path=str(destination),
        )
        basis = "full_artwork"
    else:
        with Image.open(image_path) as source:
            if style is CoverStyle.ARTWORK_FIRST:
                canvas = _artwork_first(source, public_title, subtitle)
                basis = "full_artwork"
            else:
                canvas, basis = _detail_study(source, public_title, subtitle, focus)
                if focus is not None:
                    basis = focus_basis
        canvas.save(destination, "JPEG", quality=95, optimize=True)
    actual_style = style.value
    if style is CoverStyle.DETAIL_STUDY and basis == "missing_focus_full_artwork":
        actual_style = CoverStyle.ARTWORK_FIRST.value
    return {
        "output_path": str(destination),
        "requested_style": style.value,
        "actual_style": actual_style,
        "crop_basis": basis,
    }
