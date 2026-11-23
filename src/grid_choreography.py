"""Grid choreography: 3-column Reels spine, luminance checkerboard, big-type balance, signature days & tempo profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

TempoProfile = Literal["2_per_day", "4_per_day", "6_per_day"]

# Prime publishing windows in Europe/Istanbul overlapping EU (CET) + US East (EST)
TEMPO_PROFILES: dict[str, dict] = {
    "2_per_day": {
        "label": "Sakin Tempo (2/gün · 1 Kaydırmalı + 1 Reels)",
        "carousels_per_day": 1,
        "reels_per_day": 1,
        "slots_ist": [(18, 30), (21, 0)],
        "audience_note": "TR + CET + EST akşam çakışması; küçük hesaplarda kendi erişimini bölme riski en düşük.",
    },
    "4_per_day": {
        "label": "Dengeli Tempo (4/gün · 3 Kaydırmalı + 1 Reels)",
        "carousels_per_day": 3,
        "reels_per_day": 1,
        "slots_ist": [(16, 0), (18, 30), (20, 30), (22, 30)],
        "audience_note": "Avrupa öğleden sonra + ABD Doğu sabah/öğlen çakışma penceresi.",
    },
    "6_per_day": {
        "label": "Yüksek Hız (6/gün · 4 Kaydırmalı + 2 Reels · Orta Sütun Reels Omurgası)",
        "carousels_per_day": 4,
        "reels_per_day": 2,
        "slots_ist": [(9, 30), (12, 0), (14, 30), (17, 30), (20, 0), (22, 30)],
        "audience_note": "Her gün tam 2 Instagram satırı (3+3). İlk 2 hafta CSV karnesiyle 4/gün ile kıyaslanması önerilir.",
    },
}

# Weekly Signature Days (weekday 0=Monday .. 6=Sunday)
SIGNATURE_DAYS: dict[int, dict[str, str]] = {
    0: {"slug": "colors", "label": "Pazartesi · Pigment & Renk", "design_hint": "astra-061"},
    1: {"slug": "detail", "label": "Salı · Yakın Bakış & Büyüteç", "design_hint": "neon-009"},
    2: {"slug": "duet", "label": "Çarşamba · 3 Oda & Düet", "design_hint": "astra-094"},
    3: {"slug": "archive", "label": "Perşembe · Analog Arşiv & Kitap", "design_hint": "neon-103"},
    4: {"slug": "story", "label": "Cuma · Quiz & Tahmin", "design_hint": "neon-151"},
    5: {"slug": "cinema", "label": "Cumartesi · Sinematik Işık", "design_hint": "astra-051"},
    6: {"slug": "gallery", "label": "Pazar · Sessiz Galeri", "design_hint": "astra-001"},
}

BIG_TYPE_FAMILIES = frozenset({"poster", "typography"})
QUIET_FAMILIES = frozenset({"gallery", "archive"})


def compute_cover_tone(image_path: str | Path) -> dict:
    """Compute average luminance (0..1) and warmth (-1..1) of a rendered cover for grid rhythm & heatmap."""
    with Image.open(image_path) as im:
        arr = np.asarray(im.convert("RGB").resize((64, 80), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    luma = float((0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]).mean())
    warmth = float((arr[:, :, 0] - arr[:, :, 2]).mean())
    tone_band = "dark" if luma < 0.36 else ("light" if luma > 0.64 else "mid")
    temp_band = "warm" if warmth > 0.05 else ("cool" if warmth < -0.04 else "neutral")
    return {
        "luminance": round(luma, 3),
        "warmth": round(warmth, 3),
        "tone_band": tone_band,
        "temp_band": temp_band,
    }


def analyze_grid_rhythm(cells: list[dict]) -> dict:
    """Audit 3-column grid rows for:
    - #22 Light/dark clumping (3 identical dark or light cells in one row)
    - #26 Big-typography overcrowding (>1 poster/typography in one row)
    - #21 Center-column Reels alignment
    """
    rows = [cells[i : i + 3] for i in range(0, len(cells), 3)]
    clump_rows = []
    type_crowded_rows = []
    center_reels = 0
    total_reels = 0

    for row_idx, row in enumerate(rows, 1):
        tones = [c.get("tone_band", "mid") for c in row]
        if len(row) == 3 and tones[0] in ("dark", "light") and len(set(tones)) == 1:
            clump_rows.append({"row": row_idx, "tone": tones[0]})
        big_type_cnt = sum(1 for c in row if c.get("design_family") in BIG_TYPE_FAMILIES)
        if big_type_cnt > 1:
            type_crowded_rows.append({"row": row_idx, "big_type_count": big_type_cnt})
        for col_idx, cell in enumerate(row):
            if cell.get("post_format") == "reel":
                total_reels += 1
                if col_idx == 1:
                    center_reels += 1

    return {
        "row_count": len(rows),
        "tone_clump_rows": clump_rows,
        "type_crowded_rows": type_crowded_rows,
        "center_reel_ratio": round(center_reels / max(1, total_reels), 3),
        "balanced": len(clump_rows) == 0 and len(type_crowded_rows) == 0,
    }


def extract_primary_secondary_rgb(image_path: str | Path) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Extract (primary_rgb, secondary_rgb) from a rendered cover for Color-Bridge grid sequencing."""
    with Image.open(image_path) as im:
        small = im.convert("RGB").resize((48, 60), Image.Resampling.BILINEAR)
        quant = small.quantize(colors=4, method=Image.Quantize.MEDIANCUT)
        pal = quant.getpalette() or []
        counts = sorted(quant.getcolors() or [], reverse=True)
    colors: list[tuple[int, int, int]] = []
    for _, idx in counts:
        base = idx * 3
        if base + 2 < len(pal):
            colors.append((pal[base], pal[base + 1], pal[base + 2]))
    if not colors:
        return (128, 128, 128), (128, 128, 128)
    primary = colors[0]
    secondary = colors[1] if len(colors) > 1 else primary
    return primary, secondary


def _color_distance(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """Perceptually weighted RGB distance (redmean approximation)."""
    rmean = (c1[0] + c2[0]) / 2.0
    dr, dg, db = c1[0] - c2[0], c1[1] - c2[1], c1[2] - c2[2]
    return float(((2.0 + rmean / 256.0) * dr * dr + 4.0 * dg * dg + (2.0 + (255.0 - rmean) / 256.0) * db * db) ** 0.5)


def sort_schedule_by_color_bridge(
    post_entries: list[dict],
    *,
    preserve_format_slots: bool = True,
) -> list[str]:
    """Order post IDs so each post's secondary pigment bridges smoothly into the next post's primary pigment.

    When preserve_format_slots=True, carousel slots stay carousels and reel slots stay reels
    so the 6/day 3-column Reels spine ([C, R, C, C, R, C]) is preserved intact.
    """
    if len(post_entries) <= 1:
        return [p["id"] for p in post_entries]

    slot_formats = [p["post_format"] for p in post_entries]
    pool_by_fmt: dict[str, list[dict]] = {"carousel": [], "reel": []}
    for entry in post_entries:
        pool_by_fmt.setdefault(entry["post_format"], []).append(entry)

    if not preserve_format_slots:
        remaining = list(post_entries)
        ordered = [remaining.pop(0)]
        while remaining:
            prev_sec = ordered[-1]["secondary_rgb"]
            best_i = min(range(len(remaining)), key=lambda i: _color_distance(prev_sec, remaining[i]["primary_rgb"]))
            ordered.append(remaining.pop(best_i))
        return [p["id"] for p in ordered]

    ordered_ids: list[str] = []
    prev_sec: tuple[int, int, int] | None = None
    for fmt in slot_formats:
        bucket = pool_by_fmt.get(fmt, [])
        if not bucket:
            continue
        if prev_sec is None:
            chosen = bucket.pop(0)
        else:
            best_i = min(range(len(bucket)), key=lambda i: _color_distance(prev_sec, bucket[i]["primary_rgb"]))
            chosen = bucket.pop(best_i)
        ordered_ids.append(chosen["id"])
        prev_sec = chosen["secondary_rgb"]
    return ordered_ids

