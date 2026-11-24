"""Artwork-aware and neighbor-aware profile design selector across the 280-design catalog."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

from src.astra_catalog import list_usable_designs

# Visual weight classification of the 10 layout families
HEAVY_TYPO_FAMILIES = frozenset({"typography", "poster"})
DARK_CINEMA_FAMILIES = frozenset({"cinema", "experimental"})
BREATHING_FAMILIES = frozenset({"gallery", "archive", "detail", "colors"})
REEL_PREFERRED_FAMILIES = frozenset({"cinema", "detail", "gallery", "colors", "experimental", "story", "typography"})

# Families that frame horizontal/wide paintings cleanly without harsh vertical cropping
LANDSCAPE_FRIENDLY_FAMILIES = frozenset({"gallery", "archive", "colors", "story", "detail"})
PORTRAIT_FRIENDLY_FAMILIES = frozenset({"cinema", "typography", "poster", "gallery", "experimental", "story"})
LONG_TITLE_SAFE_FAMILIES = frozenset({"gallery", "archive", "colors", "story", "detail", "duet"})


def audit_design_catalog_eligibility() -> dict:
    """Analyze all 280 usable designs by source count, family, and format suitability."""
    usable = list_usable_designs()
    by_sources = Counter(d.required_sources for d in usable)
    by_family = Counter(d.family for d in usable)
    single_source = [d for d in usable if d.required_sources == 1]
    reel_eligible = [d for d in single_source if d.family in REEL_PREFERRED_FAMILIES]
    return {
        "total_usable": len(usable),
        "by_required_sources": dict(sorted(by_sources.items())),
        "by_family": dict(sorted(by_family.items(), key=lambda kv: (-kv[1], kv[0]))),
        "carousel_1_source_eligible": len(single_source),
        "carousel_2_source_eligible": by_sources.get(2, 0),
        "carousel_3_source_eligible": by_sources.get(3, 0),
        "carousel_4_source_eligible": by_sources.get(4, 0),
        "reel_eligible": len(reel_eligible),
    }


def extract_artwork_visual_profile(image_path: str | Path, title: str = "") -> dict:
    """Inspect an artwork's orientation, luminance, detail density, and title length."""
    p = Path(image_path)
    with Image.open(p) as im:
        w, h = im.size
        small = im.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float32) / 255.0

    aspect = w / max(1, h)
    if aspect >= 1.18:
        orientation = "landscape"
    elif aspect <= 0.85:
        orientation = "portrait"
    else:
        orientation = "square"

    luma = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    mean_luma = float(luma.mean())
    tone_band = "dark" if mean_luma < 0.36 else ("light" if mean_luma > 0.66 else "mid")

    # Detail density via simple horizontal + vertical gradient magnitude
    gx = np.abs(np.diff(luma, axis=1)).mean()
    gy = np.abs(np.diff(luma, axis=0)).mean()
    detail_density = float(gx + gy)

    clean_title = " ".join((title or "").split())
    title_len = len(clean_title)
    title_bucket = "long" if title_len > 38 else ("medium" if title_len > 22 else "short")

    return {
        "width": w,
        "height": h,
        "aspect_ratio": round(aspect, 3),
        "orientation": orientation,
        "mean_luminance": round(mean_luma, 3),
        "tone_band": tone_band,
        "detail_density": round(detail_density, 4),
        "title_length": title_len,
        "title_bucket": title_bucket,
    }


def _score_candidate(
    design: Any,
    *,
    post_format: str,
    lead_profile: dict,
    row_peers: Sequence[dict],
    above_peer: dict | None,
    recent_peers: Sequence[dict],
    usage_counts: Counter[str],
    family_counts: Counter[str],
) -> tuple[float, list[str]]:
    """Score a single design for artwork fit FIRST, then 3-column grid neighbor cohesion."""
    score = 0.0
    reasons: list[str] = []

    # --- 1. Artwork Fit (Primary) ---
    orient = lead_profile["orientation"]
    if orient == "landscape":
        if design.family in LANDSCAPE_FRIENDLY_FAMILIES:
            score += 4.5
            reasons.append("landscape_fit")
        elif design.family in {"poster", "typography"}:
            score -= 2.5
    elif orient == "portrait":
        if design.family in PORTRAIT_FRIENDLY_FAMILIES:
            score += 3.5
            reasons.append("portrait_fit")
    else:  # square
        if design.family in {"gallery", "archive", "colors", "detail"}:
            score += 3.0
            reasons.append("square_fit")

    # Protect long titles from giant poster/display overlays
    if lead_profile["title_bucket"] == "long":
        if design.family in LONG_TITLE_SAFE_FAMILIES:
            score += 3.0
            reasons.append("long_title_safe")
        elif design.family in HEAVY_TYPO_FAMILIES:
            score -= 5.0
    elif lead_profile["title_bucket"] == "short" and design.family in HEAVY_TYPO_FAMILIES:
        score += 1.8

    # High-detail / complex paintings shouldn't be buried under heavy graphic text
    if lead_profile["detail_density"] >= 0.085:
        if design.family in BREATHING_FAMILIES:
            score += 3.2
            reasons.append("preserves_complex_painting")
        elif design.family in HEAVY_TYPO_FAMILIES:
            score -= 3.0

    # Protect portrait faces from diagonal/zigzag split cuts (e.g. neon-060)
    if design.id == "neon-060" and orient == "portrait":
        score -= 9.0

    # Format alignment
    if post_format == "reel":
        if design.family in {"cinema", "detail", "gallery", "colors"}:
            score += 2.5
        elif design.family == "poster":
            score -= 3.5

    # --- 2. Neighbor & Whole-Profile Cohesion (Secondary) ---
    # Horizontal row peers (same 3-column Instagram row)
    row_heavy_typo = sum(1 for p in row_peers if p.get("family") in HEAVY_TYPO_FAMILIES)
    row_dark_cinema = sum(1 for p in row_peers if p.get("family") in DARK_CINEMA_FAMILIES)
    row_families = {p.get("family") for p in row_peers}

    if design.family in HEAVY_TYPO_FAMILIES and row_heavy_typo >= 1:
        score -= 7.5  # Avoid stacking big typography side-by-side in the same row
    if design.family in DARK_CINEMA_FAMILIES and row_dark_cinema >= 1:
        score -= 5.5  # Avoid consecutive dark heavy frames in the same row
    if design.family in row_families:
        score -= 4.5
    if design.family in BREATHING_FAMILIES and (row_heavy_typo >= 1 or row_dark_cinema >= 1):
        score += 3.5  # Intentional breathing-room contrast beside a heavy neighbor
        reasons.append("row_breathing_contrast")

    # Vertical neighbor (directly above in the 3-column grid: slot i-3)
    if above_peer is not None:
        above_fam = above_peer.get("family")
        if above_fam == design.family:
            score -= 5.0
        if above_fam in HEAVY_TYPO_FAMILIES and design.family in HEAVY_TYPO_FAMILIES:
            score -= 6.0  # Never stack heavy typography vertically
        if above_fam in DARK_CINEMA_FAMILIES and design.family in DARK_CINEMA_FAMILIES:
            score -= 4.0

    # Immediate previous post (prevent identical family or portrait-crop repetition)
    if recent_peers:
        prev = recent_peers[-1]
        if prev.get("family") == design.family:
            score -= 4.0
        if (
            prev.get("orientation") == "portrait"
            and orient == "portrait"
            and prev.get("family") == "cinema"
            and design.family == "cinema"
        ):
            score -= 4.5

    # Soft diversity decay: allow intentional rhythm while avoiding narrow 44/280 lock-in
    recent_ids = [p.get("design_id") for p in recent_peers[-18:]]
    if design.id in recent_ids:
        score -= 8.0
    score -= usage_counts[design.id] * 2.4
    score -= family_counts[design.family] * 0.22

    # Slight tie-break favoring both Astra and Neon balance
    return score, reasons


def select_profile_design(
    *,
    post_format: str,
    required_sources: int,
    lead_profile: dict,
    placed_posts: Sequence[dict],
    usage_counts: Counter[str],
    family_counts: Counter[str],
    is_quiz: bool = False,
    soft_nudges: dict[str, float] | None = None,
) -> tuple[str, dict]:
    """Select the strongest design from the full 280-design catalog for artwork fit & 3-column grid rhythm."""
    usable = list_usable_designs()
    candidates = [d for d in usable if d.required_sources == required_sources]
    if is_quiz and required_sources == 1:
        quiz_ids = [d for d in candidates if d.id in {"neon-151", "neon-157"}]
        if quiz_ids:
            chosen = min(quiz_ids, key=lambda d: usage_counts[d.id])
            return chosen.id, {"family": chosen.family, "reasons": ["friday_quiz_slot"], "score": 10.0}
    else:
        # Never accidentally assign exact-year quiz templates to non-quiz posts
        candidates = [d for d in candidates if d.id not in {"neon-151", "neon-157"}]

    if post_format == "reel" and required_sources == 1:
        reel_pool = [d for d in candidates if d.family in REEL_PREFERRED_FAMILIES]
        if reel_pool:
            candidates = reel_pool

    idx = len(placed_posts)
    row_start = (idx // 3) * 3
    row_peers = list(placed_posts[row_start:idx])
    above_peer = placed_posts[idx - 3] if idx >= 3 else None

    best_design: Any = None
    best_score = -1e9
    best_reasons: list[str] = []
    nudges = soft_nudges or {}

    for d in candidates:
        sc, reasons = _score_candidate(
            d,
            post_format=post_format,
            lead_profile=lead_profile,
            row_peers=row_peers,
            above_peer=above_peer,
            recent_peers=placed_posts[-18:],
            usage_counts=usage_counts,
            family_counts=family_counts,
        )
        if d.id in nudges:
            sc += nudges[d.id]
            reasons.append(f"operator_nudge:{nudges[d.id]:+.1f}")
        if sc > best_score:
            best_score = sc
            best_design = d
            best_reasons = reasons

    assert best_design is not None, f"No usable design found for required_sources={required_sources}"
    return best_design.id, {
        "family": best_design.family,
        "reasons": best_reasons,
        "score": round(best_score, 2),
    }
