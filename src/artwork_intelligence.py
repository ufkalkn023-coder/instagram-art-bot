"""Artwork visual intelligence: auto-focus, border trimming, monumental scale check, resolution & render QA."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

MONUMENTAL_THRESHOLD_CM = 180.0


def detect_smart_focus(image: Image.Image) -> tuple[float, float]:
    """Estimate the most visually salient focal point (fx, fy) in [0.15, 0.85]
    using local contrast/edge energy + upper-middle portrait composition prior.
    """
    small = image.convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
    gray = np.asarray(small.convert("L"), dtype=np.float32) / 255.0
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    edge_energy = gx + gy

    # Subtle luminance & warm-tone saliency (faces, highlights, candles)
    rgb = np.asarray(small, dtype=np.float32) / 255.0
    warm = np.clip(rgb[:, :, 0] - rgb[:, :, 2], 0.0, 1.0)
    saliency = edge_energy * 0.65 + warm * 0.20 + gray * 0.15

    # Gentle portrait/museum prior favoring center-x and upper-mid y (0.42)
    yy, xx = np.mgrid[0:96, 0:96].astype(np.float32) / 95.0
    prior = np.exp(-(((xx - 0.5) ** 2) / 0.14 + ((yy - 0.43) ** 2) / 0.16))
    weighted = saliency * (0.45 + 0.55 * prior)

    total = float(weighted.sum())
    if total <= 1e-6:
        return (0.5, 0.45)
    fx = float((weighted * xx).sum() / total)
    fy = float((weighted * yy).sum() / total)
    return (round(max(0.18, min(0.82, fx)), 3), round(max(0.18, min(0.80, fy)), 3))


def trim_museum_scan_borders(image: Image.Image, *, max_trim_ratio: float = 0.08) -> tuple[Image.Image, dict]:
    """Detect and safely trim uniform dark/gray museum scanner borders or calibration strips."""
    rgb = image.convert("RGB")
    w, h = rgb.size
    if min(w, h) < 120:
        return rgb, {"trimmed": False, "crop_box": (0, 0, w, h)}

    arr = np.asarray(rgb, dtype=np.float32)
    max_x = int(w * max_trim_ratio)
    max_y = int(h * max_trim_ratio)

    def _is_border_line(line_px: np.ndarray) -> bool:
        std = float(line_px.std())
        mean = float(line_px.mean())
        # Uniform very dark (<28), uniform flat gray/white (>238 or std < 8)
        return std < 8.5 and (mean < 32.0 or mean > 236.0)

    left = 0
    while left < max_x and _is_border_line(arr[:, left, :]):
        left += 1
    right = w
    while (w - right) < max_trim_ratio * w and right > left + 60 and _is_border_line(arr[:, right - 1, :]):
        right -= 1
    top = 0
    while top < max_y and _is_border_line(arr[top, :, :]):
        top += 1
    bottom = h
    while (h - bottom) < max_trim_ratio * h and bottom > top + 60 and _is_border_line(arr[bottom - 1, :, :]):
        bottom -= 1

    trimmed = (left, top, right, bottom) != (0, 0, w, h)
    if not trimmed:
        return rgb, {"trimmed": False, "crop_box": (0, 0, w, h)}
    return rgb.crop((left, top, right, bottom)), {"trimmed": True, "crop_box": (left, top, right, bottom)}


def validate_artwork_resolution(
    image_path: str | Path,
    *,
    min_short_edge: int = 1200,
    min_aspect: float = 0.32,
    max_aspect: float = 3.2,
) -> dict:
    """Preflight check for minimum artwork resolution and safe aspect ratio."""
    path = Path(image_path)
    if not path.is_file():
        return {"valid": False, "warnings": ["missing_file"], "width": 0, "height": 0}
    with Image.open(path) as im:
        w, h = im.size
    short_edge = min(w, h)
    aspect = w / max(1, h)
    warnings = []
    if short_edge < min_short_edge:
        warnings.append(f"low_resolution:{w}x{h}<{min_short_edge}px")
    if aspect < min_aspect or aspect > max_aspect:
        warnings.append(f"extreme_aspect:{aspect:.2f}")
    return {
        "valid": short_edge >= 300,
        "hd_ready": len(warnings) == 0,
        "width": w,
        "height": h,
        "aspect_ratio": round(aspect, 3),
        "warnings": warnings,
    }


def parse_dimensions_cm(dimensions_text: str | None) -> tuple[float, float] | None:
    """Parse (height_cm, width_cm) from museum dimension strings like '210 × 340 cm' or '82 × 114 in.'."""
    if not dimensions_text:
        return None
    text = str(dimensions_text).replace("×", "x").replace("X", "x")
    cm_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*cm", text, re.IGNORECASE)
    if cm_match:
        return (float(cm_match.group(1)), float(cm_match.group(2)))
    m_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*m\b", text, re.IGNORECASE)
    if m_match:
        return (round(float(m_match.group(1)) * 100.0, 1), round(float(m_match.group(2)) * 100.0, 1))
    in_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*(?:in\.?|inches)", text, re.IGNORECASE)
    if in_match:
        return (round(float(in_match.group(1)) * 2.54, 1), round(float(in_match.group(2)) * 2.54, 1))
    return None


def should_show_monumental_scale_slide(dimensions_text: str | None) -> tuple[bool, tuple[float, float] | None]:
    """True ONLY for very large / monumental works (longest side >= 180 cm)."""
    dims = parse_dimensions_cm(dimensions_text)
    if dims is None:
        return False, None
    return max(dims) >= MONUMENTAL_THRESHOLD_CM, dims


def audit_rendered_slide(image_path: str | Path, *, expected_size: tuple[int, int] = (1080, 1350)) -> dict:
    """Automated post-render QA checking dimensions, blank/clipped regions, and luminance contrast."""
    path = Path(image_path)
    if not path.is_file():
        return {"passed": False, "warnings": ["file_missing"], "mean_luminance": 0.0}
    with Image.open(path) as im:
        w, h = im.size
        arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    luma = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    mean_luma = float(luma.mean())
    std_luma = float(luma.std())

    warnings = []
    if (w, h) != expected_size:
        warnings.append(f"unexpected_size:{w}x{h}")
    if std_luma < 0.04:
        warnings.append("flat_or_blank_render")
    # Check extreme edge clipping (top/bottom 12px should not be saturated text/art collision noise)
    top_edge_std = float(luma[:8, :].std())
    if top_edge_std > 0.36:
        warnings.append("top_edge_crowded")
    return {
        "passed": len(warnings) == 0,
        "warnings": warnings,
        "width": w,
        "height": h,
        "mean_luminance": round(mean_luma, 3),
        "contrast_std": round(std_luma, 3),
    }


def draw_human_silhouette(draw: ImageDraw.ImageDraw, cx: int, floor_y: int, height_px: int, fill=(44, 42, 38)) -> None:
    """Draw a clean minimalist 170 cm museum visitor silhouette for monumental scale slides."""
    head_r = max(6, int(height_px * 0.065))
    head_cy = floor_y - height_px + head_r
    draw.ellipse((cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r), fill=fill)
    shoulder_y = head_cy + head_r + max(3, int(height_px * 0.02))
    hip_y = floor_y - int(height_px * 0.46)
    sw = max(10, int(height_px * 0.14))
    hw = max(8, int(height_px * 0.10))
    draw.polygon(
        [(cx - sw, shoulder_y), (cx + sw, shoulder_y), (cx + hw, hip_y), (cx - hw, hip_y)],
        fill=fill,
    )
    leg_w = max(4, int(hw * 0.44))
    draw.rectangle((cx - hw + 1, hip_y, cx - hw + 1 + leg_w, floor_y), fill=fill)
    draw.rectangle((cx + hw - 1 - leg_w, hip_y, cx + hw - 1, floor_y), fill=fill)
