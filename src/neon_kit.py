"""Shared production-safe visual kit for all 180 Artfolio Neon design templates."""

from __future__ import annotations

import colorsys
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from src.astra_renderer import (
    AstraArtworkInput,
    DEJAVU_FONT_DIR,
    REPO_ROOT,
    extract_pigments,
    extract_vivid_accent,
    hexcol,
    ink,
    load_source_image,
    mix,
)

W, H = 1080, 1350

PAPER = (244, 239, 230)
VELLUM = (241, 234, 217)
NEWSPRINT = (239, 236, 228)
OBSIDIAN = (20, 18, 16)
DEEP_NIGHT = (14, 13, 12)
CHARCOAL = (36, 34, 30)
INK = (28, 26, 24)
MUTED_INK = (106, 100, 90)
SOFT_IVORY = (238, 232, 220)
MUTED_LIGHT = (176, 169, 156)
GOLD = (200, 164, 100)
VERMILION = (184, 59, 42)
PURE_WHITE = (255, 255, 255)
RULE_DARK = (58, 53, 47)
RULE_LIGHT = (204, 196, 184)

_REPO_SERIF = str(REPO_ROOT / "assets/fonts/source-serif/SourceSerif4-Regular.otf")
_REPO_SANS = str(REPO_ROOT / "assets/fonts/source-sans/SourceSans3-Regular.otf")
_REPO_CINZEL = str(REPO_ROOT / "assets/fonts/Cinzel.ttf")
_DJ_SANS = str(DEJAVU_FONT_DIR / "DejaVuSans.ttf")
_DJ_BOLD = str(DEJAVU_FONT_DIR / "DejaVuSans-Bold.ttf")
_DJ_COND = str(DEJAVU_FONT_DIR / "DejaVuSansCondensed-Bold.ttf")
_DJ_SERIF = str(DEJAVU_FONT_DIR / "DejaVuSerif.ttf")
_DJ_ITALIC = str(DEJAVU_FONT_DIR / "DejaVuSerif-Italic.ttf")
_DJ_MONO = str(DEJAVU_FONT_DIR / "DejaVuSansMono.ttf")

_FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "serif": (_REPO_SERIF, _DJ_SERIF),
    "didot": ("/System/Library/Fonts/Supplemental/Didot.ttc", _REPO_SERIF, _DJ_SERIF),
    "bodoni": ("/System/Library/Fonts/Supplemental/Bodoni 72.ttc", _REPO_SERIF, _DJ_SERIF),
    "baskerville": ("/System/Library/Fonts/Supplemental/Baskerville.ttc", _REPO_SERIF, _DJ_SERIF),
    "georgia": ("/System/Library/Fonts/Supplemental/Georgia.ttf", _REPO_SERIF, _DJ_SERIF),
    "georgia_i": ("/System/Library/Fonts/Supplemental/Georgia Italic.ttf", _DJ_ITALIC, _REPO_SERIF),
    "georgia_b": ("/System/Library/Fonts/Supplemental/Georgia Bold.ttf", _DJ_SERIF, _REPO_SERIF),
    "cinzel": (_REPO_CINZEL, _REPO_SERIF, _DJ_SERIF),
    "sans": (_REPO_SANS, _DJ_SANS),
    "bold": (_DJ_BOLD, _REPO_SANS),
    "futura": ("/System/Library/Fonts/Supplemental/Futura.ttc", _DJ_BOLD, _REPO_SANS),
    "din_b": ("/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf", _DJ_BOLD, _REPO_SANS),
    "din_c": ("/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf", _DJ_COND, _DJ_BOLD),
    "mono": ("/System/Library/Fonts/Supplemental/Courier New.ttf", _DJ_MONO),
    "mono_b": ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", _DJ_MONO, _DJ_BOLD),
}


@lru_cache(maxsize=600)
def F(name: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = _FONT_CANDIDATES.get(name, (_REPO_SANS, _DJ_SANS))
    px = max(10, int(round(size)))
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, px)
            except OSError:
                continue
    return ImageFont.truetype(_DJ_SANS, px)


def cover_crop(img: Image.Image, tw: int, th: int, focus: tuple[float, float] = (0.5, 0.5), zoom: float = 1.0) -> Image.Image:
    w, h = img.size
    tar, sar = tw / max(1, th), w / max(1, h)
    if sar > tar:
        ch, cw = float(h), h * tar
    else:
        cw, ch = float(w), w / tar
    cw /= max(1.0, zoom)
    ch /= max(1.0, zoom)
    x0 = max(0.0, min(w - cw, focus[0] * w - cw / 2.0))
    y0 = max(0.0, min(h - ch, focus[1] * h - ch / 2.0))
    return img.resize((max(1, tw), max(1, th)), Image.Resampling.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))


def contain_fit(img: Image.Image, mw: int, mh: int) -> Image.Image:
    return ImageOps.contain(img, (max(1, mw), max(1, mh)), Image.Resampling.LANCZOS)


def add_shadow(canvas: Image.Image, box: tuple[int, int, int, int], blur: int = 18, alpha: int = 52, offset: tuple[int, int] = (0, 10)) -> None:
    x0, y0, x1, y1 = box
    ov = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rectangle((x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]), fill=(10, 8, 6, alpha))
    ov = ov.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), ov).convert("RGB"))


def paste_centered(canvas: Image.Image, img: Image.Image, box: tuple[int, int, int, int], shadow: bool = False) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    fitted = contain_fit(img, x1 - x0, y1 - y0)
    px = x0 + (x1 - x0 - fitted.width) // 2
    py = y0 + (y1 - y0 - fitted.height) // 2
    if shadow:
        add_shadow(canvas, (px, py, px + fitted.width, py + fitted.height))
    canvas.paste(fitted, (px, py))
    return (px, py, px + fitted.width, py + fitted.height)


def apply_scrim(
    canvas: Image.Image,
    direction: str = "bottom",
    frac: float = 0.44,
    color: tuple[int, int, int] = (12, 10, 9),
    max_alpha: int = 230,
) -> None:
    w, h = canvas.size
    if direction == "bottom":
        span = max(1, int(h * frac))
        alpha = np.uint8(np.round(max_alpha * (np.linspace(0.0, 1.0, span) ** 1.4)))
        mask = Image.fromarray(np.repeat(alpha[:, None], w, axis=1))
        canvas.paste(Image.new("RGB", (w, span), color), (0, h - span), mask)
    elif direction == "top":
        span = max(1, int(h * frac))
        alpha = np.uint8(np.round(max_alpha * (np.linspace(1.0, 0.0, span) ** 1.4)))
        mask = Image.fromarray(np.repeat(alpha[:, None], w, axis=1))
        canvas.paste(Image.new("RGB", (w, span), color), (0, 0), mask)
    elif direction == "left":
        span = max(1, int(w * frac))
        alpha = np.uint8(np.round(max_alpha * (np.linspace(1.0, 0.0, span) ** 1.4)))
        mask = Image.fromarray(np.repeat(alpha[None, :], h, axis=0))
        canvas.paste(Image.new("RGB", (span, h), color), (0, 0), mask)


def fit_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_name: str,
    size: int,
    max_w: int,
    *,
    min_size: int = 18,
    max_lines: int = 3,
    lh_mult: float = 1.16,
    max_h: int | None = None,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    cleaned = " ".join(str(text).strip().split())
    if not cleaned:
        return F(font_name, size), [""], int(round(size * lh_mult))
    explicit = [ln.strip() for ln in str(text).splitlines() if ln.strip()] if "\n" in str(text) else None

    cur_size = size
    while cur_size >= min_size:
        font = F(font_name, cur_size)
        if explicit is not None:
            lines = explicit
        else:
            words, lines, cur = cleaned.split(), [], ""
            for w_tok in words:
                cand = f"{cur} {w_tok}".strip()
                if cur and draw.textlength(cand, font=font) > max_w:
                    lines.append(cur)
                    cur = w_tok
                else:
                    cur = cand
            if cur:
                lines.append(cur)
        widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        lh = int(round(cur_size * lh_mult))
        total_h = len(lines) * lh
        if len(lines) <= max_lines and widest <= max_w and (max_h is None or total_h <= max_h):
            return font, lines, lh
        cur_size -= 2

    font = F(font_name, min_size)
    words, lines, cur = cleaned.split(), [], ""
    for w_tok in words:
        cand = f"{cur} {w_tok}".strip()
        if cur and draw.textlength(cand, font=font) > max_w:
            lines.append(cur)
            cur = w_tok
        else:
            cur = cand
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return font, lines, int(round(min_size * lh_mult))


def draw_fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font_name: str,
    size: int,
    max_w: int,
    fill: tuple[int, int, int],
    *,
    min_size: int = 18,
    max_lines: int = 2,
    lh_mult: float = 1.15,
    max_h: int | None = None,
    align: str = "left",
) -> int:
    font, lines, lh = fit_lines(
        draw, text, font_name, size, max_w, min_size=min_size, max_lines=max_lines, lh_mult=lh_mult, max_h=max_h
    )
    x, y = xy
    for ln in lines:
        lw = draw.textlength(ln, font=font)
        dx = x + (max_w - lw) / 2 if align == "center" else (x + max_w - lw if align == "right" else x)
        draw.text((dx, y), ln, font=font, fill=fill)
        y += lh
    return y


def make_noise_surface(w: int, h: int, base: tuple[int, int, int], amp: int = 10, seed: int = 42) -> Image.Image:
    rng = np.random.default_rng(seed)
    sw, sh = max(1, w // 4), max(1, h // 4)
    noise = rng.integers(-amp, amp + 1, size=(sh, sw, 1))
    arr = np.clip(np.array(base, dtype=np.int16)[None, None, :] + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr).resize((w, h), Image.Resampling.BILINEAR)


def parse_year(date_str: str) -> int | None:
    match = re.search(r"(1[0-9]{3}|20[0-2][0-9])", str(date_str or ""))
    return int(match.group(1)) if match else None


def short_museum(institution: str) -> str:
    raw = str(institution or "").strip()
    if not raw:
        return "MUSEUM COLLECTION"
    low = raw.lower()
    if "metropolitan" in low:
        return "THE MET"
    if "rijksmuseum" in low:
        return "RIJKSMUSEUM"
    if "chicago" in low:
        return "ART INSTITUTE OF CHICAGO"
    if "cleveland" in low:
        return "CLEVELAND MUSEUM OF ART"
    return raw.upper()[:36]


def byline_for(art: AstraArtworkInput) -> str:
    parts = [art.artist]
    if art.date:
        parts.append(art.date)
    if art.institution:
        parts.append(short_museum(art.institution))
    return "  ·  ".join(p for p in parts if p)


def thermal_false_color(img: Image.Image) -> Image.Image:
    """Convert an image into a real conservation infrared/thermal false-color map."""
    gray = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    r = np.clip((gray - 0.35) * 2.2, 0, 1) * 255
    g = np.clip(np.sin(gray * math.pi) * 1.15, 0, 1) * 255
    b = np.clip((0.68 - gray) * 1.9, 0, 1) * 255
    rgb_arr = np.stack([r, g, b], axis=-1).astype(np.uint8)
    return Image.fromarray(rgb_arr)


def duotone_image(img: Image.Image, dark: tuple[int, int, int], light: tuple[int, int, int]) -> Image.Image:
    gray = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    d_arr = np.array(dark, dtype=np.float32)
    l_arr = np.array(light, dtype=np.float32)
    out = np.clip(d_arr[None, None, :] * (1.0 - gray[:, :, None]) + l_arr[None, None, :] * gray[:, :, None], 0, 255)
    return Image.fromarray(out.astype(np.uint8))


def halftone_dots(img: Image.Image, cell: int = 10, bg: tuple[int, int, int] = PAPER, dot: tuple[int, int, int] = INK) -> Image.Image:
    w, h = img.size
    gray = np.asarray(img.convert("L").resize((max(1, w // cell), max(1, h // cell)), Image.Resampling.BILINEAR))
    out = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(out)
    gh, gw = gray.shape
    for gy in range(gh):
        for gx in range(gw):
            darkness = 1.0 - (float(gray[gy, gx]) / 255.0)
            rad = int(round((cell * 0.48) * math.sqrt(max(0.0, darkness))))
            if rad >= 1:
                cx = gx * cell + cell // 2
                cy = gy * cell + cell // 2
                draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=dot)
    return out


def compute_image_metrics(img: Image.Image) -> dict[str, float]:
    """Compute real visual DNA metrics (luminance, warmth, saturation, contrast) from pixels."""
    arr = np.asarray(img.resize((120, 120), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    cmax = arr.max(axis=2)
    cmin = arr.min(axis=2)
    sat = np.where(cmax > 0, (cmax - cmin) / np.maximum(cmax, 1e-5), 0.0)
    warmth = float(np.clip(0.5 + np.mean(r - b) * 1.4, 0.05, 0.98))
    return {
        "luminance": float(np.clip(np.mean(luma), 0.05, 0.98)),
        "contrast": float(np.clip(np.std(luma) * 2.6, 0.08, 0.98)),
        "saturation": float(np.clip(np.mean(sat), 0.05, 0.98)),
        "warmth": warmth,
    }


def draw_brand_tag(draw: ImageDraw.ImageDraw, x: int, y: int, fill: tuple[int, int, int] = GOLD, label: str = "ART.FOLIO") -> None:
    draw.text((x, y), label, font=F("mono_b", 20), fill=fill)


__all__ = [
    "W", "H", "PAPER", "VELLUM", "NEWSPRINT", "OBSIDIAN", "DEEP_NIGHT", "CHARCOAL",
    "INK", "MUTED_INK", "SOFT_IVORY", "MUTED_LIGHT", "GOLD", "VERMILION", "PURE_WHITE",
    "RULE_DARK", "RULE_LIGHT", "F", "cover_crop", "contain_fit", "add_shadow",
    "paste_centered", "apply_scrim", "draw_fit_text", "make_noise_surface",
    "parse_year", "short_museum", "byline_for", "thermal_false_color", "duotone_image",
    "halftone_dots", "compute_image_metrics", "draw_brand_tag", "extract_pigments",
    "extract_vivid_accent", "hexcol", "ink", "load_source_image", "mix",
    "AstraArtworkInput", "Image", "ImageChops", "ImageDraw", "ImageEnhance", "ImageFilter", "ImageOps", "np", "math", "colorsys",
]
