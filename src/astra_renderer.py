"""Production-safe Pillow renderer for all 100 Artfolio Astra design templates (astra-001..astra-100)."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from src.astra_catalog import get_design

REPO_ROOT = Path(__file__).resolve().parents[1]
DEJAVU_FONT_DIR = REPO_ROOT / "assets" / "fonts" / "dejavu"
URW_FONT_DIR = Path("/usr/share/fonts/opentype/urw-base35")

CANVAS_BASE_W = 540
CANVAS_BASE_H = 675
CANVAS_SCALE = 2
OUTPUT_SIZE = (CANVAS_BASE_W * CANVAS_SCALE, CANVAS_BASE_H * CANVAS_SCALE)

BRAND = "art.folio"
PAPER = "#f2eee3"
BLACK = "#171816"
WHITE = "#fff9ed"

URW_FONTS: dict[str, str] = {
    "sans": "NimbusSans-Regular",
    "bold": "NimbusSans-Bold",
    "cond": "NimbusSansNarrow-Bold",
    "serif": "NimbusRoman-Regular",
    "italic": "NimbusRoman-Italic",
    "book": "P052-Roman",
    "mono": "NimbusMonoPS-Regular",
    "script": "Z003-MediumItalic",
    "gothic": "URWGothic-Demi",
}

BUNDLED_FONTS: dict[str, str] = {
    "sans": "DejaVuSans.ttf",
    "bold": "DejaVuSans-Bold.ttf",
    "cond": "DejaVuSansCondensed-Bold.ttf",
    "serif": "DejaVuSerif.ttf",
    "italic": "DejaVuSerif-Italic.ttf",
    "book": "DejaVuSerif.ttf",
    "mono": "DejaVuSansMono.ttf",
    "script": "DejaVuSerif-Italic.ttf",
    "gothic": "DejaVuSans-Bold.ttf",
}


@dataclass(frozen=True)
class AstraArtworkInput:
    """Real museum artwork input bound to a physical local image file."""

    image_path: str | Path
    title: str
    artist: str
    date: str = ""
    institution: str = ""
    museum_url: str = ""
    focus: tuple[float, float] | tuple[float, float, float, float] | None = None

    def validated_path(self) -> Path:
        path = Path(self.image_path)
        if not path.is_file():
            raise FileNotFoundError(f"Artwork source image not found: {path}")
        return path

    def focal_point(self, default: tuple[float, float] = (0.5, 0.5)) -> tuple[float, float]:
        if self.focus is None:
            return default
        return normalize_focus_point(self.focus)


def normalize_focus_point(
    focus: tuple[float, float] | tuple[float, float, float, float],
) -> tuple[float, float]:
    """Convert either (fx, fy) or normalized (left, top, right, bottom) into a valid focal point."""
    values = tuple(float(v) for v in focus)
    if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in values):
        raise ValueError("Focus coordinates must be finite values within [0, 1]")
    if len(values) == 2:
        return values[0], values[1]
    if len(values) == 4:
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            raise ValueError("Focus box must have positive width and height")
        return (left + right) / 2.0, (top + bottom) / 2.0
    raise ValueError("Focus must be a 2-tuple (fx, fy) or 4-tuple (left, top, right, bottom)")


@lru_cache(maxsize=600)
def load_astra_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    """Load URW Base35 font on Linux if present, otherwise bundled DejaVu fonts."""
    if style not in BUNDLED_FONTS:
        raise ValueError(f"Unknown Astra font style: {style}")
    urw_candidate = URW_FONT_DIR / f"{URW_FONTS[style]}.otf"
    if urw_candidate.is_file():
        return ImageFont.truetype(str(urw_candidate), size)
    bundled = DEJAVU_FONT_DIR / BUNDLED_FONTS[style]
    if bundled.is_file():
        return ImageFont.truetype(str(bundled), size)
    raise FileNotFoundError(f"Missing bundled font for style '{style}': {bundled}")


def load_source_image(path: str | Path) -> Image.Image:
    """Open and EXIF-normalize a real local artwork image as RGB."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Artwork source image not found: {file_path}")
    with Image.open(file_path) as raw:
        return ImageOps.exif_transpose(raw).convert("RGB")


def rgb(color: str | tuple[int, int, int]) -> tuple[int, int, int]:
    return ImageColor.getrgb(color) if isinstance(color, str) else tuple(int(v) for v in color[:3])


def mix(a: str | tuple[int, int, int], b: str | tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(rgb(a), rgb(b)))


def ink(color: str | tuple[int, int, int]) -> str:
    """Pick high-contrast dark or warm-white ink using WCAG relative luminance."""
    r, g, b = rgb(color)

    def _lin(channel: int) -> float:
        s = channel / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    return "#1a1915" if luminance > 0.178 else WHITE


def hexcol(color: str | tuple[int, int, int]) -> str:
    r, g, b = rgb(color)
    return f"#{r:02X}{g:02X}{b:02X}"


def extract_pigments(image: Image.Image, n: int = 5) -> list[tuple[tuple[int, int, int], int]]:
    """Compute dominant pigments directly from the input artwork image, sorted dark-to-light."""
    sample = image.convert("RGB").copy()
    sample.thumbnail((145, 145))
    quantized = sample.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    ordered = sorted(
        [(tuple(palette[idx * 3 : idx * 3 + 3]), count) for count, idx in counts],
        key=lambda item: sum(item[0]),
    )
    if not ordered:
        ordered = [((42, 42, 42), 1)]
    while len(ordered) < n:
        base_color, base_count = ordered[-1]
        step = len(ordered) / max(n, 1)
        ordered.append((mix(base_color, PAPER, min(0.85, 0.18 * step)), max(1, base_count // 2)))
    return ordered[:n]


def extract_vivid_accent(image: Image.Image) -> tuple[int, int, int]:
    """Compute the most saturated rich accent tone directly from the input artwork."""
    small = image.convert("RGB").resize((160, 200))
    px = np.asarray(small).astype(np.float32)
    cmax = px.max(axis=2)
    cmin = px.min(axis=2)
    sat = np.where(cmax > 0, (cmax - cmin) / np.maximum(cmax, 1.0), 0.0)
    mask = (sat > 0.35) & (cmax > 65) & (cmax < 245)
    if np.any(mask):
        med = np.median(px[mask], axis=0).astype(int)
        return (int(med[0]), int(med[1]), int(med[2]))
    pigments = extract_pigments(image, 5)
    return pigments[2][0]


def _box(x: float, y: float, w: float, h: float) -> tuple[int, int, int, int]:
    return tuple(round(v * CANVAS_SCALE) for v in (x, y, x + w, y + h))  # type: ignore[return-value]


def _point(p: tuple[float, float]) -> tuple[int, int]:
    return (round(p[0] * CANVAS_SCALE), round(p[1] * CANVAS_SCALE))


def _split_two_parts(text: str, fallback_left: str = "ART", fallback_right: str = "FOLIO") -> tuple[str, str]:
    cleaned = " ".join(str(text).strip().split())
    if not cleaned:
        return fallback_left, fallback_right
    if "/" in cleaned:
        parts = [p.strip() for p in cleaned.split("/", 1)]
        if parts[0] and parts[1]:
            return parts[0], parts[1]
    words = cleaned.split()
    if len(words) >= 2:
        mid = max(1, len(words) // 2)
        return " ".join(words[:mid]), " ".join(words[mid:])
    word = words[0]
    if len(word) >= 4:
        cut = (len(word) + 1) // 2
        return word[:cut], word[cut:]
    return word, fallback_right


class AstraCard:
    """Shared 1080x1350 vector/image composition canvas ported from ARTFOLIO_ASTRA."""

    def __init__(self, color: str | tuple[int, int, int]):
        self.im = Image.new("RGB", OUTPUT_SIZE, rgb(color))
        self.d = ImageDraw.Draw(self.im)

    def set(self, image: Image.Image) -> None:
        self.im = image.convert("RGB")
        self.d = ImageDraw.Draw(self.im)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        c: str | tuple[int, int, int] | None = None,
        outline: str | tuple[int, int, int] | None = None,
        sw: float = 1,
        r: float = 0,
    ) -> None:
        fill_col = rgb(c) if c is not None else None
        out_col = rgb(outline) if outline is not None else None
        if r:
            self.d.rounded_rectangle(
                _box(x, y, w, h),
                radius=round(r * CANVAS_SCALE),
                fill=fill_col,
                outline=out_col,
                width=max(1, round(sw * CANVAS_SCALE)),
            )
        else:
            self.d.rectangle(
                _box(x, y, w, h),
                fill=fill_col,
                outline=out_col,
                width=max(1, round(sw * CANVAS_SCALE)),
            )

    def ellipse(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        c: str | tuple[int, int, int] | None = None,
        outline: str | tuple[int, int, int] | None = None,
        sw: float = 1,
    ) -> None:
        fill_col = rgb(c) if c is not None else None
        out_col = rgb(outline) if outline is not None else None
        self.d.ellipse(
            _box(x, y, w, h),
            fill=fill_col,
            outline=out_col,
            width=max(1, round(sw * CANVAS_SCALE)),
        )

    def line(self, pts: Sequence[tuple[float, float]], c: str | tuple[int, int, int], sw: float = 1) -> None:
        self.d.line(
            [_point(p) for p in pts],
            fill=rgb(c),
            width=max(1, round(sw * CANVAS_SCALE)),
            joint="curve",
        )

    def polygon(self, pts: Sequence[tuple[float, float]], c: str | tuple[int, int, int]) -> None:
        self.d.polygon([_point(p) for p in pts], fill=rgb(c))

    def dot(self, x: float, y: float, r: float, c: str | tuple[int, int, int]) -> None:
        self.ellipse(x - r, y - r, r * 2, r * 2, c)

    def text(
        self,
        s: str,
        x: float,
        y: float,
        sz: float = 20,
        c: str | tuple[int, int, int] = BLACK,
        f: str = "sans",
        a: str = "la",
        maxw: float | None = None,
        track: float = 0,
    ) -> None:
        s = str(s).strip()
        if not s:
            return
        fill_col = rgb(c)
        fs = max(4, round(sz * CANVAS_SCALE))
        while fs > 10:
            ff = load_astra_font(f, fs)
            length = self.d.textlength(s, font=ff) + max(0, len(s) - 1) * track * CANVAS_SCALE
            if maxw is None or length <= maxw * CANVAS_SCALE:
                break
            fs -= 1
        ff = load_astra_font(f, fs)
        if not track:
            self.d.text(_point((x, y)), s, font=ff, fill=fill_col, anchor=a)
            return
        px, py = _point((x, y))
        length = self.d.textlength(s, font=ff) + (len(s) - 1) * track * CANVAS_SCALE
        if a[0] == "m":
            px -= length / 2
        elif a[0] == "r":
            px -= length
        for ch in s:
            self.d.text((px, py), ch, font=ff, fill=fill_col, anchor="l" + a[1])
            px += self.d.textlength(ch, font=ff) + track * CANVAS_SCALE

    def title_block(
        self,
        s: str,
        x: float,
        y: float,
        *,
        sz: float = 64,
        min_sz: float = 16,
        c: str | tuple[int, int, int] = BLACK,
        f: str = "serif",
        a: str = "lt",
        maxw: float = 470,
        maxh: float = 140,
        leading: float = 0.96,
        max_lines: int = 3,
    ) -> None:
        """Render an editorial title with safe wrapping and height/width auto-fitting."""
        cleaned = " ".join(str(s).strip().split())
        if not cleaned:
            return

        explicit_lines = None
        if " / " in s:
            explicit_lines = [part.strip() for part in s.split(" / ") if part.strip()]
        elif "\n" in s:
            explicit_lines = [part.strip() for part in s.splitlines() if part.strip()]

        chosen_sz = float(sz)
        chosen_lines = [cleaned]

        def _wrap_for_size(font_size: float) -> list[str]:
            if explicit_lines is not None:
                return explicit_lines
            ff = load_astra_font(f, max(10, round(font_size * CANVAS_SCALE)))
            words = cleaned.split()
            lines: list[str] = []
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and self.d.textlength(candidate, font=ff) > maxw * CANVAS_SCALE:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                lines.append(current)
            return lines

        while chosen_sz >= min_sz:
            lines = _wrap_for_size(chosen_sz)
            ff = load_astra_font(f, max(10, round(chosen_sz * CANVAS_SCALE)))
            widest = max(self.d.textlength(line, font=ff) for line in lines)
            total_h = len(lines) * chosen_sz * leading
            if len(lines) <= max_lines and widest <= maxw * CANVAS_SCALE and total_h <= maxh:
                chosen_lines = lines
                break
            chosen_sz -= 1.5
        else:
            lines = _wrap_for_size(min_sz)
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = lines[-1].rstrip(".,;:") + "…"
            chosen_sz = min_sz
            chosen_lines = lines

        for idx, line in enumerate(chosen_lines):
            self.text(line, x, y + idx * chosen_sz * leading, chosen_sz, c, f, a, maxw=maxw)

    def wrap(
        self,
        s: str,
        x: float,
        y: float,
        sz: float,
        c: str | tuple[int, int, int] = BLACK,
        f: str = "sans",
        maxw: float = 190,
        step: float = 1.16,
        maxlines: int = 5,
    ) -> None:
        ff = load_astra_font(f, max(10, round(sz * CANVAS_SCALE)))
        lines: list[str] = []
        line = ""
        for word in str(s).split():
            test = (line + " " + word).strip()
            if line and self.d.textlength(test, font=ff) > maxw * CANVAS_SCALE:
                lines.append(line)
                line = word
            else:
                line = test
        if line:
            lines.append(line)
        if len(lines) > maxlines:
            lines = lines[:maxlines]
            lines[-1] = lines[-1] + "…"
        for i, ln in enumerate(lines):
            self.text(ln, x, y + i * sz * step, sz, c, f, "lt", maxw)

    def sig(self, x: float = 33, y: float = 32, c: str | tuple[int, int, int] = BLACK, a: str = "lt") -> None:
        self.text(BRAND, x, y, 17, c, "gothic", a)

    def credit(
        self,
        artwork: AstraArtworkInput,
        x: float = 33,
        y: float = 622,
        c: str | tuple[int, int, int] = BLACK,
        align: str = "lt",
        w: float = 464,
        year: bool = False,
        sz: float = 15,
    ) -> None:
        self.text(artwork.title, x, y, sz, c, "serif", align, w)
        byline = artwork.artist
        if year and artwork.date:
            byline = f"{byline} · {artwork.date}"
        if artwork.institution and len(byline) + len(artwork.institution) <= 58:
            byline = f"{byline} · {artwork.institution}"
        self.text(byline, x, y + 21, 10.5, c, "sans", align, w)

    def rotate_type(
        self,
        s: str,
        x: float,
        y: float,
        w: float,
        h: float,
        sz: float,
        c: str | tuple[int, int, int] = BLACK,
        f: str = "cond",
        direction: int = 90,
    ) -> None:
        s = str(s).strip()
        if not s:
            return
        layer = Image.new("RGBA", (max(1, round(h * CANVAS_SCALE)), max(1, round(w * CANVAS_SCALE))))
        d = ImageDraw.Draw(layer)
        fs = max(12, round(sz * CANVAS_SCALE))
        while fs > 12 and d.textlength(s, font=load_astra_font(f, fs)) > h * CANVAS_SCALE * 0.96:
            fs -= 1
        ff = load_astra_font(f, fs)
        d.text((layer.width / 2, layer.height / 2), s, fill=rgb(c), font=ff, anchor="mm")
        layer = layer.rotate(direction, expand=True)
        self.im.paste(layer, _point((x, y)), layer)

    def art_image(
        self,
        pil: Image.Image,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        mode: str = "cover",
        focus: tuple[float, float] = (0.5, 0.5),
        z: float = 1.0,
        mask: str | list[tuple[float, float]] | None = None,
        tint: str | tuple[int, int, int] | None = None,
        t: float = 0.0,
        mirror: bool = False,
        blur: float = 0.0,
    ) -> tuple[float, float, float, float]:
        working = ImageOps.mirror(pil) if mirror else pil
        fx, fy = focus
        size = (max(1, round(w * CANVAS_SCALE)), max(1, round(h * CANVAS_SCALE)))
        if mode == "fit":
            img = ImageOps.contain(working, size, Image.Resampling.LANCZOS)
            paste_x = round(x * CANVAS_SCALE + (size[0] - img.width) / 2)
            paste_y = round(y * CANVAS_SCALE + (size[1] - img.height) / 2)
            self.im.paste(img, (paste_x, paste_y))
            return (
                x + (w - img.width / CANVAS_SCALE) / 2,
                y + (h - img.height / CANVAS_SCALE) / 2,
                img.width / CANVAS_SCALE,
                img.height / CANVAS_SCALE,
            )

        iw, ih = working.size
        ratio = w / h
        cw, ch = (ih * ratio, ih) if (iw / ih) > ratio else (iw, iw / ratio)
        cw, ch = cw / max(z, 1.0), ch / max(z, 1.0)
        x0 = max(0.0, min(iw - cw, fx * iw - cw / 2.0))
        y0 = max(0.0, min(ih - ch, fy * ih - ch / 2.0))
        img = working.resize(size, Image.Resampling.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))
        if tint is not None and t > 0:
            img = Image.blend(img, Image.new("RGB", size, rgb(tint)), t)
        if blur > 0:
            img = img.filter(ImageFilter.GaussianBlur(blur * CANVAS_SCALE))

        matte = None
        if mask:
            matte = Image.new("L", size, 0)
            md = ImageDraw.Draw(matte)
            if mask == "oval":
                md.ellipse((0, 0, *size), 255)
            elif mask == "arch":
                md.ellipse((0, 0, size[0], size[0]), 255)
                md.rectangle((0, size[0] / 2, size[0], size[1]), 255)
            elif mask == "round":
                md.rounded_rectangle((0, 0, *size), radius=22 * CANVAS_SCALE, fill=255)
            elif isinstance(mask, list):
                md.polygon(
                    [(round(px * size[0]), round(py * size[1])) for px, py in mask],
                    255,
                )
        self.im.paste(img, _point((x, y)), matte)
        return x, y, w, h

    def frame_image(
        self,
        pil: Image.Image,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        wood: str = "#4b382a",
        mat: str = "#eee6d4",
        border: float = 7,
        margin: float = 13,
    ) -> None:
        self.rect(x + 7, y + 10, w, h, "#2b2921")
        self.rect(x, y, w, h, wood)
        self.rect(x + 2, y + 2, w - 4, h - 4, None, mix(wood, WHITE, 0.27))
        self.rect(x + border, y + border, w - border * 2, h - border * 2, mat)
        self.art_image(
            pil,
            x + border + margin,
            y + border + margin,
            w - 2 * (border + margin),
            h - 2 * (border + margin),
            mode="fit",
        )

    def paper_art(
        self,
        pil: Image.Image,
        artist_label: str,
        x: float,
        y: float,
        w: float,
        h: float,
        angle: float,
        mat: str = "#f9edce",
        fit: bool = False,
        focus: tuple[float, float] = (0.5, 0.5),
    ) -> None:
        p = AstraCard(mat)
        p.im = Image.new("RGB", (max(1, round(w * CANVAS_SCALE)), max(1, round(h * CANVAS_SCALE))), rgb(mat))
        p.d = ImageDraw.Draw(p.im)
        p.art_image(pil, 11, 11, w - 22, h - 35, mode="fit" if fit else "cover", focus=focus)
        p.text(artist_label, 12, h - 18, 10, BLACK, "mono", "lt", w - 22)
        layer = p.im.convert("RGBA").rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        pos = (round((x + w / 2) * CANVAS_SCALE - layer.width / 2), round((y + h / 2) * CANVAS_SCALE - layer.height / 2))
        shadow = Image.new("RGBA", layer.size, (9, 7, 4, 175))
        self.im.paste(shadow, (pos[0] + 8, pos[1] + 10), layer.getchannel("A").filter(ImageFilter.GaussianBlur(6)))
        self.im.paste(layer, pos, layer)

    def textured_type(
        self,
        pil: Image.Image,
        s: str,
        x: float,
        y: float,
        w: float,
        h: float,
        sz: float,
        f: str = "cond",
        focus: tuple[float, float] = (0.5, 0.5),
    ) -> None:
        s = (str(s).strip() or "ART").upper()
        temp = AstraCard(BLACK)
        temp.art_image(pil, x, y, w, h, z=1.0, focus=focus)
        matte = Image.new("L", self.im.size, 0)
        md = ImageDraw.Draw(matte)
        fs = max(16, round(sz * CANVAS_SCALE))
        while fs > 16 and md.textlength(s, font=load_astra_font(f, fs)) > w * CANVAS_SCALE * 0.96:
            fs -= 2
        md.text(_point((x + w / 2, y + h / 2)), s, font=load_astra_font(f, fs), fill=255, anchor="mm")
        self.im.paste(temp.im, (0, 0), matte)

    def grain(self, seed: int, amount: float = 1.7) -> None:
        arr = np.asarray(self.im).astype(np.int16)
        noise = np.random.default_rng(seed).normal(0, amount, arr.shape[:2]).astype(np.int16)
        self.set(Image.fromarray(np.clip(arr + noise[:, :, None], 0, 255).astype(np.uint8)))

    def gradient(
        self,
        a: str | tuple[int, int, int],
        b: str | tuple[int, int, int],
        x: float = 0,
        y: float = 0,
        w: float = CANVAS_BASE_W,
        h: float = CANVAS_BASE_H,
        axis: int = 0,
    ) -> None:
        steps = max(1, round((h if axis == 0 else w) * CANVAS_SCALE))
        p = np.linspace(0, 1, steps)
        q = np.rint(
            np.array(rgb(a))[None, :] * (1 - p[:, None]) + np.array(rgb(b))[None, :] * p[:, None]
        ).astype(np.uint8)
        if axis == 0:
            data = np.repeat(q[:, None, :], round(w * CANVAS_SCALE), axis=1)
        else:
            data = np.repeat(q[None, :, :], round(h * CANVAS_SCALE), axis=0)
        self.im.paste(Image.fromarray(data), (round(x * CANVAS_SCALE), round(y * CANVAS_SCALE)))

    def fade(
        self,
        c: str | tuple[int, int, int],
        y: float,
        h: float,
        top: int = 0,
        bottom: int = 255,
    ) -> None:
        alpha = np.uint8(np.round(np.linspace(top, bottom, max(1, round(h * CANVAS_SCALE)))))
        grad = Image.fromarray(np.repeat(alpha[:, None], CANVAS_BASE_W * CANVAS_SCALE, axis=1))
        self.im.paste(Image.new("RGB", grad.size, rgb(c)), _point((0, y)), grad)


def _resolve_multi_focuses(
    artwork: AstraArtworkInput,
    detail_focuses: Sequence[tuple[float, float] | tuple[float, float, float, float]] | None,
    count: int,
    defaults: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    if detail_focuses:
        if len(detail_focuses) == count:
            return [normalize_focus_point(item) for item in detail_focuses]
        if len(detail_focuses) == 1:
            item = detail_focuses[0]
            if len(item) == 4:
                left, top, right, bottom = tuple(float(v) for v in item)
                normalize_focus_point(item)
                cy = (top + bottom) / 2.0
                span = right - left
                return [
                    (left + span * ((i + 0.5) / count), cy)
                    for i in range(count)
                ]
            fx, fy = normalize_focus_point(item)
            if count == 1:
                return [(fx, fy)]
            spread = 0.28
            return [
                (max(0.10, min(0.90, fx + spread * (2 * i / max(1, count - 1) - 1))), fy)
                for i in range(count)
            ]
        raise ValueError(f"Expected 1 or {count} detail focus coordinates, got {len(detail_focuses)}")
    if artwork.focus is not None:
        return _resolve_multi_focuses(artwork, [artwork.focus], count, defaults)
    return list(defaults[:count])


def _render_gallery(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.5)])[0]
    title_str = head or art.title
    bg = ["#283a32", "#d8cbaa", "#f6eedc", "#1d40a0", "#e6c6b1", "#7e342e", "#b09b77", "#d7d1ba", "#7b7160", "#c5b596"][i]
    c = AstraCard(bg)
    if i == 0:
        c.gradient("#344c41", "#18271f")
        c.sig(34, 36, "#eaddbc")
        c.title_block(title_str, 34, 104, sz=54, min_sz=22, c="#f1e4c7", f="serif", maxw=472, maxh=112, leading=0.98, max_lines=2)
        c.frame_image(pil, 48, 233, 444, 250, wood="#8b7549", mat="#e4d7b9", border=6, margin=14)
        c.credit(art, 492, 580, "#eaddbc", align="rt", w=420, year=True)
    elif i == 1:
        c.gradient("#eee0bc", "#aa997a")
        c.sig(34, 36)
        c.dot(270, 111, 4, BLACK)
        c.line([(270, 110), (221, 200), (319, 200), (270, 110)], "#4e3e29")
        c.frame_image(pil, 140, 188, 260, 332, wood="#8a5e3a", mat="#f5e3bb", border=8, margin=12)
        c.credit(art, 36, 588, BLACK, w=460, year=True, sz=20)
    elif i == 2:
        c.sig(44, 38)
        c.art_image(pil, 45, 121, 364, 382, mode="fit")
        c.line([(44, 565), (491, 565)], "#95886b")
        c.credit(art, 44, 588, BLACK, w=450, year=True, sz=24)
    elif i == 3:
        c.gradient("#193289", "#2759c9", axis=1)
        c.sig(34, 37, "#f0e8cf")
        c.frame_image(pil, 50, 168, 440, 340, wood="#0c2159", mat="#f3ecda", border=9, margin=22)
        c.credit(art, 51, 575, "#f5efd9", w=440, year=True)
    elif i == 4:
        c.sig(38, 36)
        c.title_block(title_str, 38, 76, sz=62, min_sz=22, c="#42271d", f="serif", maxw=464, maxh=110, max_lines=2)
        c.frame_image(pil, 185, 195, 258, 387, wood="#674536", mat="#eee0c9", border=7, margin=8)
        c.credit(art, 38, 612, "#42271d", w=464, year=True)
    elif i == 5:
        c.gradient("#4e2525", "#9a4732", axis=1)
        c.rect(151, 72, 349, 555, "#bb775e")
        c.rect(166, 84, 337, 543, "#231c1a")
        c.art_image(pil, 183, 102, 302, 492, focus=focus)
        c.rect(150, 615, 353, 11, "#cca17b")
        c.rotate_type(BRAND, 31, 96, 32, 260, 23, WHITE, "gothic")
        c.credit(art, 183, 631, WHITE, w=302, sz=14)
    elif i == 6:
        c.sig(36, 36)
        c.rect(111, 187, 378, 313, None, "#473923", 3)
        c.rect(120, 197, 359, 293, None, "#dfcda3", 2)
        c.frame_image(pil, 64, 147, 388, 304, wood="#5d4f35", mat="#eadcbb", border=6, margin=8)
        c.credit(art, 64, 553, BLACK, w=410, year=True, sz=20)
    elif i == 7:
        c.sig(35, 33)
        c.rect(34, 114, 472, 421, "#ede5ce")
        c.rect(52, 131, 436, 387, None, "#b49b78")
        c.art_image(pil, 102, 171, 336, 306, mode="fit")
        c.credit(art, 270, 568, BLACK, align="mt", w=444, year=True)
    elif i == 8:
        c.gradient("#bea880", "#493e30", axis=1)
        c.polygon([(0, 0), (205, 0), (111, 675), (0, 675)], "#bfab83")
        c.art_image(pil, 207, 118, 298, 426, mode="fit")
        c.sig(32, 35)
        c.title_block(title_str, 33, 134, sz=44, min_sz=18, c="#21170f", f="italic", maxw=166, maxh=220, max_lines=4)
        c.credit(art, 207, 582, WHITE, w=299, year=True)
    else:
        c.gradient("#ddd1b0", "#9c896f")
        c.rect(0, 566, 540, 109, "#746450")
        c.frame_image(pil, 92, 127, 356, 272, wood="#544431", mat="#e8d9b7", border=5, margin=11)
        c.polygon([(135, 490), (376, 490), (404, 515), (159, 515)], "#513d2e")
        c.polygon([(159, 515), (404, 515), (404, 526), (159, 526)], "#382920")
        c.rect(171, 526, 13, 38, "#382b22")
        c.rect(371, 526, 14, 38, "#382b22")
        c.sig(34, 35)
        c.credit(art, 35, 612, "#fff3db", w=460, year=True)
    return c, {"family": "gallery", "source_count": 1}


def _render_detail(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    title_str = head or art.title
    c = AstraCard([PAPER, "#c0a884", "#eee6ce", "#121c2f", "#ead9c2", "#58463a", "#e5bfcd", "#152f29", "#211b16", "#e9dab4"][i])
    if i == 0:
        focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.55, 0.55)])[0]
        c.sig(34, 32)
        c.title_block(title_str, 34, 72, sz=44, min_sz=18, c=BLACK, f="serif", maxw=472, maxh=68, max_lines=2)
        c.art_image(pil, 33, 145, 474, 359, z=2.48, focus=focus)
        c.art_image(pil, 33, 525, 157, 117, mode="fit")
        c.rect(91, 554, 43, 37, None, "#f0cb72", 2)
        c.line([(134, 572), (226, 572), (226, 530)], "#685843")
        c.credit(art, 248, 563, BLACK, w=256, year=True)
    elif i == 1:
        focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.52, 0.36)])[0]
        c.art_image(pil, 42, 83, 456, 492, mode="fit")
        c.ellipse(165, 154, 330, 330, "#f0e0bb")
        c.art_image(pil, 175, 164, 310, 310, z=2.6, focus=focus, mask="oval")
        c.ellipse(175, 164, 310, 310, None, "#31281e", 3)
        c.line([(422, 442), (502, 531)], "#31281e", 11)
        c.sig(34, 32)
        c.credit(art, 34, 612, BLACK, w=465, year=True)
    elif i == 2:
        focuses = _resolve_multi_focuses(art, detail_focuses, 3, [(0.18, 0.52), (0.50, 0.52), (0.82, 0.52)])
        c.sig(34, 32, BLACK)
        c.title_block(title_str, 34, 78, sz=58, min_sz=22, c="#403222", f="italic", maxw=472, maxh=88, leading=0.95, max_lines=2)
        for idx, pt in enumerate(focuses):
            c.art_image(pil, 34 + idx * 157, 176, 147, 366, mode="cover", focus=pt, z=1.63)
        c.credit(art, 34, 599, BLACK, align="lt", w=472, year=True)
        return c, {"family": "detail", "source_count": 1, "detail_focuses": [list(pt) for pt in focuses]}
    elif i == 3:
        focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.5)])[0]
        c.art_image(pil, 0, 94, 540, 482, z=1.32, focus=focus)
        for j in range(1, 7):
            x = 34 + j * 67
            c.line([(x, 117), (x, 548)], "#e9d69f")
        for j in range(1, 6):
            y = 107 + j * 71
            c.line([(34, y), (504, y)], "#e9d69f")
        c.rect(167, 247, 136, 140, None, "#f9f0d1", 3)
        for j, ch in enumerate("ABCDEFG"):
            c.text(ch, 55 + 67 * j, 103, 12, WHITE, "mono", "mt")
        c.sig(34, 33, WHITE)
        c.credit(art, 33, 603, WHITE, w=475, year=True)
    elif i == 4:
        focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.52, 0.58)])[0]
        c.art_image(pil, 0, 121, 540, 288, z=2.2, focus=focus)
        c.rect(0, 410, 540, 12, "#652c20")
        c.art_image(pil, 310, 462, 192, 145, mode="fit")
        c.sig(34, 36)
        c.text(title_str, 34, 83, 28, BLACK, "italic", "lt", 469)
        c.wrap(art.title, 34, 469, 21, BLACK, "serif", 246, maxlines=4)
        byline = f"{art.artist} · {art.date}" if art.date else art.artist
        c.text(byline, 34, 585, 11, BLACK, "sans", "lt", 246)
    elif i == 5:
        focuses = _resolve_multi_focuses(art, detail_focuses, 3, [(0.48, 0.52), (0.80, 0.62), (0.20, 0.60)])
        c.sig(34, 34, "#f2debc")
        for idx, (x, y, w, h) in enumerate([(33, 115, 474, 185), (33, 310, 204, 256), (247, 310, 260, 256)]):
            c.art_image(pil, x, y, w, h, z=2.3, focus=focuses[idx])
        c.credit(art, 34, 606, "#f6e1ca", w=460, year=True)
    elif i == 6:
        focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.54, 0.42)])[0]
        c.art_image(pil, 0, 0, 540, 675, blur=18, tint="#df9cab", t=0.3, focus=focus)
        c.fade("#35212d", 0, 230, 200, 0)
        c.sig(34, 33, WHITE)
        c.title_block(title_str, 34, 86, sz=56, min_sz=22, c=WHITE, f="italic", maxw=472, maxh=92, max_lines=2)
        c.art_image(pil, 161, 186, 218, 360, z=2.05, focus=focus, mask="oval")
        c.ellipse(161, 186, 218, 360, None, "#fce9ef", 2)
        c.fade("#2e161e", 530, 145, 0, 230)
        c.credit(art, 34, 616, WHITE, w=470, year=True)
    elif i == 7:
        focuses = _resolve_multi_focuses(art, detail_focuses, 2, [(0.74, 0.62), (0.30, 0.52)])
        c.art_image(pil, 0, 0, 277, 512, z=1.5, focus=focuses[0])
        c.art_image(pil, 283, 0, 257, 512, z=2.2, focus=focuses[1], tint="#183228", t=0.13)
        c.rect(0, 0, 278, 68, "#152f29")
        c.sig(31, 25, WHITE)
        c.art_image(pil, 32, 533, 99, 100, mode="fit")
        c.credit(art, 152, 574, WHITE, w=346, sz=17, year=True)
    elif i == 8:
        focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.5)])[0]
        c.art_image(pil, 0, 86, 540, 473, z=1.25, tint="#201811", t=0.10, focus=focus)
        c.line([(74, 490), (153, 417), (195, 319), (277, 258), (356, 255)], "#edc967", 2)
        for px, py in [(74, 490), (195, 319), (356, 255)]:
            c.dot(px, py, 4, "#f9df9f")
        c.sig(34, 27, WHITE)
        c.title_block(title_str, 35, 566, sz=38, min_sz=18, c="#fff0c5", f="italic", maxw=470, maxh=58, max_lines=2)
        c.text(f"{art.artist} · {art.date}" if art.date else art.artist, 36, 632, 12, WHITE, "sans", "lt", maxw=468)
    else:
        focuses = _resolve_multi_focuses(art, detail_focuses, 2, [(0.26, 0.56), (0.72, 0.56)])
        c.sig(34, 32)
        c.art_image(pil, 31, 85, 478, 334, mode="fit")
        for x, pt in zip((54, 286), focuses):
            c.art_image(pil, x, 435, 184, 137, z=2.25, focus=pt)
            c.line([(x + 92, 435), (x + 92, 359), (x + 116, 344)], "#684b26", 1)
            c.dot(x + 116, 344, 4, "#684b26")
        c.credit(art, 34, 612, BLACK, w=455, year=True)
    return c, {"family": "detail", "source_count": 1}


def format_duet_artists_line(
    art_a: AstraArtworkInput,
    art_b: AstraArtworkInput,
    *,
    sep: str = " / ",
    include_dates: bool = False,
) -> str:
    """Format 2-work cover artist credit without repeating identical artist names ('Artist / Artist')."""
    a_name = " ".join((art_a.artist or "").split()).strip() or "Archive"
    b_name = " ".join((art_b.artist or "").split()).strip() or "Archive"
    da = " ".join((art_a.date or "").split()).strip()
    db = " ".join((art_b.date or "").split()).strip()
    if a_name.casefold() == b_name.casefold():
        if da and db and da != db:
            return f"{a_name} · {da} & {db}"
        if da or db:
            return f"{a_name} · {da or db}"
        return a_name
    if include_dates:
        left = f"{a_name} ({da})" if da else a_name
        right = f"{b_name} ({db})" if db else b_name
        return f"{left}{sep}{right}"
    return f"{a_name}{sep}{b_name}"


def _render_duet(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    del detail_focuses
    art_a, art_b = sources[0], sources[1]
    pil_a = load_source_image(art_a.validated_path())
    pil_b = load_source_image(art_b.validated_path())
    fa = art_a.focal_point((0.5, 0.5))
    fb = art_b.focal_point((0.5, 0.5))
    title_str = head or format_duet_artists_line(art_a, art_b)
    artists_line = format_duet_artists_line(art_a, art_b)
    c = AstraCard(["#182126", "#c2ae97", "#eae0c4", "#3e181d", "#e7e5d9", "#172537", "#ded2b6", "#d8bd90", "#282321", "#182e37"][i])
    if i == 0:
        c.art_image(pil_a, 0, 128, 270, 358, z=1.24, focus=fa)
        c.art_image(pil_b, 271, 128, 269, 358, z=1.26, focus=fb)
        c.line([(38, 365), (505, 365)], "#fff0bf")
        c.sig(34, 34, WHITE)
        c.title_block(title_str, 34, 514, sz=50, min_sz=20, c=WHITE, f="serif", maxw=472, maxh=96, max_lines=2)
        c.text(artists_line, 35, 626, 12, WHITE, "sans", "lt", 470)
    elif i == 1:
        c.polygon([(0, 0), (237, 75), (240, 605), (0, 675)], "#dfcfb4")
        c.polygon([(540, 0), (302, 75), (300, 605), (540, 675)], "#a68873")
        c.frame_image(pil_a, 34, 170, 198, 318, wood="#664e3c", mat="#f0dec0", border=5, margin=9)
        c.frame_image(pil_b, 306, 155, 198, 337, wood="#594c3d", mat="#f0dec0", border=6, margin=9)
        c.sig(270, 37, BLACK, "mt")
        c.title_block(title_str, 270, 545, sz=46, min_sz=20, c=BLACK, f="serif", a="mt", maxw=475, maxh=68, max_lines=2)
        c.text(artists_line, 270, 622, 12, BLACK, "sans", "mt", 466)
    elif i == 2:
        c.sig(35, 36)
        c.rect(31, 127, 478, 411, "#f6edda")
        c.gradient("#e5d3a8", "#f9f1dd", 271, 128, 237, 409, axis=1)
        c.art_image(pil_a, 44, 177, 210, 294, mode="fit")
        c.art_image(pil_b, 285, 177, 210, 294, mode="fit")
        c.line([(270, 129), (270, 537)], "#b7a580", 2)
        c.text(art_a.artist, 50, 504, 12, BLACK, "serif", "lt", 200)
        c.text(art_b.artist, 290, 504, 12, BLACK, "serif", "lt", 200)
        c.title_block(title_str, 35, 580, sz=34, min_sz=18, c=BLACK, f="serif", maxw=467, maxh=72, max_lines=2)
    elif i == 3:
        c.art_image(pil_a, 37, 87, 239, 362, focus=fa)
        c.art_image(pil_b, 262, 240, 242, 359, focus=fb)
        c.line([(35, 485), (169, 485), (169, 197), (350, 197), (350, 414), (507, 414)], "#f47664", 2)
        c.sig(35, 30, WHITE)
        c.title_block(title_str, 35, 530, sz=40, min_sz=18, c=WHITE, f="italic", maxw=215, maxh=96, max_lines=3)
        c.text(artists_line, 35, 638, 11, WHITE, "sans", "lt", 478)
    elif i == 4:
        c.sig(35, 35)
        c.title_block(title_str, 35, 76, sz=54, min_sz=20, c="#333929", f="serif", maxw=470, maxh=88, max_lines=2)
        c.art_image(pil_a, 35, 171, 470, 243, z=1.04, focus=fa)
        c.art_image(pil_b, 148, 343, 244, 233, mode="fit")
        c.line([(35, 601), (504, 601)], "#585847")
        c.text(artists_line, 35, 621, 12, BLACK, "sans", "lt", 466)
    elif i == 5:
        c.art_image(pil_a, 0, 0, 540, 303, z=1.1, focus=fa)
        c.art_image(pil_b, 0, 373, 540, 302, z=1.1, focus=fb)
        c.rect(0, 285, 540, 106, "#142230")
        c.title_block(title_str, 270, 306, sz=54, min_sz=20, c=WHITE, f="cond", a="mt", maxw=495, maxh=74, max_lines=2)
        c.rect(0, 0, 130, 57, "#142230")
        c.sig(25, 19, WHITE)
        c.rect(0, 641, 540, 34, "#142230")
        c.text(artists_line, 270, 651, 12, WHITE, "sans", "mt", 470)
    elif i == 6:
        c.sig(35, 35)
        c.art_image(pil_a, 34, 155, 226, 350, mask="arch", focus=fa)
        c.art_image(pil_b, 280, 155, 226, 350, mask="arch", focus=fb)
        c.title_block(title_str, 270, 545, sz=50, min_sz=20, c=BLACK, f="serif", a="mt", maxw=467, maxh=72, max_lines=2)
        c.text(artists_line, 270, 625, 12, BLACK, "sans", "mt", 467)
    elif i == 7:
        c.sig(35, 35)
        c.paper_art(pil_a, art_a.artist, 69, 115, 337, 300, 9, focus=fa)
        c.paper_art(pil_b, art_b.artist, 155, 290, 312, 256, -10, focus=fb)
        c.title_block(title_str.replace(" / ", " and "), 35, 576, sz=48, min_sz=20, c=BLACK, f="italic", maxw=470, maxh=76, max_lines=2)
    elif i == 8:
        c.art_image(pil_a, 33, 133, 231, 267, z=2.7, focus=fa)
        c.art_image(pil_b, 276, 133, 231, 267, z=2.35, focus=fb)
        c.art_image(pil_a, 72, 428, 151, 144, mode="fit")
        c.art_image(pil_b, 315, 428, 153, 144, mode="fit")
        c.sig(35, 35, WHITE)
        c.title_block(title_str, 35, 72, sz=42, min_sz=18, c=WHITE, f="serif", maxw=470, maxh=56, max_lines=2)
        c.text(art_a.artist, 148, 610, 12, WHITE, "sans", "mt", 200)
        c.text(art_b.artist, 392, 610, 12, WHITE, "sans", "mt", 200)
    else:
        c.art_image(pil_a, 0, 0, 540, 330, focus=fa)
        c.art_image(pil_b, 0, 333, 540, 342, z=1.16, focus=fb)
        c.fade("#172a34", 0, 151, 214, 0)
        c.fade("#172a34", 518, 157, 0, 240)
        c.rect(0, 324, 540, 9, "#e7d9af")
        c.sig(34, 34, WHITE)
        c.title_block(title_str, 35, 528, sz=54, min_sz=20, c=WHITE, f="serif", maxw=470, maxh=92, max_lines=2)
        c.text(artists_line, 35, 632, 12, WHITE, "sans", "lt", 474)
    return c, {"family": "duet", "source_count": 2}


def _render_poster(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.5)])[0]
    title_str = (head or art.title).upper()
    bg, fg = [
        ("#2045b3", "#fff1cb"), ("#e9d9c4", "#a82026"), ("#f5d148", "#1c1c11"), ("#16283b", "#cde8ed"),
        ("#ecaa31", "#241b1a"), ("#171917", "#f6efdf"), ("#551e64", "#f5d1f1"), ("#ebeee1", "#26342b"),
        ("#b8be63", "#173b24"), ("#de6925", "#282019"),
    ][i]
    c = AstraCard(bg)
    if i == 0:
        c.ellipse(54, 121, 432, 432, fg)
        c.art_image(pil, 68, 135, 404, 404, mask="oval", z=1.15, focus=focus)
        c.title_block(title_str, 270, 496, sz=110, min_sz=28, c=fg, f="cond", a="mt", maxw=475, maxh=126, max_lines=2)
    elif i == 1:
        c.polygon([(0, 191), (540, 0), (540, 289), (0, 490)], fg)
        c.art_image(pil, 157, 138, 319, 405, mask=[(0.04, 0.14), (1, 0), (1, 0.86), (0, 1)], focus=focus)
        c.title_block(title_str, 35, 412, sz=140, min_sz=28, c=fg, f="bold", maxw=450, maxh=190, max_lines=2)
    elif i == 2:
        c.art_image(pil, 0, 70, 540, 502, z=1.2, focus=focus)
        c.rect(0, 367, 540, 112, bg)
        c.title_block(title_str, 270, 379, sz=88, min_sz=24, c=fg, f="cond", a="mt", maxw=490, maxh=92, max_lines=2)
    elif i == 3:
        c.art_image(pil, 118, 141, 422, 394, z=1.37, focus=focus)
        c.rect(0, 107, 82, 474, "#0e1b29")
        c.rect(386, 402, 154, 174, "#0e1b29")
        c.title_block(title_str, 35, 113, sz=96, min_sz=24, c=fg, f="cond", maxw=460, maxh=190, leading=0.89, max_lines=2)
        c.line([(35, 504), (381, 504)], fg, 2)
    elif i == 4:
        c.art_image(pil, 265, 0, 275, 565, z=1.1, focus=focus)
        c.polygon([(0, 479), (281, 346), (281, 675), (0, 675)], bg)
        c.rotate_type(title_str, 25, 85, 217, 460, 132, fg, "cond")
    elif i == 5:
        c.ellipse(58, 104, 424, 452, fg)
        c.art_image(pil, 165, 91, 210, 464, z=1.05, focus=focus)
        c.title_block(title_str, 270, 497, sz=82, min_sz=24, c=fg, f="cond", a="mt", maxw=490, maxh=124, leading=0.84, max_lines=2)
    elif i == 6:
        c.title_block(title_str, 35, 84, sz=106, min_sz=26, c=fg, f="cond", maxw=470, maxh=130, max_lines=2)
        c.art_image(pil, 40, 223, 465, 355, z=1.25, mask=[(0.16, 0.03), (0.98, 0), (0.82, 0.99), (0.01, 0.88)], focus=focus)
        c.line([(36, 581), (498, 581)], fg, 2)
    elif i == 7:
        c.art_image(pil, 0, 124, 540, 412, z=1.03, focus=focus)
        for j in range(11):
            x = 38 + j * 49
            c.line([(x, 124), (x, 535)], bg, (j % 3) * 2 + 1)
        c.title_block(title_str, 37, 547, sz=72, min_sz=22, c=fg, f="cond", maxw=468, maxh=110, leading=0.82, max_lines=2)
        c.sig(35, 34, fg)
        return c, {"family": "poster", "source_count": 1}
    elif i == 8:
        c.rect(227, 0, 313, 675, "#285338")
        c.art_image(pil, 60, 147, 419, 383, z=1.8, mask=[(0.16, 0), (1, 0.05), (0.86, 1), (0, 0.94)], focus=focus)
        c.title_block(title_str, 29, 469, sz=120, min_sz=28, c="#eedcab", f="cond", maxw=486, maxh=148, max_lines=2)
    else:
        c.ellipse(55, 112, 430, 430, "#c93923")
        c.art_image(pil, 43, 143, 450, 367, z=1.24, mask="oval", focus=focus)
        c.title_block(title_str, 270, 472, sz=130, min_sz=28, c=fg, f="cond", a="mt", maxw=493, maxh=142, max_lines=2)
    c.sig(34, 34, WHITE if i == 8 else fg)
    c.text(f"{art.artist} · {art.date}" if art.date else art.artist, 36, 635, 12, WHITE if i == 8 else fg, "mono", "lt", 470)
    return c, {"family": "poster", "source_count": 1}


def _render_archive(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    title_str = head or art.title
    bg = ["#a68754", "#242422", "#a99a75", "#bf9972", "#625341", "#b06348", "#777f67", "#e4d0ac", "#5b4835", "#b6a078"][i]
    c = AstraCard(bg)
    if i == 0:
        c.rect(43, 105, 177, 49, "#eaddbc", r=9)
        c.rect(43, 135, 454, 469, "#eaddbc", r=10)
        c.sig(55, 118)
        c.art_image(pil, 63, 165, 414, 331, mode="fit")
        c.line([(61, 511), (481, 511)], "#8e7f5b")
        c.credit(art, 62, 534, BLACK, w=411, year=True, sz=20)
    elif i == 1:
        focuses = _resolve_multi_focuses(art, detail_focuses, 4, [(0.24, 0.48), (0.50, 0.44), (0.74, 0.60), (0.40, 0.74)])
        c.sig(31, 32, WHITE)
        for j in range(4):
            x = 35 + (j % 2) * 239
            y = 108 + (j // 2) * 234
            c.rect(x - 4, y - 4, 231, 224, "#ded7be")
            c.art_image(pil, x, y, 222, 192, z=1.6, focus=focuses[j])
            c.text(f"Frame {j + 1}", x, y + 199, 10, BLACK, "mono", "lt", 213)
        c.credit(art, 34, 618, WHITE, w=470, year=True)
    elif i == 2:
        c.rect(38, 69, 469, 551, "#ece0ba", r=4)
        for y in range(98, 598, 32):
            c.dot(58, y, 4, "#6f644e")
            c.line([(77, y + 9), (489, y + 9)], "#c8b78f")
        c.sig(84, 87)
        c.title_block(title_str, 84, 138, sz=48, min_sz=20, c="#3e4840", f="script", maxw=384, maxh=82, max_lines=2)
        c.paper_art(pil, art.artist, 111, 229, 347, 267, 5, "#f4e8cb")
        c.text(art.title, 85, 560, 19, BLACK, "italic", "lt", 386)
        c.text(art.artist, 86, 587, 11, BLACK, "mono", "lt", 386)
    elif i == 3:
        c.rect(42, 81, 456, 505, "#eedeae")
        c.rect(49, 88, 442, 491, None, "#963f33")
        c.art_image(pil, 61, 109, 418, 304, focus=art.focal_point())
        c.ellipse(376, 381, 97, 97, None, "#963f33", 2)
        c.ellipse(384, 389, 81, 81, None, "#963f33")
        stamp_year = "".join(ch for ch in art.date if ch.isdigit())[:4] or "ART"
        c.text(stamp_year, 425, 419, 20, "#7f392d", "mono", "mt")
        c.line([(67, 472), (352, 472)], "#9b7651")
        c.title_block(title_str, 65, 428, sz=34, min_sz=16, c="#4c3526", f="script", maxw=282, maxh=42, max_lines=1)
        c.credit(art, 64, 494, BLACK, w=402, year=True)
        c.sig(43, 612, "#2e2319")
    elif i == 4:
        c.rect(38, 73, 465, 531, "#352d24", r=5)
        c.rect(52, 86, 433, 510, "#dfc9a2")
        c.rect(64, 96, 415, 502, "#f0e1c1")
        c.paper_art(pil, art.artist, 76, 128, 384, 402, -9, "#f7e9cc", fit=True)
        c.sig(77, 559)
    elif i == 5:
        c.rect(53, 87, 434, 493, "#f0d7a3")
        c.art_image(pil, 72, 106, 396, 344, z=1.08, focus=art.focal_point())
        for x in range(53, 488, 12):
            c.dot(x, 477, 2, "#875640")
        for y in (88, 478, 579):
            c.dot(53, y, 11, bg)
            c.dot(487, y, 11, bg)
        c.sig(72, 499)
        c.credit(art, 72, 528, BLACK, w=385, sz=20, year=True)
        c.text(art.institution or "Museum Print Room", 270, 619, 15, "#35261d", "italic", "mt", maxw=430)
    elif i == 6:
        c.rect(65, 94, 410, 467, "#dcdac0", r=1)
        c.art_image(pil, 100, 119, 340, 410, mode="fit")
        c.polygon([(65, 94), (99, 119), (99, 529), (65, 561)], "#c2bc9d")
        c.polygon([(475, 94), (440, 119), (441, 530), (475, 561)], "#b0ad91")
        c.polygon([(65, 561), (100, 529), (440, 529), (475, 561)], "#c5c0a5")
        c.sig(65, 41, "#15261d")
        c.credit(art, 65, 600, "#15261d", w=415, year=True)
    elif i == 7:
        c.rect(53, 78, 435, 505, "#ecddb9", outline="#c9b791", sw=2)
        c.rect(62, 87, 417, 487, None, "#f0e4c5", 2)
        c.art_image(pil, 128, 148, 283, 299, mode="fit")
        c.title_block(title_str, 270, 476, sz=42, min_sz=18, c="#534434", f="italic", a="mt", maxw=382, maxh=64, max_lines=2)
        c.text(art.artist, 270, 548, 12, BLACK, "mono", "mt", 380)
        c.sig(52, 612, "#534434")
    elif i == 8:
        for x, y, w, h, r in [(48, 125, 444, 449, 5), (55, 111, 427, 450, 5), (63, 94, 411, 452, 5)]:
            c.rect(x, y, w, h, "#d7c398", outline="#65543d", sw=1, r=r)
        c.rect(74, 107, 137, 38, "#cdbc90", r=4)
        c.sig(88, 116)
        c.art_image(pil, 93, 165, 352, 293, mode="fit")
        c.credit(art, 92, 481, BLACK, w=356, year=True)
    else:
        c.rect(34, 82, 472, 511, "#7f6745")
        c.rect(41, 90, 458, 495, "#e6d5ae")
        c.art_image(pil, 65, 114, 409, 127, z=1.15, focus=art.focal_point())
        c.rect(75, 281, 390, 242, "#f1e9cd", outline="#ac986b")
        c.sig(96, 301)
        c.wrap(art.title, 97, 355, 32, BLACK, "serif", 338, maxlines=3)
        c.text(art.artist, 98, 473, 14, BLACK, "mono", "lt", 340)
        c.text(art.date or art.institution, 98, 494, 12, BLACK, "mono", "lt", 340)
    return c, {"family": "archive", "source_count": 1}


def _render_cinema(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.48)])[0]
    title_str = head or art.title
    c = AstraCard(["#10151a", "#211b17", "#281d16", "#171b1c", "#2f151e", "#222322", "#110e0d", "#1b2430", "#141f27", "#151916"][i])
    if i == 0:
        c.art_image(pil, 0, 183, 540, 303, focus=focus)
        c.line([(35, 163), (505, 163)], "#536569")
        c.sig(35, 36, WHITE)
        c.title_block(title_str, 270, 526, sz=46, min_sz=20, c=WHITE, f="serif", a="mt", maxw=478, maxh=76, max_lines=2)
        c.text(art.artist, 270, 616, 12, WHITE, "sans", "mt", 460)
    elif i == 1:
        c.art_image(pil, 65, 84, 410, 475, z=1.14, focus=focus)
        for y in range(96, 551, 47):
            c.rect(27, y, 16, 21, "#c8b79e", r=3)
            c.rect(498, y, 16, 21, "#c8b79e", r=3)
        c.sig(64, 33, WHITE)
        c.title_block(title_str, 64, 566, sz=52, min_sz=20, c=WHITE, f="serif", maxw=415, maxh=62, max_lines=2)
        c.text(art.artist, 65, 635, 11, WHITE, "mono", "lt", 415)
    elif i == 2:
        c.art_image(pil, 0, 0, 540, 675, z=1.18, focus=focus)
        c.fade("#20120e", 0, 270, 220, 0)
        c.fade("#20120e", 355, 320, 0, 255)
        c.sig(34, 33, WHITE)
        c.title_block(title_str.upper(), 32, 423, sz=104, min_sz=26, c="#ffcf90", f="cond", maxw=476, maxh=190, leading=0.88, max_lines=2)
        c.text(art.artist, 36, 634, 12, WHITE, "mono", "lt", 470)
    elif i == 3:
        c.sig(35, 33, "#e9e1c9")
        c.art_image(pil, 34, 204, 472, 216, mode="fit")
        c.line([(34, 428), (506, 428)], "#685e49")
        c.title_block(title_str, 270, 496, sz=44, min_sz=20, c="#ede2c9", f="italic", a="mt", maxw=475, maxh=96, max_lines=2)
        c.text(art.artist, 270, 615, 12, "#ede2c9", "mono", "mt", 460)
    elif i == 4:
        c.art_image(pil, 36, 86, 470, 471, z=1.25, focus=focus)
        c.polygon([(0, 0), (210, 0), (116, 270), (103, 458), (41, 578), (0, 675)], "#361820")
        c.polygon([(540, 0), (396, 0), (444, 218), (462, 435), (501, 565), (540, 675)], "#32151e")
        c.line([(202, 35), (116, 269), (103, 454), (61, 546)], "#6c3440", 3)
        c.sig(35, 34, WHITE)
        c.title_block(title_str, 270, 566, sz=50, min_sz=20, c="#f5e0c6", f="serif", a="mt", maxw=465, maxh=62, max_lines=2)
        c.text(art.artist, 270, 634, 12, WHITE, "mono", "mt", 460)
    elif i == 5:
        c.art_image(pil, 0, 116, 540, 350, z=1.02, focus=focus)
        c.art_image(pil, 295, 381, 204, 165, z=2.25, focus=focus)
        c.rect(292, 378, 210, 171, None, "#edb372", 2)
        c.sig(34, 34, WHITE)
        c.title_block(title_str, 34, 554, sz=48, min_sz=20, c="#ffe1ba", f="italic", maxw=470, maxh=68, max_lines=2)
        c.text(art.artist, 35, 631, 12, WHITE, "mono", "lt", 468)
    elif i == 6:
        c.art_image(pil, 153, 56, 234, 491, z=1.34, focus=focus)
        c.fade("#110e0d", 383, 174, 0, 255)
        c.sig(35, 35, WHITE)
        c.title_block(title_str, 270, 535, sz=68, min_sz=22, c="#e8b989", f="serif", a="mt", maxw=472, maxh=88, max_lines=2)
        c.text(art.artist, 270, 630, 12, WHITE, "mono", "mt", 460)
    elif i == 7:
        focuses = _resolve_multi_focuses(art, detail_focuses, 3, [(0.18, 0.55), (0.52, 0.55), (0.76, 0.55)])
        for j, (x, w) in enumerate([(0, 153), (164, 217), (391, 149)]):
            c.art_image(pil, x, 124 + j * 15, w, 350, z=1.35, focus=focuses[j])
        c.sig(34, 35, WHITE)
        c.title_block(title_str, 34, 534, sz=62, min_sz=22, c=WHITE, f="italic", maxw=470, maxh=88, max_lines=2)
        c.text(art.artist, 35, 630, 12, WHITE, "mono", "lt", 468)
    elif i == 8:
        c.art_image(pil, 0, 98, 540, 247, mode="fit")
        c.art_image(pil, 0, 369, 540, 206, z=2.3, focus=focus)
        c.fade("#141f27", 535, 140, 0, 220)
        c.sig(34, 35, WHITE)
        c.title_block(title_str.upper(), 33, 484, sz=80, min_sz=24, c="#f6eddb", f="cond", maxw=474, maxh=142, leading=0.82, max_lines=2)
        c.text(art.artist, 35, 643, 12, WHITE, "mono", "lt", 468)
    else:
        c.sig(35, 36, "#e6d3ab")
        c.art_image(pil, 113, 180, 314, 231, mode="fit")
        c.line([(113, 424), (428, 424)], "#665b46")
        c.title_block(title_str, 270, 480, sz=54, min_sz=20, c="#e6d3ab", f="serif", a="mt", maxw=470, maxh=120, max_lines=2)
        c.text(art.artist, 270, 627, 12, "#e6d3ab", "mono", "mt", 460)
    return c, {"family": "cinema", "source_count": 1}


def _render_colors(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    title_str = head or art.title
    pigments = extract_pigments(pil, 7 if i == 5 else 5)
    palette = [v[0] for v in pigments]
    a, b, d, e, z = palette[:5]
    c = AstraCard(z)
    if i == 0:
        fg = ink(z)
        c.art_image(pil, 34, 97, 472, 306, mode="fit")
        for j, v in enumerate(palette):
            c.rect(34 + j * 94.4, 424, 94.4, 87, v)
            c.text(hexcol(v), 81 + j * 94.4, 460, 12, ink(v), "mono", "mt")
        c.sig(34, 35, fg)
        c.title_block(title_str, 34, 532, sz=50, min_sz=20, c=fg, f="serif", maxw=472, maxh=68, leading=0.95, max_lines=2)
        c.credit(art, 35, 611, fg, align="lt", w=470, year=True)
    elif i == 1:
        c.art_image(pil, 0, 0, 540, 343, focus=art.focal_point())
        for j, v in enumerate(palette):
            c.rect(0, 343 + j * 66.4, 540, 66.4, v)
        c.rect(0, 0, 127, 56, a)
        c.sig(24, 20, ink(a))
        c.title_block(title_str, 35, 540, sz=52, min_sz=20, c=ink(e), f="serif", maxw=470, maxh=82, max_lines=2)
        c.text(art.artist, 35, 641, 12, ink(z), "sans", "lt", 468)
    elif i == 2:
        focuses = _resolve_multi_focuses(art, detail_focuses, 3, [(0.29, 0.45), (0.54, 0.57), (0.67, 0.31)])
        for j, v in enumerate((a, d, z)):
            c.rect(180 * j, 0, 180, 675, v)
            c.art_image(pil, 15 + 180 * j, 171 + j * 55, 149, 259, z=2.6, focus=focuses[j])
            c.text(hexcol(v), 89 + 180 * j, 450 + j * 54, 12, ink(v), "mono", "mt")
        c.sig(34, 34, ink(a))
        c.rect(0, 594, 540, 81, a)
        c.title_block(title_str, 34, 608, sz=38, min_sz=18, c=ink(a), f="serif", maxw=472, maxh=56, max_lines=2)
    elif i == 3:
        tone_words = [hexcol(z), hexcol(a), hexcol(b)]
        for j, v in enumerate([z, a, b]):
            c.rect(0, j * 225, 540, 225, v)
            c.text(tone_words[j], 32, j * 225 + 82, 82, ink(v), "cond", "lt", 478)
        c.art_image(pil, 250, 239, 263, 190, z=1.07, focus=art.focal_point())
        c.sig(34, 29, ink(z))
        c.text(f"{art.artist} · {art.title}", 36, 630, 13, ink(b), "mono", "lt", 468)
    elif i == 4:
        accent = extract_vivid_accent(pil)
        c = AstraCard(accent)
        fg = ink(accent)
        c.sig(34, 33, fg)
        c.art_image(pil, 307, 139, 151, 267, z=1.13, focus=art.focal_point())
        c.title_block(title_str, 35, 196, sz=66, min_sz=22, c=fg, f="serif", maxw=260, maxh=220, max_lines=3)
        c.text(hexcol(accent), 36, 448, 19, fg, "mono", "lt")
        c.credit(art, 35, 615, fg, w=466, year=True)
    elif i == 5:
        c = AstraCard("#eee4d1")
        c.sig(34, 33)
        for j, v in enumerate(palette):
            c.d.pieslice(_box(66, 116, 408, 408), start=-90 + j * 360 / 7, end=-90 + (j + 1) * 360 / 7, fill=v)
        c.ellipse(123, 173, 294, 294, "#e6d9bc")
        c.art_image(pil, 132, 182, 276, 276, mask="oval", focus=art.focal_point())
        c.title_block(title_str, 270, 554, sz=48, min_sz=20, c=BLACK, f="serif", a="mt", maxw=475, maxh=68, max_lines=2)
        c.text(art.artist, 270, 629, 12, BLACK, "mono", "mt", 460)
    elif i == 6:
        c = AstraCard("#ddd5bd")
        c.sig(34, 33)
        c.art_image(pil, 37, 92, 466, 300, mode="fit")
        for j, v in enumerate(palette):
            x = 35 + j * 95
            y = 421 + (j % 2) * 17
            c.rect(x, y, 89, 126, WHITE)
            c.rect(x + 5, y + 5, 79, 86, v)
            c.text(hexcol(v), x + 45, y + 101, 11, BLACK, "mono", "mt")
        c.credit(art, 35, 612, BLACK, w=470, year=True)
    elif i == 7:
        c.art_image(pil, 0, 0, 540, 422, focus=art.focal_point())
        c.rect(0, 422, 540, 253, a)
        count = max(1, sum(v[1] for v in pigments))
        x = 0.0
        for v, n in pigments:
            w = (n / count) * 540
            c.rect(x, 424, w, 118, v)
            c.text(f"{round(n / count * 100)}%", x + w / 2, 465, 15, ink(v), "mono", "mt", maxw=max(w - 8, 6))
            x += w
        c.rect(0, 0, 125, 53, a)
        c.sig(25, 18, ink(a))
        c.title_block(title_str, 35, 566, sz=54, min_sz=20, c=ink(a), f="serif", maxw=470, maxh=64, max_lines=2)
        c.text(art.artist, 35, 638, 12, ink(a), "sans", "lt", 468)
    elif i == 8:
        art_b = sources[1]
        pil_b = load_source_image(art_b.validated_path())
        p_b = extract_pigments(pil_b, 5)
        c.art_image(pil, 0, 0, 270, 675, z=1.17, focus=art.focal_point())
        c.art_image(pil_b, 270, 0, 270, 675, z=1.1, focus=art_b.focal_point())
        for j, (v, _) in enumerate(pigments):
            c.rect(0, 306 + j * 24, 270, 24, v)
        for j, (v, _) in enumerate(p_b):
            c.rect(270, 306 + j * 24, 270, 24, v)
        c.fade("#131614", 0, 130, 211, 0)
        c.fade("#131614", 515, 160, 0, 231)
        c.sig(34, 35, WHITE)
        left_lbl, right_lbl = _split_two_parts(title_str, "WARM", "COOL")
        c.title_block(left_lbl.upper(), 35, 558, sz=62, min_sz=22, c=WHITE, f="cond", maxw=215, maxh=68, max_lines=1)
        c.title_block(right_lbl.upper(), 302, 558, sz=62, min_sz=22, c=WHITE, f="cond", maxw=205, maxh=68, max_lines=1)
        c.text(art.artist, 35, 635, 11, WHITE, "mono", "lt", 220)
        c.text(art_b.artist, 302, 635, 11, WHITE, "mono", "lt", 201)
        return c, {"family": "colors", "source_count": 2, "palette_hex": [hexcol(v) for v in palette]}
    else:
        c = AstraCard(a)
        c.sig(34, 34, ink(a))
        for j in range(20):
            if j in (6, 7, 8, 11, 12, 13):
                continue
            c.rect(34 + (j % 5) * 95, 106 + (j // 5) * 110, 89, 104, palette[(j * 3 + j // 5) % 5])
        c.art_image(pil, 129, 216, 279, 214, z=1.04, focus=art.focal_point())
        c.title_block(title_str, 34, 566, sz=52, min_sz=20, c=ink(a), f="serif", maxw=472, maxh=68, max_lines=2)
        c.text(art.artist, 35, 641, 12, ink(a), "mono", "lt", 468)
    return c, {"family": "colors", "source_count": 1, "palette_hex": [hexcol(v) for v in palette]}


def _render_typography(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.45)])[0]
    title_str = head or art.title
    bg = ["#e9dfbf", "#232720", "#dcceb2", "#e9bf46", "#2b292a", "#ebd0df", "#c5c7ad", "#e6d9c4", "#513529", "#173c48"][i]
    c = AstraCard(bg)
    if i == 0:
        word = title_str.split()[0].upper() if title_str.strip() else "ART"
        c.textured_type(pil, word, 24, 191, 492, 295, 220, "cond", focus=focus)
        c.line([(35, 194), (505, 194)], "#947d51")
        c.line([(35, 467), (505, 467)], "#947d51")
        c.art_image(pil, 41, 496, 194, 114, mode="fit")
        c.sig(35, 35)
        c.credit(art, 261, 545, BLACK, w=244, year=True)
    elif i == 1:
        c.title_block(title_str.upper(), 34, 96, sz=130, min_sz=30, c="#f0e1ab", f="cond", maxw=472, maxh=260, leading=0.84, max_lines=2)
        c.paper_art(pil, art.artist, 223, 363, 271, 205, -7, "#e3d5a4", focus=focus)
        c.sig(35, 33, "#e3d5a4")
        c.text(art.artist, 35, 634, 12, "#e3d5a4", "mono", "lt", 468)
    elif i == 2:
        letter = next((ch.upper() for ch in title_str if ch.isalpha()), "A")
        c.art_image(pil, 217, 93, 286, 473, z=1.13, focus=focus)
        c.text(letter, 38, 148, 420, "#302a24", "gothic", "lt", 446)
        c.art_image(pil, 240, 277, 263, 225, z=1.1, focus=focus, mask=[(0, 0.56), (1, 0), (1, 0.54), (0, 1)])
        c.sig(35, 35)
        c.credit(art, 36, 616, BLACK, w=465, year=True)
    elif i == 3:
        c.art_image(pil, 143, 0, 397, 675, z=1.16, focus=focus)
        c.rotate_type(title_str.upper(), 6, 89, 120, 502, 140, "#292519", "cond")
        c.sig(18, 34)
        c.rect(143, 620, 397, 55, "#201a15")
        c.credit(art, 160, 625, WHITE, w=364)
    elif i == 4:
        c.art_image(pil, 0, 215, 540, 460, z=1.1, focus=focus)
        c.fade("#2b292a", 215, 100, 255, 0)
        c.sig(35, 35, WHITE)
        c.text(title_str.upper(), 270, 135, 60, "#ead4aa", "cond", "mt", 482, track=4.8)
        c.fade("#2b292a", 539, 136, 0, 200)
        c.text(art.artist, 36, 635, 12, WHITE, "mono", "lt", 468)
    elif i == 5:
        top_part, bot_part = _split_two_parts(title_str, "ART", "WORK")
        c.text(top_part.upper(), 36, 98, 155, "#5c2945", "cond", "lt", 470)
        c.art_image(pil, 0, 254, 540, 163, z=2.1, focus=focus)
        c.text(bot_part.upper(), 504, 412, 155, "#5c2945", "cond", "rt", 470)
        c.sig(35, 35, "#5c2945")
        c.credit(art, 35, 619, "#5c2945", w=467, year=True)
    elif i == 6:
        c.art_image(pil, 34, 84, 472, 497, mode="fit")
        c.rect(62, 169, 412, 280, "#e3dcc0")
        c.art_image(pil, 62, 169, 412, 280, tint="#f5eccc", t=0.62, focus=focus)
        c.title_block(title_str.upper(), 270, 195, sz=110, min_sz=26, c="#28352f", f="cond", a="mt", maxw=391, maxh=228, leading=0.84, max_lines=2)
        c.sig(35, 32)
        c.credit(art, 35, 618, BLACK, w=461, year=True)
    elif i == 7:
        c.art_image(pil, 160, 100, 333, 446, z=1.05, focus=focus)
        c.rotate_type(title_str, 38, 102, 108, 450, 42, "#3f3429", "italic")
        c.sig(34, 32)
        c.credit(art, 160, 605, BLACK, w=330, sz=18, year=True)
    elif i == 8:
        letter = next((ch.upper() for ch in title_str if ch.isalpha()), "O")
        c.text(letter, 270, 116, 390, "#eedcc1", "gothic", "mt", 490)
        c.art_image(pil, 172, 236, 196, 171, z=2.03, focus=focus, mask="oval")
        c.sig(35, 35, WHITE)
        c.credit(art, 270, 575, WHITE, align="mt", w=465, year=True, sz=21)
    else:
        top_part, bot_part = _split_two_parts(title_str, "IN", "STEP")
        c.art_image(pil, 40, 226, 461, 313, z=1.1, focus=focus)
        c.line([(34, 233), (508, 233)], "#efc989", 3)
        c.line([(34, 525), (508, 525)], "#efc989", 3)
        c.text(top_part.upper(), 33, 84, 140, "#efc989", "cond", "lt", 472)
        c.text(bot_part.upper(), 502, 443, 136, "#efc989", "cond", "rt", 472)
        c.sig(34, 34, WHITE)
        c.text(art.artist, 35, 635, 12, WHITE, "mono", "lt", 468)
    return c, {"family": "typography", "source_count": 1}


def _render_story(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    art = sources[0]
    pil = load_source_image(art.validated_path())
    focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.50, 0.45)])[0]
    title_str = head or art.title
    c = AstraCard(["#2d1517", "#e5dfc7", "#201d19", "#1c2c36", "#1b3831", "#292331", "#d1b287", "#ded7bf", "#cfbfac", "#1c3828"][i])
    if i == 0:
        c.art_image(pil, 202, 0, 338, 675, z=1.2, focus=focus)
        c.art_image(pil, 165, 112, 72, 462, z=1.65, focus=focus)
        c.rect(0, 0, 167, 675, "#351a1a")
        c.sig(29, 35, WHITE)
        c.title_block(title_str, 29, 250, sz=42, min_sz=18, c="#f4c4a8", f="serif", maxw=130, maxh=260, max_lines=4)
        c.text(art.artist, 30, 630, 11, WHITE, "mono", "lt", 133)
    elif i == 1:
        focuses = _resolve_multi_focuses(art, detail_focuses, 3, [(0.50, 0.48), (0.61, 0.50), (0.73, 0.53)])
        c.sig(34, 34)
        c.title_block(title_str, 34, 78, sz=68, min_sz=24, c="#1c3041", f="serif", maxw=472, maxh=152, max_lines=2)
        c.art_image(pil, 35, 243, 470, 207, z=1.23, focus=focuses[0])
        c.art_image(pil, 173, 458, 332, 104, z=2.1, focus=focuses[1])
        c.art_image(pil, 344, 570, 161, 76, z=3.0, focus=focuses[2])
        c.text(art.artist, 35, 630, 12, "#1c3041", "mono", "lt", 300)
    elif i == 2:
        c.art_image(pil, 0, 0, 540, 436, mode="cover", focus=focus, z=1.24)
        c.fade("#231b18", 0, 135, 210, 0)
        c.polygon([(0, 398), (540, 339), (540, 675), (0, 675)], "#231b18")
        c.line([(35, 465), (503, 415)], "#b67743", 2)
        c.sig(35, 35, WHITE)
        c.title_block(title_str, 34, 473, sz=70, min_sz=24, c="#f8d7a2", f="serif", maxw=472, maxh=142, leading=0.94, max_lines=3)
        byline = f"{art.artist} · {art.date}" if art.date else art.artist
        c.text(byline, 37, 628, 12, WHITE, "mono", "lt", maxw=468)
        return c, {"family": "story", "source_count": 1, "focus": list(focus)}
    elif i == 3:
        c.art_image(pil, 0, 213, 540, 462, z=1.03, focus=focus)
        c.fade("#1c2c36", 213, 90, 255, 0)
        c.sig(34, 35, WHITE)
        c.title_block(title_str, 34, 86, sz=58, min_sz=22, c="#fff0cb", f="serif", maxw=472, maxh=120, max_lines=2)
        c.line([(37, 476), (504, 476)], "#f8d6a2", 2)
        c.fade("#1c2c36", 529, 146, 0, 228)
        c.text(art.artist, 35, 631, 12, WHITE, "mono", "lt", 468)
    elif i == 4:
        c.art_image(pil, 0, 89, 540, 440, z=1.13, focus=focus)
        c.sig(35, 33, WHITE)
        for j, (px, py) in enumerate([(99, 358), (270, 316), (439, 367)]):
            c.dot(px, py, 10, "#f4edcc")
            c.dot(px, py, 3, "#14362d")
            c.text(f"0:0{j + 1}", px, py + 18, 13, "#fff3dc", "mono", "mt")
        c.title_block(title_str, 35, 558, sz=46, min_sz=20, c="#f3edd2", f="italic", maxw=470, maxh=68, max_lines=2)
        c.text(art.artist, 37, 636, 12, WHITE, "mono", "lt", 468)
    elif i == 5:
        c.art_image(pil, 204, 0, 336, 675, z=1.22, focus=focus)
        c.polygon([(0, 0), (289, 0), (209, 675), (0, 675)], "#32283b")
        c.line([(289, 0), (209, 675)], "#a58e9c", 2)
        c.sig(34, 35, WHITE)
        c.title_block(title_str, 35, 187, sz=46, min_sz=18, c="#f0ded6", f="italic", maxw=210, maxh=260, max_lines=4)
        c.text(art.artist, 35, 634, 11, WHITE, "mono", "lt", 190)
    elif i == 6:
        focuses = _resolve_multi_focuses(art, detail_focuses, 2, [(0.30, 0.53), (0.73, 0.53)])
        c.art_image(pil, 0, 125, 255, 428, z=1.25, focus=focuses[0])
        c.art_image(pil, 283, 125, 257, 428, z=1.25, focus=focuses[1])
        c.line([(218, 344), (323, 344)], "#fff0c6", 2)
        c.polygon([(323, 344), (313, 338), (313, 350)], "#fff0c6")
        c.sig(34, 35)
        c.title_block(title_str, 35, 568, sz=48, min_sz=20, c=BLACK, f="serif", maxw=470, maxh=62, max_lines=2)
        c.text(art.artist, 36, 635, 12, BLACK, "mono", "lt", 468)
    elif i == 7:
        c.art_image(pil, 34, 202, 472, 345, focus=focus)
        c.line(
            [(100, 319), (140, 312), (176, 299), (205, 290), (232, 279), (255, 273), (273, 264), (291, 273),
             (322, 274), (349, 313), (371, 341), (419, 351), (443, 354), (497, 382)],
            "#eee3a5",
            3,
        )
        c.sig(34, 35)
        c.title_block(title_str, 34, 78, sz=76, min_sz=24, c="#333e2d", f="serif", maxw=472, maxh=112, max_lines=2)
        c.credit(art, 35, 611, "#333e2d", w=459, year=True)
    elif i == 8:
        c.art_image(pil, 98, 113, 346, 376, z=1.05, focus=focus)
        c.sig(35, 34)
        c.title_block(title_str, 99, 524, sz=34, min_sz=18, c="#493b31", f="italic", maxw=346, maxh=64, max_lines=2)
        c.text(art.artist, 100, 598, 12, "#493b31", "mono", "lt", 346)
    else:
        c.art_image(pil, 55, 196, 433, 401, mode="fit")
        c.art_image(pil, 0, 74, 268, 350, z=2.02, focus=focus, mask=[(0, 0), (1, 0), (1, 0.86), (0, 1)])
        c.sig(35, 30, WHITE)
        c.title_block(title_str, 34, 496, sz=56, min_sz=22, c="#ebefce", f="serif", maxw=472, maxh=126, max_lines=2)
        c.text(art.artist, 36, 635, 12, WHITE, "mono", "lt", 468)
    return c, {"family": "story", "source_count": 1}


def _render_experimental(i: int, sources: Sequence[AstraArtworkInput], head: str, detail_focuses) -> tuple[AstraCard, dict]:
    if i == 3:
        if len(sources) != 3:
            raise ValueError("Design 'astra-094' (Three Rooms) requires exactly 3 artwork sources")
        pils = [load_source_image(item.validated_path()) for item in sources]
        focus_points = [
            normalize_focus_point(detail_focuses[idx]) if detail_focuses and idx < len(detail_focuses) else item.focal_point()
            for idx, item in enumerate(sources)
        ]
        card = AstraCard("#d4c2a3")
        for idx, wall in enumerate(("#a44a36", "#d4b05c", "#537366")):
            card.rect(idx * 180, 151, 180, 397, wall)
        card.art_image(pils[0], 14, 204, 152, 304, mode="cover", focus=focus_points[0], mask="arch")
        card.art_image(pils[1], 197, 245, 146, 244, mode="cover", focus=focus_points[1], z=1.15)
        card.art_image(pils[2], 370, 270, 158, 158, mode="cover", focus=focus_points[2], mask="oval", z=1.03)
        card.sig(35, 34, BLACK)
        card.title_block(head or "Three rooms", 35, 74, sz=64, min_sz=22, c="#372a21", f="serif", maxw=470, maxh=68, leading=0.92, max_lines=2)
        for idx, item in enumerate(sources):
            card.text(item.artist, 12 + idx * 180, 579, 11, BLACK, "mono", "lt", maxw=158)
            card.text(item.title, 12 + idx * 180, 597, 9.5, "#3d332a", "sans", "lt", maxw=158)
        return card, {"family": "experimental", "source_count": 3}

    art = sources[0]
    pil = load_source_image(art.validated_path())
    focus = _resolve_multi_focuses(art, detail_focuses, 1, [(0.5, 0.48)])[0]
    title_str = head or art.title
    c = AstraCard(["#301e20", "#172c27", "#b9c6b5", "#d4c2a3", "#4d262a", "#a1ad8c", "#101b25", "#e2ca75", "#e1c2d1", "#341c21"][i])
    if i == 0:
        c.rect(90, 94, 360, 441, "#9c6855")
        c.art_image(pil, 106, 111, 328, 408, z=1.17, focus=focus)
        c.polygon([(0, 0), (75, 74), (111, 115), (112, 521), (69, 573), (0, 675)], "#593631")
        c.art_image(pil, 25, 126, 169, 365, z=1.68, focus=focus, mask=[(0, 0), (1, 0.14), (1, 0.83), (0, 1)])
        c.line([(194, 178), (194, 427)], "#f2c089", 2)
        c.sig(34, 33, WHITE)
        c.title_block(title_str, 35, 568, sz=50, min_sz=20, c=WHITE, f="serif", maxw=470, maxh=64, max_lines=2)
        c.text(art.artist, 36, 638, 12, WHITE, "mono", "lt", 468)
    elif i == 1:
        focuses = _resolve_multi_focuses(art, detail_focuses, 2, [(0.54, 0.30), (0.33, 0.63)])
        c.ellipse(65, 94, 413, 428, None, "#a1a881")
        c.art_image(pil, 30, 200, 291, 290, mode="fit")
        c.art_image(pil, 292, 86, 163, 163, z=2.35, focus=focuses[0], mask="oval")
        c.art_image(pil, 334, 402, 160, 160, z=2.46, focus=focuses[1], mask="oval")
        c.ellipse(292, 86, 163, 163, None, "#ddd4a4", 2)
        c.ellipse(334, 402, 160, 160, None, "#ddd4a4", 2)
        c.sig(34, 32, WHITE)
        c.credit(art, 35, 618, WHITE, w=457, sz=21, year=True)
    elif i == 2:
        for j, (h, y) in enumerate([(400, 99), (402, 138), (365, 156), (402, 111), (378, 184), (355, 134)]):
            x = 30 + 81 * j
            c.art_image(pil, x, y, 76, h, z=1.2, focus=(0.11 + 0.15 * j, 0.54))
        c.sig(33, 31)
        c.title_block(title_str, 34, 558, sz=50, min_sz=20, c=BLACK, f="serif", maxw=472, maxh=68, max_lines=2)
        c.text(art.artist, 35, 636, 12, BLACK, "mono", "lt", 468)
    elif i == 4:
        c.art_image(pil, 0, 88, 540, 338, focus=focus)
        c.art_image(pil, 0, 302, 540, 282, focus=focus, z=1.14, tint="#bc5e2e", t=0.25, mirror=True)
        for j, (y, h) in enumerate([(302, 10), (337, 19), (370, 5), (414, 58), (515, 8)]):
            c.art_image(pil, 0, y, 540, h, focus=(focus[0], min(0.95, focus[1] + 0.03 * j)), z=1.7, tint="#ae5849", t=0.4)
        c.sig(34, 34, WHITE)
        c.title_block(title_str, 35, 574, sz=56, min_sz=20, c="#ffe3c3", f="italic", maxw=470, maxh=66, max_lines=2)
        c.text(art.artist, 36, 646, 11, WHITE, "mono", "lt", 468)
    elif i == 5:
        c.art_image(pil, 33, 121, 267, 411, mask=[(0, 0), (1, 0.2), (1, 0.83), (0, 1)], z=1.15, focus=focus)
        c.art_image(pil, 300, 121, 207, 411, mask=[(0, 0.2), (1, 0), (1, 1), (0, 0.83)], mirror=True, tint="#374b36", t=0.23, focus=focus)
        c.line([(300, 202), (300, 463)], "#ece9c6", 2)
        c.sig(34, 34)
        c.title_block(title_str, 34, 568, sz=52, min_sz=20, c="#253222", f="serif", maxw=472, maxh=64, max_lines=2)
        c.text(art.artist, 35, 638, 12, "#253222", "mono", "lt", 468)
    elif i == 6:
        c.ellipse(68, 118, 404, 404, None, "#59666e")
        c.art_image(pil, 137, 188, 266, 266, z=1.27, focus=focus, mask="oval")
        c.ellipse(137, 188, 266, 266, None, "#b2a881")
        c.dot(270, 117, 4, "#d4b887")
        c.sig(35, 35, WHITE)
        c.title_block(title_str, 270, 540, sz=44, min_sz=20, c="#ebdcbb", f="serif", a="mt", maxw=470, maxh=76, max_lines=2)
        c.text(art.artist, 270, 630, 12, WHITE, "mono", "mt", 460)
    elif i == 7:
        brush_mask = [
            (0.05, 0.19), (0.86, 0), (0.76, 0.18), (1, 0.18), (0.90, 0.32), (0.98, 0.41),
            (0.78, 0.64), (0.92, 0.66), (0.59, 0.87), (0.12, 1), (0.22, 0.76), (0, 0.84),
            (0.06, 0.65), (0.02, 0.5), (0.23, 0.29), (0, 0.36),
        ]
        c.art_image(pil, 26, 110, 489, 424, z=1.22, focus=focus, mask=brush_mask)
        c.sig(35, 34)
        c.title_block(title_str, 35, 536, sz=48, min_sz=20, c=BLACK, f="italic", maxw=470, maxh=92, max_lines=2)
        c.text(art.artist, 36, 640, 12, BLACK, "mono", "lt", 468)
    elif i == 8:
        focuses = _resolve_multi_focuses(art, detail_focuses, 3, [(0.43, 0.28), (0.68, 0.49), (0.52, 0.80)])
        shapes = [
            (32, 98, 299, 287, [(0.16, 0), (1, 0.20), (0.74, 1), (0, 0.77)]),
            (269, 184, 244, 270, [(0.27, 0), (1, 0.29), (0.86, 1), (0, 0.71)]),
            (67, 371, 318, 198, [(0, 0.29), (0.58, 0), (1, 0.54), (0.47, 1)]),
        ]
        for j, (x, y, w, h, mask) in enumerate(shapes):
            c.art_image(pil, x, y, w, h, z=2.15, focus=focuses[j], mask=mask)
        c.art_image(pil, 395, 452, 105, 113, mode="fit")
        c.sig(35, 34)
        c.title_block(title_str, 35, 580, sz=48, min_sz=20, c="#462839", f="serif", maxw=470, maxh=58, max_lines=2)
        c.text(art.artist, 36, 643, 12, BLACK, "mono", "lt", 468)
    else:
        focuses = _resolve_multi_focuses(art, detail_focuses, 4, [(0.18, 0.20), (0.75, 0.20), (0.18, 0.78), (0.75, 0.78)])
        coords = [(x, y) for y in (122, 338) for x in (35, 271)]
        for j, (x, y) in enumerate(coords):
            c.art_image(pil, x, y, 233, 211, z=2.22, focus=focuses[j], tint="#541f30", t=(j % 3) * 0.10)
        c.line([(35, 122), (504, 549)], "#e0b59c", 2)
        c.line([(505, 122), (35, 549)], "#e0b59c", 2)
        c.ellipse(180, 246, 180, 180, "#e0b59c")
        c.art_image(pil, 185, 251, 170, 170, mask="oval", focus=focus)
        c.sig(35, 33, WHITE)
        c.title_block(title_str, 35, 568, sz=52, min_sz=20, c="#f0d8bf", f="serif", maxw=470, maxh=64, max_lines=2)
        c.text(art.artist, 36, 637, 12, WHITE, "mono", "lt", 468)
    return c, {"family": "experimental", "source_count": 1}


_FAMILY_RENDERERS = (
    _render_gallery,
    _render_detail,
    _render_duet,
    _render_poster,
    _render_archive,
    _render_cinema,
    _render_colors,
    _render_typography,
    _render_story,
    _render_experimental,
)


def render_astra_image(
    design_id: str,
    sources: Sequence[AstraArtworkInput],
    editorial_title: str,
    *,
    detail_focuses: Sequence[tuple[float, float] | tuple[float, float, float, float]] | None = None,
) -> tuple[Image.Image, dict]:
    """Render any ready Astra or Neon design to an in-memory 1080x1350 PIL Image + metadata."""
    entry = get_design(design_id)
    if not entry.is_usable:
        raise ValueError(
            f"Design '{design_id}' ({entry.name}) is registered in the catalog "
            "but not yet ported as a usable template"
        )
    if len(sources) != entry.required_sources:
        raise ValueError(
            f"Design '{design_id}' ({entry.name}) requires {entry.required_sources} "
            f"artwork source(s), received {len(sources)}"
        )

    if entry.collection == "neon":
        from src.neon_renderer import render_neon_image

        return render_neon_image(entry, sources, editorial_title, detail_focuses=detail_focuses)

    if entry.collection != "astra" or not 1 <= entry.number <= 100:
        raise ValueError(f"Unsupported Astra design id: {design_id}")

    group_idx = (entry.number - 1) // 10
    variant_idx = (entry.number - 1) % 10
    card, extra = _FAMILY_RENDERERS[group_idx](variant_idx, sources, editorial_title, detail_focuses)
    if group_idx in (0, 4, 7, 9):
        card.grain(771 + entry.number, 1.7 if group_idx != 4 else 2.8)

    metadata = {
        "design_id": entry.id,
        "design_name": entry.name,
        "collection": entry.collection,
        "family": entry.family,
        "family_name": entry.family_name,
        "width": card.im.width,
        "height": card.im.height,
        **extra,
    }
    return card.im, metadata


def render_astra_design(
    design_id: str,
    sources: Sequence[AstraArtworkInput],
    editorial_title: str,
    output_path: str | Path,
    *,
    detail_focuses: Sequence[tuple[float, float] | tuple[float, float, float, float]] | None = None,
) -> dict:
    """Render a deterministic 1080x1350 JPEG for any ready Astra or Neon template and save to disk."""
    image, metadata = render_astra_image(
        design_id,
        sources,
        editorial_title,
        detail_focuses=detail_focuses,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(".render.tmp.jpg")
    try:
        image.save(tmp_path, format="JPEG", quality=92, optimize=True, subsampling=0)
        tmp_path.replace(destination)
    finally:
        tmp_path.unlink(missing_ok=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        **metadata,
        "output_path": str(destination),
        "sha256": digest,
    }
