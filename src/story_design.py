"""Local, role-aware renderer for reviewable Artfolio story pages."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.carousel_cover import COVER_HEIGHT, COVER_WIDTH, _load_font
from src.editorial_design import CoverStyle, FIELD, INK, render_editorial_cover
from src.editorial_v2 import SourceArtwork
from src.quality_filter import validate_local_image_file
from src.story_plan import StorySlide

RENDERER_VERSION = "artfolio-story-renderer-v1"
_MARGIN = 80
_IMAGE_BOX = (920, 740)


def _source(sources: Mapping[str, SourceArtwork], identity: str) -> SourceArtwork:
    try:
        source = sources[identity]
    except KeyError as exc:
        raise ValueError(f"Missing source artwork for {identity}") from exc
    result = validate_local_image_file(source.image_path)
    if not result.valid:
        raise ValueError(
            f"Source artwork image is invalid ({result.reason}): {identity}"
        )
    return source


def _credit(source: SourceArtwork) -> str:
    art = source.artwork
    artist = art.artist_display_name or art.artist_name
    date = art.creation_date_display or art.creation_date or "Unknown Date"
    return f"{artist} · {date} · {art.museum_name}"


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    words = " ".join(text.split()).split(" ") if text.strip() else []
    lines: list[str] = []
    current = ""
    for word in words:
        if draw.textlength(word, font=font) > width:
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for char in word:
                if chunk and draw.textlength(chunk + char, font=font) > width:
                    lines.append(chunk)
                    chunk = ""
                chunk += char
            current = chunk
            continue
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    width: int,
    height: int,
    size: int,
    minimum: int,
    serif: bool = False,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    for font_size in range(size, minimum - 1, -2):
        font = _load_font(font_size, serif=serif)
        lines = _wrap(draw, text, font, width)
        line_height = font_size + max(5, font_size // 5)
        if len(lines) * line_height <= height:
            return font, lines, line_height
    raise ValueError("Story copy exceeds the readable layout capacity")


def _draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    *,
    width: int,
    height: int,
    size: int,
    minimum: int,
    serif: bool = False,
) -> int:
    font, lines, line_height = _fit_text(
        draw, text, width=width, height=height, size=size, minimum=minimum, serif=serif
    )
    y = xy[1]
    for line in lines:
        draw.text((xy[0], y), line, font=font, fill=INK, anchor="lt")
        y += line_height
    return y


def _fit_image(source: SourceArtwork, box: tuple[int, int], focus=None) -> Image.Image:
    with Image.open(source.image_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    if focus is not None:
        left, top, right, bottom = focus
        image = image.crop(
            (
                round(left * image.width),
                round(top * image.height),
                round(right * image.width),
                round(bottom * image.height),
            )
        )
    return ImageOps.contain(image, box, Image.Resampling.LANCZOS)


def _page() -> Image.Image:
    return Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), FIELD)


def _header(canvas: Image.Image, position: int, total: int) -> None:
    draw = ImageDraw.Draw(canvas)
    font = _load_font(20)
    draw.text((_MARGIN, 38), "ARTFOLIO", font=font, fill=INK, anchor="lt")
    draw.text(
        (COVER_WIDTH - _MARGIN, 38),
        f"{position:02d} / {total:02d}",
        font=font,
        fill=INK,
        anchor="rt",
    )
    draw.line((_MARGIN, 78, COVER_WIDTH - _MARGIN, 78), fill=(190, 185, 174), width=1)


def _draw_single(slide: StorySlide, source: SourceArtwork) -> Image.Image:
    canvas = _page()
    image = _fit_image(source, _IMAGE_BOX)
    canvas.paste(
        image,
        ((COVER_WIDTH - image.width) // 2, 118 + (_IMAGE_BOX[1] - image.height) // 2),
    )
    draw = ImageDraw.Draw(canvas)
    title = slide.title or source.artwork.title
    y = _draw_text(
        draw,
        title,
        (_MARGIN, 900),
        width=920,
        height=112,
        size=48,
        minimum=32,
        serif=True,
    )
    if slide.body:
        y = _draw_text(
            draw,
            slide.body,
            (_MARGIN, y + 8),
            width=920,
            height=max(24, 1210 - y),
            size=34,
            minimum=28,
        )
    _draw_text(
        draw,
        _credit(source),
        (_MARGIN, 1240),
        width=920,
        height=70,
        size=26,
        minimum=22,
    )
    return canvas


def _draw_detail(slide: StorySlide, source: SourceArtwork) -> Image.Image:
    canvas = _page()
    image = _fit_image(source, (920, 710), slide.focus)
    canvas.paste(
        image, ((COVER_WIDTH - image.width) // 2, 118 + (710 - image.height) // 2)
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((_MARGIN, 850), "DETAIL", font=_load_font(18), fill=INK, anchor="lt")
    title = slide.title or "A closer look"
    y = _draw_text(
        draw,
        title,
        (_MARGIN, 890),
        width=920,
        height=100,
        size=44,
        minimum=30,
        serif=True,
    )
    if slide.body:
        _draw_text(
            draw,
            slide.body,
            (_MARGIN, y + 8),
            width=920,
            height=max(24, 1210 - y),
            size=34,
            minimum=28,
        )
    _draw_text(
        draw,
        _credit(source),
        (_MARGIN, 1240),
        width=920,
        height=70,
        size=26,
        minimum=22,
    )
    return canvas


def _draw_comparison(
    slide: StorySlide, sources: tuple[SourceArtwork, SourceArtwork]
) -> Image.Image:
    canvas = _page()
    draw = ImageDraw.Draw(canvas)
    for index, source in enumerate(sources):
        x = 80 + index * 480
        image = _fit_image(source, (440, 570))
        canvas.paste(
            image, (x + (440 - image.width) // 2, 125 + (570 - image.height) // 2)
        )
        _draw_text(
            draw,
            source.artwork.title,
            (x, 720),
            width=440,
            height=105,
            size=32,
            minimum=22,
            serif=True,
        )
        _draw_text(
            draw, _credit(source), (x, 840), width=440, height=180, size=26, minimum=22
        )
    _draw_text(
        draw,
        slide.title or "Look side by side",
        (80, 1080),
        width=920,
        height=80,
        size=38,
        minimum=28,
        serif=True,
    )
    if slide.body:
        _draw_text(
            draw, slide.body, (80, 1170), width=920, height=130, size=32, minimum=28
        )
    return canvas


def _draw_context(slide: StorySlide, source: SourceArtwork) -> Image.Image:
    quote = slide.body
    if not quote.strip():
        raise ValueError("Context slides require an exact museum quotation")
    description = source.artwork.description or ""
    evidence_quotes = [e.source_quote for e in slide.evidence if e.kind == "museum"]
    if quote not in description or quote not in evidence_quotes:
        raise ValueError(
            "Context quotation must match source metadata and slide evidence exactly"
        )
    canvas = _page()
    image = _fit_image(source, (300, 360))
    canvas.paste(
        image, (80 + (300 - image.width) // 2, 140 + (360 - image.height) // 2)
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((430, 140), "FROM THE MUSEUM", font=_load_font(18), fill=INK, anchor="lt")
    _draw_text(
        draw,
        f"“{quote}”",
        (430, 185),
        width=570,
        height=420,
        size=35,
        minimum=24,
        serif=True,
    )
    title = slide.title or "From the museum"
    _draw_text(
        draw, title, (80, 650), width=920, height=100, size=35, minimum=25, serif=True
    )
    _draw_text(
        draw, _credit(source), (80, 805), width=920, height=150, size=20, minimum=16
    )
    return canvas


def render_story_slide(
    slide: StorySlide,
    sources: dict[str, SourceArtwork],
    public_title: str,
    cover_style: CoverStyle,
    output_path: str,
    *,
    position: int,
    total: int,
    cover_detail: StorySlide | None = None,
) -> dict[str, str | int | None]:
    """Render one validated story slide to an 1080×1350 JPEG."""
    if total < 1 or not 1 <= position <= total:
        raise ValueError("Page position must be within the story total")
    selected_cover_style = CoverStyle(cover_style)
    if cover_detail is not None:
        if (
            slide.role != "cover"
            or cover_detail.role != "detail"
            or cover_detail.artwork_ids != slide.artwork_ids
        ):
            raise ValueError(
                "Cover detail must be a detail slide for the cover artwork"
            )
    artworks = tuple(_source(sources, identity) for identity in slide.artwork_ids)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, str | int | None] = {
        "output_path": str(destination),
        "role": slide.role,
        "actual_style": None,
        "crop_basis": None,
        "renderer_version": RENDERER_VERSION,
    }
    if slide.role == "cover":
        focus = (
            cover_detail.focus
            if cover_detail is not None
            and selected_cover_style is CoverStyle.DETAIL_STUDY
            else None
        )
        focus_basis = "preview_focus"
        if cover_detail is not None and cover_detail.focus_basis in {
            "preview_focus",
            "model_focus",
        }:
            focus_basis = cover_detail.focus_basis
        cover_credit = _credit(artworks[0])
        result = render_editorial_cover(
            artworks[0].image_path,
            public_title,
            cover_credit,
            selected_cover_style,
            str(destination),
            focus,
            focus_basis=focus_basis,
        )
        # The cover helper owns cover styling. Add only the stable pagination band.
        with Image.open(destination) as rendered:
            canvas = rendered.convert("RGB")
        if result["actual_style"] == CoverStyle.MUSEUM_JOURNAL.value:
            # Museum Journal already owns its brand header on the image.
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((890, 65, 1000, 108), radius=5, fill=FIELD)
            draw.text(
                (985, 75),
                f"{position:02d} / {total:02d}",
                font=_load_font(20),
                fill=INK,
                anchor="rt",
            )
        else:
            _header(canvas, position, total)
        canvas.save(destination, "JPEG", quality=95, optimize=True)
        crop_basis = (
            cover_detail.focus_basis
            if result["actual_style"] == CoverStyle.DETAIL_STUDY.value
            and focus is not None
            else result["crop_basis"]
        )
        metadata.update(actual_style=result["actual_style"], crop_basis=crop_basis)
        return metadata
    if slide.role in {"artwork", "closing"}:
        canvas = _draw_single(slide, artworks[0])
        metadata["crop_basis"] = "full_artwork"
    elif slide.role == "detail":
        canvas = _draw_detail(slide, artworks[0])
        metadata["crop_basis"] = slide.focus_basis
    elif slide.role == "comparison":
        canvas = _draw_comparison(slide, (artworks[0], artworks[1]))
        metadata["crop_basis"] = "full_artwork"
    elif slide.role == "context":
        canvas = _draw_context(slide, artworks[0])
        metadata["crop_basis"] = "full_artwork"
    else:
        raise ValueError(f"Unsupported story role: {slide.role}")
    _header(canvas, position, total)
    canvas.save(destination, "JPEG", quality=95, optimize=True)
    return metadata
