"""30-day adjustable local Artfolio planner: Carousel + Reels ONLY (default 4 Carousels + 2 Reels / day = 6/day)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.art_calendar import (
    find_anniversaries_for_date,
    match_anniversary_artist,
    render_split_screen_comparison,
)
from src.artwork_intelligence import (
    audit_rendered_slide,
    detect_smart_focus,
    draw_human_silhouette,
    should_show_monumental_scale_slide,
    trim_museum_scan_borders,
)
from src.astra_catalog import get_design, list_usable_designs, load_design_catalog
from src.astra_pilot_workspace import PilotArtworkRecord, PriorProfilePost
from src.astra_renderer import (
    AstraArtworkInput,
    extract_pigments,
    hexcol,
    ink,
    load_source_image,
    render_astra_design,
)
from src.editorial_copy_rules import (
    compose_curated_caption,
    compose_first_comment,
    compose_seo_alt_text,
    lint_english_caption,
)
from src.grid_choreography import (
    SIGNATURE_DAYS,
    TEMPO_PROFILES,
    analyze_grid_rhythm,
    compute_cover_tone,
    extract_primary_secondary_rgb,
    sort_schedule_by_color_bridge,
)
from src.meta_business_export import export_meta_business_suite_package
from src.museum_pool import ArtworkReuseLedger, select_diverse_pool
from src.neon_kit import (
    F, GOLD, H, INK, MUTED_INK, MUTED_LIGHT, OBSIDIAN, PAPER, PURE_WHITE,
    SOFT_IVORY, W, byline_for, cover_crop, draw_brand_tag, draw_fit_text,
    paste_centered, short_museum,
)
from src.reel_studio import (
    REEL_MOTION_PRESETS,
    ReelMotionPreset,
    render_local_reel_mp4,
    render_reel_9x16_cover,
    verify_reel_mp4,
)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")
MAX_API_BYTES = 256_000
MAX_PINNED_POSTS = 3
MAX_MONTHLY_POSTS = 240  # Supports 30 days x 6/day = 180 posts (up to 240).

# Default 6 daily prime slots in Europe/Istanbul:
# Row 1: 09:30 Carousel, 12:00 Reel, 14:30 Carousel
# Row 2: 17:30 Carousel, 20:00 Reel, 22:30 Carousel
DAILY_SIX_SLOTS_IST: tuple[tuple[int, int], ...] = (
    (9, 30),
    (12, 0),
    (14, 30),
    (17, 30),
    (20, 0),
    (22, 30),
)


class _StrictMonthlyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonthlyPlanPost(_StrictMonthlyModel):
    """One scheduled local Carousel or Reel in the adjustable monthly plan (no singles)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    post_format: Literal["carousel", "reel"]
    design_id: str = Field(pattern=r"^[a-z][a-z0-9]{1,15}-[0-9]{3}$")
    editorial_title: str = Field(min_length=1, max_length=160)
    caption_en: str = Field(min_length=1, max_length=2200)
    first_comment_en: str = Field(default="", max_length=2200)
    alt_text_en: str = Field(default="", max_length=320)
    anniversary_note: str = Field(default="", max_length=240)
    comparison_mode: Literal["none", "study_vs_final", "before_after_restoration"] = "none"
    scheduled_at: str = Field(min_length=10, max_length=64)
    artworks: tuple[PilotArtworkRecord, ...] = Field(min_length=1, max_length=4)
    detail_focuses: tuple[tuple[float, float] | tuple[float, float, float, float], ...] | None = None
    slides: tuple[str, ...] = Field(default=(), max_length=10)
    slide_count: int = Field(default=1, ge=1, le=10, strict=True)
    rendered_sha256: str = Field(default="", max_length=64)
    reel_cover_9x16_path: str = Field(default="", max_length=512)
    reel_motion_preset: ReelMotionPreset = "detail_to_full"
    reel_kinetic_hook: str = Field(default="", max_length=140)
    reel_zoom_start: tuple[float, float] | None = None
    reel_zoom_end: tuple[float, float] | None = None
    reel_video_path: str = Field(default="", max_length=4096)
    reel_status: Literal["video_ready", "draft_awaiting_reel_video", "not_applicable"] = "not_applicable"
    approval_status: Literal["approved", "draft"] = "draft"
    meta_uploaded: bool = False
    editorial_project_dir: str = Field(default="", max_length=4096)
    dimensions_cm_text: str = Field(default="", max_length=120)
    signature_day_label: str = Field(default="", max_length=80)
    design_family: str = Field(default="", max_length=40)
    tone_band: Literal["dark", "mid", "light"] = "mid"
    temp_band: Literal["warm", "neutral", "cool"] = "neutral"
    luminance: float = Field(default=0.5, ge=0.0, le=1.0)
    qa_warnings: tuple[str, ...] = Field(default=(), max_length=20)


class MonthlyPlanState(_StrictMonthlyModel):
    """Persisted local state for a Carousel + Reels monthly plan (default 4 Carousels + 2 Reels/day)."""

    schema_version: Literal["artfolio-monthly-plan-v2"] = "artfolio-monthly-plan-v2"
    profile_handle: str = Field(default="@artfolio.db", min_length=2, max_length=64)
    publication_ready: Literal[False] = False
    days: int = Field(default=30, ge=1, le=60, strict=True)
    start_at: str = Field(min_length=10, max_length=64)
    carousels_per_day: int = Field(default=4, ge=0, le=8, strict=True)
    reels_per_day: int = Field(default=2, ge=0, le=6, strict=True)
    carousel_count: int = Field(ge=0, le=MAX_MONTHLY_POSTS, strict=True)
    reel_count: int = Field(ge=0, le=MAX_MONTHLY_POSTS, strict=True)
    posts: tuple[MonthlyPlanPost, ...] = Field(min_length=1, max_length=MAX_MONTHLY_POSTS)
    schedule_order: tuple[str, ...] = Field(min_length=1, max_length=MAX_MONTHLY_POSTS)
    pinned_ids: tuple[str, ...] = Field(default=(), max_length=MAX_PINNED_POSTS)
    history_posts: tuple[PriorProfilePost, ...] = Field(default=(), max_length=30)
    planner_notes: str = Field(
        default=(
            "Tekli gönderi devre dışı. Varsayılan tempo: günde 6 gönderi (4 Kaydırmalı + 2 Reels), "
            "her gün 2 tam 3'lü Instagram satırı ve orta sütunda Reels omurgası kurar."
        ),
        max_length=500,
    )

    @model_validator(mode="after")
    def _validate_integrity(self) -> "MonthlyPlanState":
        post_ids = [p.id for p in self.posts]
        if len(set(post_ids)) != len(post_ids):
            raise ValueError("Monthly plan post IDs must be unique")
        if set(self.schedule_order) != set(post_ids) or len(self.schedule_order) != len(post_ids):
            raise ValueError("schedule_order must reference every post exactly once")
        hist_ids = [h.id for h in self.history_posts]
        if len(set(hist_ids)) != len(hist_ids):
            raise ValueError("History post IDs must be unique")
        if set(post_ids).intersection(hist_ids):
            raise ValueError("Post IDs and history post IDs must be distinct")
        valid_pins = set(post_ids).union(hist_ids)
        if len(set(self.pinned_ids)) != len(self.pinned_ids):
            raise ValueError("Pinned IDs must be unique")
        if any(pid not in valid_pins for pid in self.pinned_ids):
            raise ValueError("Pinned IDs must exist in posts or history")
        return self


def _daily_format_sequence(carousels_per_day: int, reels_per_day: int) -> list[Literal["carousel", "reel"]]:
    """Order one day's formats cleanly; for 4 Carousels + 2 Reels puts Reels in slots 2 & 5
    so every 3-post Instagram row has Carousel | Reel | Carousel (center Reels spine!).
    """
    if carousels_per_day == 4 and reels_per_day == 2:
        return ["carousel", "reel", "carousel", "carousel", "reel", "carousel"]
    total = carousels_per_day + reels_per_day
    if total < 1:
        raise ValueError("At least one carousel or reel per day is required")
    seq: list[Literal["carousel", "reel"]] = []
    c_used = r_used = 0
    for step in range(total):
        c_def = (carousels_per_day / total) * (step + 1) - c_used if c_used < carousels_per_day else -1e9
        r_def = (reels_per_day / total) * (step + 1) - r_used if r_used < reels_per_day else -1e9
        if r_def > c_def:
            seq.append("reel")
            r_used += 1
        else:
            seq.append("carousel")
            c_used += 1
    return seq


def build_daily_cadence_schedule(
    start_at: datetime,
    *,
    days: int,
    carousels_per_day: int = 4,
    reels_per_day: int = 2,
) -> list[tuple[Literal["carousel", "reel"], str]]:
    """Generate exact (format, ISO timestamp) slots across `days` in Europe/Istanbul."""
    per_day = carousels_per_day + reels_per_day
    total = days * per_day
    if total < 1:
        raise ValueError("Schedule must contain at least 1 post")
    if total > MAX_MONTHLY_POSTS:
        raise ValueError(f"Monthly plan supports at most {MAX_MONTHLY_POSTS} posts (requested {total})")

    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=ISTANBUL_TZ)
    base_day = start_at.astimezone(ISTANBUL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    day_pattern = _daily_format_sequence(carousels_per_day, reels_per_day)

    # Distribute `per_day` slots smoothly between 09:30 and 22:30 Istanbul time
    if per_day == 6:
        slot_times = list(DAILY_SIX_SLOTS_IST)
    elif per_day == 1:
        slot_times = [(18, 30)]
    else:
        start_min, end_min = 9 * 60 + 30, 22 * 60 + 30
        step = (end_min - start_min) / (per_day - 1)
        slot_times = [(int((start_min + i * step) // 60), int((start_min + i * step) % 60)) for i in range(per_day)]

    slots: list[tuple[Literal["carousel", "reel"], str]] = []
    for day_idx in range(days):
        day_dt = base_day + timedelta(days=day_idx)
        for fmt, (hh, mm) in zip(day_pattern, slot_times, strict=True):
            ts = day_dt.replace(hour=hh, minute=mm).isoformat()
            slots.append((fmt, ts))
    return slots


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _render_inner_carousel_slides(
    workspace_dir: Path,
    post: MonthlyPlanPost,
    sources: list[AstraArtworkInput],
    *,
    explicit_slide_specs: Sequence[str] | None = None,
) -> list[str]:
    """Generate rich ordered inner slides (2..N) from a single finalized slide_specs list so
    every JPG's corner page number (e.g. 02/03, 03/03) matches the exact exported slide count.
    """
    art = sources[0]
    pil_raw = load_source_image(art.validated_path())
    pil, _ = trim_museum_scan_borders(pil_raw)
    smart_focus = art.focal_point() if art.focus is not None else detect_smart_focus(pil)
    renders_dir = workspace_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)

    is_quiz = post.design_id in {"neon-151", "neon-157"}
    show_scale, dims_cm = should_show_monumental_scale_slide(post.dimensions_cm_text)

    # Curated pools from our 280-design catalog so inner slides showcase diverse layouts (not 80% detail crops!)
    _INNER_POOLS = {
        "template_gallery": ("astra-002", "astra-004", "astra-006", "neon-012", "neon-015", "neon-021", "astra-008"),
        "template_archive": ("astra-041", "astra-042", "astra-044", "neon-071", "neon-074", "neon-078", "astra-046"),
        "template_colors": ("astra-061", "astra-063", "astra-066", "neon-101", "neon-103", "neon-106", "astra-068"),
        "template_story": ("astra-081", "astra-083", "astra-085", "neon-141", "neon-144", "neon-148", "astra-088"),
        "template_cinema": ("astra-051", "astra-053", "astra-056", "neon-051", "neon-054", "neon-057"),
        "template_experimental": ("astra-091", "astra-093", "neon-122", "neon-124", "neon-173", "neon-177"),
    }
    seed_int = int(hashlib.sha256(f"{post.id}:{post.design_id}".encode("utf-8")).hexdigest()[:8], 16)

    def _pick_inner_design(pool_key: str, salt: int = 0) -> str:
        pool = [d for d in _INNER_POOLS[pool_key] if d != post.design_id]
        return pool[(seed_int + salt) % len(pool)]

    if explicit_slide_specs is not None:
        slide_specs = list(explicit_slide_specs)
    else:
        if is_quiz:
            slide_specs = ["quiz_reveal", "full_plate"]
        elif len(sources) >= 3:
            slide_specs = ["room_1_gallery", "room_2_archive", "room_3_colors"]
        elif len(sources) == 2:
            slide_specs = ["duet_work_1", "duet_work_2"]
        else:
            arc = seed_int % 5
            if arc == 0:
                slide_specs = ["template_gallery", "template_archive"]
            elif arc == 1:
                slide_specs = ["template_colors", "full_plate"]
            elif arc == 2:
                slide_specs = ["detail_callout", "full_plate"]
            elif arc == 3:
                slide_specs = ["template_story", "template_cinema"]
            else:
                slide_specs = ["full_plate", "template_experimental"]

        if show_scale and dims_cm is not None:
            slide_specs.append("monumental_scale")
        if len(sources) < 3:
            slide_specs.append("save_card")

    total_slides = 1 + len(slide_specs)
    inner_paths: list[str] = []

    for offset, kind in enumerate(slide_specs, 2):
        page_sig = f"{offset:02d} / {total_slides:02d}"
        rel = f"renders/{post.id}-slide-{offset:02d}.jpg"

        if kind in _INNER_POOLS:
            did = _pick_inner_design(kind, offset)
            render_astra_design(did, [art], art.title, workspace_dir / rel)
            inner_paths.append(rel)

        elif kind in {"room_1_gallery", "room_2_archive", "room_3_colors"}:
            room_idx = {"room_1_gallery": 0, "room_2_archive": 1, "room_3_colors": 2}[kind]
            pool_key = {"room_1_gallery": "template_gallery", "room_2_archive": "template_archive", "room_3_colors": "template_colors"}[kind]
            room_src = sources[room_idx]
            did = _pick_inner_design(pool_key, room_idx + 1)
            render_astra_design(did, [room_src], f"Room 0{room_idx + 1}: {room_src.title}", workspace_dir / rel)
            inner_paths.append(rel)

        elif kind in {"duet_work_1", "duet_work_2"}:
            w_idx = 0 if kind == "duet_work_1" else 1
            pool_key = "template_gallery" if w_idx == 0 else "template_archive"
            w_src = sources[w_idx]
            did = _pick_inner_design(pool_key, w_idx + 2)
            render_astra_design(did, [w_src], w_src.title, workspace_dir / rel)
            inner_paths.append(rel)

        elif kind == "quiz_reveal":
            # #16 Quiz Answer Reveal Slide
            im = Image.new("RGB", (W, H), OBSIDIAN)
            paste_centered(im, pil, (80, 180, W - 80, 860), shadow=True)
            d = ImageDraw.Draw(im)
            draw_brand_tag(d, 72, 54, GOLD, f"{page_sig}  ·  ANSWER REVEAL")
            draw_fit_text(d, f"Painted in {art.date or 'Archive'}", (72, 916), "didot", 62, W - 144, GOLD, max_lines=1)
            draw_fit_text(d, f"{art.artist} — {art.title}", (72, 1020), "serif", 40, W - 144, SOFT_IVORY, max_lines=2)
            draw_fit_text(d, short_museum(art.institution), (72, 1210), "mono", 20, W - 144, MUTED_LIGHT, max_lines=1)
            im.save(workspace_dir / rel, format="JPEG", quality=92, optimize=True, subsampling=0)
            inner_paths.append(rel)

        elif kind == "detail_callout":
            # #13 Curiosity Reward: marked detail callout (only ~20% of carousels)
            im = Image.new("RGB", (W, H), OBSIDIAN)
            im.paste(cover_crop(pil, W - 128, 600, focus=smart_focus, zoom=2.25), (64, 140))
            mini_box = paste_centered(im, pil, (64, 774, 420, 1080))
            d = ImageDraw.Draw(im)
            draw_brand_tag(d, 64, 54, GOLD, f"{page_sig}  ·  CLOSER LOOK")
            d.rectangle((64, 140, W - 64, 740), outline=GOLD, width=3)
            mx0, my0, mx1, my1 = mini_box
            fx_px = int(mx0 + smart_focus[0] * (mx1 - mx0))
            fy_px = int(my0 + smart_focus[1] * (my1 - my0))
            d.rectangle((fx_px - 28, fy_px - 28, fx_px + 28, fy_px + 28), outline=GOLD, width=3)
            draw_fit_text(d, "Detail in focus", (456, 800), "didot", 48, W - 520, SOFT_IVORY, max_lines=2)
            draw_fit_text(d, f"{art.title}\n{art.artist} ({art.date or 'Archive'})", (456, 916), "sans", 24, W - 520, MUTED_LIGHT, max_lines=4)
            draw_fit_text(d, byline_for(art), (64, 1220), "mono", 19, W - 128, MUTED_LIGHT, max_lines=1)
            im.save(workspace_dir / rel, format="JPEG", quality=92, optimize=True, subsampling=0)
            inner_paths.append(rel)

        elif kind == "full_plate":
            im = Image.new("RGB", (W, H), PAPER)
            paste_centered(im, pil, (72, 140, W - 72, 980), shadow=True)
            d = ImageDraw.Draw(im)
            draw_brand_tag(d, 72, 54, INK, f"{page_sig}  ·  FULL ARTWORK")
            draw_fit_text(d, art.title, (72, 1026), "serif", 46, W - 144, INK, max_lines=2)
            draw_fit_text(d, byline_for(art), (72, 1210), "mono", 20, W - 144, MUTED_INK, max_lines=1)
            im.save(workspace_dir / rel, format="JPEG", quality=92, optimize=True, subsampling=0)
            inner_paths.append(rel)

        elif kind == "second_work":
            src2 = sources[1]
            pil2, _ = trim_museum_scan_borders(load_source_image(src2.validated_path()))
            im = Image.new("RGB", (W, H), PAPER)
            paste_centered(im, pil2, (72, 140, W - 72, 980), shadow=True)
            d = ImageDraw.Draw(im)
            draw_brand_tag(d, 72, 54, INK, f"{page_sig}  ·  COMPANION WORK")
            draw_fit_text(d, src2.title, (72, 1026), "serif", 46, W - 144, INK, max_lines=2)
            draw_fit_text(d, byline_for(src2), (72, 1210), "mono", 20, W - 144, MUTED_INK, max_lines=1)
            im.save(workspace_dir / rel, format="JPEG", quality=92, optimize=True, subsampling=0)
            inner_paths.append(rel)

        elif kind == "monumental_scale" and dims_cm is not None:
            # #18 True Museum Scale Check (ONLY for very large works >= 180 cm)
            h_cm, w_cm = dims_cm
            im = Image.new("RGB", (W, H), (236, 232, 222))
            d = ImageDraw.Draw(im)
            draw_brand_tag(d, 64, 54, INK, f"{page_sig}  ·  MONUMENTAL SCALE CHECK")
            floor_y = 950
            d.rectangle((0, floor_y, W, H), fill=(214, 206, 192))
            d.line((64, floor_y, W - 64, floor_y), fill=INK, width=2)
            # Fit artwork + 170cm visitor at the exact same pixels-per-cm scale
            scale_px_per_cm = min(640.0 / max(100.0, w_cm), 620.0 / max(170.0, h_cm))
            art_w_px = max(120, int(round(w_cm * scale_px_per_cm)))
            art_h_px = max(120, int(round(h_cm * scale_px_per_cm)))
            human_h_px = max(90, int(round(170.0 * scale_px_per_cm)))
            art_x = 300 + (680 - art_w_px) // 2
            art_y = max(160, floor_y - 90 - art_h_px)
            fitted = pil.resize((art_w_px, art_h_px), Image.Resampling.LANCZOS)
            im.paste(fitted, (art_x, art_y))
            d.rectangle((art_x, art_y, art_x + art_w_px, art_y + art_h_px), outline=INK, width=2)
            draw_human_silhouette(d, 170, floor_y, human_h_px)
            d.text((112, floor_y + 14), "170 cm", font=F("mono_b", 20), fill=INK)
            d.text((art_x, floor_y + 14), f"{w_cm:.0f} × {h_cm:.0f} cm", font=F("mono_b", 20), fill=INK)
            draw_fit_text(d, f"Standing before {art.title}", (64, 1020), "serif", 44, W - 128, INK, max_lines=2)
            draw_fit_text(d, byline_for(art), (64, 1210), "mono", 19, W - 128, MUTED_INK, max_lines=1)
            im.save(workspace_dir / rel, format="JPEG", quality=92, optimize=True, subsampling=0)
            inner_paths.append(rel)

        else:  # save_card (#14 Final Archival Save Card)
            src_last = sources[-1]
            pil_last = load_source_image(src_last.validated_path())
            pig = extract_pigments(pil_last, 5)
            im = Image.new("RGB", (W, H), PAPER)
            d = ImageDraw.Draw(im)
            draw_brand_tag(d, 72, 54, INK, f"{page_sig}  ·  ARCHIVAL SAVE CARD")
            paste_centered(im, pil_last, (72, 130, W - 72, 690), shadow=True)
            sw = (W - 144) // 5
            for idx, (col, _) in enumerate(pig):
                d.rectangle((72 + idx * sw, 734, 72 + (idx + 1) * sw - 8, 884), fill=col)
                d.text((84 + idx * sw, 846), hexcol(col), font=F("mono_b", 18), fill=PURE_WHITE if ink(col) != "#1a1915" else INK)
            draw_fit_text(d, f"{src_last.artist} — {src_last.title}", (72, 932), "serif", 42, W - 144, INK, max_lines=2)
            draw_fit_text(d, f"{short_museum(src_last.institution)}  ·  {src_last.date or 'Archive'}", (72, 1110), "mono", 20, W - 144, MUTED_INK, max_lines=1)
            d.rounded_rectangle((72, 1170, 560, 1236), radius=10, fill=INK)
            d.text((98, 1190), "SAVE THIS PALETTE & ARCHIVE CARD", font=F("mono_b", 20), fill=PAPER)
            im.save(workspace_dir / rel, format="JPEG", quality=92, optimize=True, subsampling=0)
            inner_paths.append(rel)

    return inner_paths


def _sources_from_post(workspace_dir: Path, post: MonthlyPlanPost) -> list[AstraArtworkInput]:
    out = []
    for art in post.artworks:
        p = Path(art.image_path)
        if not p.is_absolute():
            p = (workspace_dir / p).resolve()
        out.append(
            AstraArtworkInput(
                image_path=p,
                title=art.title,
                artist=art.artist,
                date=art.date,
                institution=art.institution,
                museum_url=art.museum_url,
                focus=art.focus,
            )
        )
    return out


def _render_monthly_post(
    workspace_dir: Path,
    post: MonthlyPlanPost,
    *,
    preserve_inner_slides: bool = False,
) -> MonthlyPlanPost:
    """Render Slide 1 (4:5 grid cover) + carousel inner slides OR 9:16 Reels cover."""
    entry = get_design(post.design_id)
    if not entry.is_usable:
        raise ValueError(f"Design '{post.design_id}' is not ready for rendering")
    if len(post.artworks) != entry.required_sources:
        raise ValueError(
            f"Design '{post.design_id}' requires {entry.required_sources} artwork(s), "
            f"but post '{post.id}' has {len(post.artworks)}"
        )
    sources = _sources_from_post(workspace_dir, post)
    renders_dir = workspace_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    seed_payload = json.dumps(
        {
            "id": post.id,
            "design_id": post.design_id,
            "comparison_mode": post.comparison_mode,
            "editorial_title": post.editorial_title,
            "artworks": [a.model_dump(mode="json") for a in post.artworks],
            "detail_focuses": post.detail_focuses,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    h12 = hashlib.sha256(seed_payload.encode("utf-8")).hexdigest()[:12]
    cover_rel = f"renders/{post.id}-{post.design_id}-{h12}.jpg"
    if post.comparison_mode != "none" and len(sources) >= 2:
        out_cov = render_split_screen_comparison(
            sources[0],
            sources[1],
            workspace_dir / cover_rel,
            mode=post.comparison_mode,
            editorial_title=post.editorial_title,
        )
        res = {"sha256": hashlib.sha256(out_cov.read_bytes()).hexdigest()}
    else:
        res = render_astra_design(
            post.design_id,
            sources,
            post.editorial_title,
            workspace_dir / cover_rel,
            detail_focuses=post.detail_focuses,
        )

    reel_cover_9x16_rel = ""
    if post.post_format == "carousel":
        if preserve_inner_slides and len(post.slides) > 1:
            slides = (cover_rel, *post.slides[1:])
        else:
            inner = _render_inner_carousel_slides(workspace_dir, post, sources)
            slides = (cover_rel, *inner)
        reel_status: Literal["video_ready", "draft_awaiting_reel_video", "not_applicable"] = "not_applicable"
    else:
        slides = (cover_rel,)
        reel_cover_9x16_rel = f"renders/{post.id}-{post.design_id}-{h12}-9x16.jpg"
        render_reel_9x16_cover(workspace_dir / cover_rel, workspace_dir / reel_cover_9x16_rel)
        vp = Path(post.reel_video_path) if post.reel_video_path else None
        if vp is not None and not vp.is_absolute():
            vp = (workspace_dir / vp).resolve()
        reel_status = "video_ready" if (vp is not None and vp.is_file()) else "draft_awaiting_reel_video"

    tone = compute_cover_tone(workspace_dir / cover_rel)
    qa = audit_rendered_slide(workspace_dir / cover_rel)
    cap_qa = lint_english_caption(post.caption_en)
    combined_warnings = tuple(qa["warnings"] + cap_qa["warnings"])
    art_dicts = [a.model_dump(mode="json") for a in post.artworks]
    first_comment = post.first_comment_en or compose_first_comment(art_dicts)
    pri_rgb, sec_rgb = extract_primary_secondary_rgb(workspace_dir / cover_rel)
    pal_hex = [hexcol(pri_rgb), hexcol(sec_rgb)]
    alt_text = post.alt_text_en or compose_seo_alt_text(
        art_dicts,
        post_format=post.post_format,
        tone_band=tone["tone_band"],
        palette_hex=pal_hex,
    )

    return post.model_copy(
        update={
            "slides": slides,
            "slide_count": len(slides),
            "rendered_sha256": res["sha256"],
            "reel_cover_9x16_path": reel_cover_9x16_rel,
            "reel_status": reel_status,
            "first_comment_en": first_comment,
            "alt_text_en": alt_text,
            "design_family": entry.family,
            "tone_band": tone["tone_band"],
            "temp_band": tone["temp_band"],
            "luminance": tone["luminance"],
            "qa_warnings": combined_warnings,
        }
    )


def save_monthly_plan_state(workspace_dir: Path, state: MonthlyPlanState) -> None:
    _atomic_write(
        workspace_dir / "monthly_plan.json",
        json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    )


def load_monthly_plan_state(workspace_dir: Path) -> MonthlyPlanState:
    path = Path(workspace_dir).resolve() / "monthly_plan.json"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Monthly plan state not found: {path}")
    return MonthlyPlanState.model_validate_json(path.read_text(encoding="utf-8"))


def compute_profile_at_moment(
    state: MonthlyPlanState,
    moment_index: int | None = None,
) -> list[dict]:
    """Compute Instagram 3-column profile at a given publication moment (1..N) or full plan (None)."""
    post_map = {item.id: item for item in state.posts}
    hist_map = {item.id: item for item in state.history_posts}
    total = len(state.schedule_order)
    cutoff = total if moment_index is None else max(0, min(total, int(moment_index)))

    published_ids_chrono = list(state.schedule_order[:cutoff])
    published_set = set(published_ids_chrono)
    slot_lookup = {pid: idx + 1 for idx, pid in enumerate(state.schedule_order)}

    def _pack_post(item: MonthlyPlanPost, slot_num: int, *, pinned: bool) -> dict:
        dt = datetime.fromisoformat(item.scheduled_at).astimezone(ISTANBUL_TZ)
        return {
            "id": item.id,
            "kind": "scheduled",
            "slot_number": slot_num,
            "pinned": pinned,
            "post_format": item.post_format,
            "design_id": item.design_id,
            "editorial_title": item.editorial_title,
            "caption_en": item.caption_en,
            "first_comment_en": item.first_comment_en,
            "alt_text_en": item.alt_text_en,
            "anniversary_note": item.anniversary_note,
            "comparison_mode": item.comparison_mode,
            "scheduled_at": item.scheduled_at,
            "scheduled_label": dt.strftime("%d.%m %H:%M"),
            "artworks": [a.model_dump(mode="json") for a in item.artworks],
            "image_path": item.slides[0] if item.slides else "",
            "slides": list(item.slides),
            "slide_count": item.slide_count,
            "reel_cover_9x16_path": item.reel_cover_9x16_path,
            "reel_motion_preset": item.reel_motion_preset,
            "reel_kinetic_hook": item.reel_kinetic_hook,
            "reel_zoom_start": list(item.reel_zoom_start) if item.reel_zoom_start else None,
            "reel_zoom_end": list(item.reel_zoom_end) if item.reel_zoom_end else None,
            "reel_video_path": item.reel_video_path,
            "reel_status": item.reel_status,
            "approval_status": item.approval_status,
            "meta_uploaded": item.meta_uploaded,
            "editorial_project_dir": item.editorial_project_dir,
            "signature_day_label": item.signature_day_label,
            "design_family": item.design_family,
            "tone_band": item.tone_band,
            "temp_band": item.temp_band,
            "luminance": item.luminance,
            "qa_warnings": list(item.qa_warnings),
        }

    def _pack_hist(item: PriorProfilePost, *, pinned: bool) -> dict:
        return {
            "id": item.id,
            "kind": "history",
            "slot_number": 0,
            "pinned": pinned,
            "post_format": item.post_format,
            "design_id": None,
            "editorial_title": item.title,
            "caption_en": item.caption_en,
            "scheduled_at": "",
            "scheduled_label": item.published_at_label or "Önceki profil",
            "artworks": [
                {
                    "source_key": item.id,
                    "image_path": item.image_path,
                    "artist": item.artist,
                    "title": item.title,
                    "date": item.date,
                    "institution": item.institution,
                    "museum_url": item.museum_url,
                }
            ],
            "image_path": item.image_path,
            "slides": [item.image_path],
            "slide_count": 1,
            "reel_cover_9x16_path": "",
            "reel_motion_preset": "detail_to_full",
            "reel_video_path": "",
            "reel_status": "not_applicable",
            "approval_status": "approved",
            "editorial_project_dir": "",
        }

    cells: list[dict] = []
    seen: set[str] = set()

    for pid in state.pinned_ids:
        if pid in seen:
            continue
        if pid in published_set and pid in post_map:
            seen.add(pid)
            cells.append(_pack_post(post_map[pid], slot_lookup[pid], pinned=True))
        elif pid in hist_map:
            seen.add(pid)
            cells.append(_pack_hist(hist_map[pid], pinned=True))

    for pid in reversed(published_ids_chrono):
        if pid in seen:
            continue
        seen.add(pid)
        cells.append(_pack_post(post_map[pid], slot_lookup[pid], pinned=False))

    for hpost in state.history_posts:
        if hpost.id in seen:
            continue
        seen.add(hpost.id)
        cells.append(_pack_hist(hpost, pinned=False))

    return cells


def _load_editorial_project_spec(project_dir: Path, post_id: str, design_id: str, scheduled_at: str) -> tuple[dict, list[Path]]:
    """Load an existing local EditorialStoryPlan project into a monthly Carousel spec."""
    from src.story_feed import review_snapshot
    from src.story_project import load_project

    pdir = project_dir.resolve()
    project = load_project(pdir)
    snapshot = review_snapshot(pdir, project)
    report_path = (pdir / "current").resolve() / "report.json"
    caption = ""
    if report_path.is_file():
        caption = json.loads(report_path.read_text(encoding="utf-8")).get("caption", "")

    artworks = []
    for src in project.sources:
        aw = src.artwork
        artworks.append(
            {
                "source_key": f"proj_{post_id}_{aw.canonical_id[:20]}",
                "image_path": str(Path(src.image_path).resolve()),
                "artist": aw.artist_display_name or aw.artist_name,
                "title": aw.title,
                "date": aw.creation_date_display or aw.creation_date or "",
                "institution": aw.museum_name or "",
                "museum_url": aw.artwork_url or aw.museum_url or "",
            }
        )
    slide_paths = [Path(p).resolve() for p in snapshot["paths"]]
    spec = {
        "id": post_id,
        "post_format": "carousel",
        "design_id": design_id,
        "editorial_title": project.plan.public_title,
        "caption_en": caption or f"{project.plan.public_title}\n\n{artworks[0]['artist']} — {artworks[0]['title']}",
        "scheduled_at": scheduled_at,
        "artworks": artworks[:1] if get_design(design_id).required_sources == 1 else artworks,
        "slide_count": max(2, len(slide_paths)),
        "editorial_project_dir": str(pdir),
    }
    return spec, slide_paths


def build_monthly_post_specs(
    assets_dir: Path,
    *,
    days: int = 30,
    carousels_per_day: int = 4,
    reels_per_day: int = 2,
    start_at: datetime | None = None,
    editorial_projects: Sequence[Path] = (),
    reuse_ledger_path: str | Path | None = None,
) -> tuple[list[dict], list[dict], dict[str, list[Path]]]:
    """Build a Carousel + Reels ONLY schedule (default 4 Carousels + 2 Reels/day = 6/day, 0 singles)."""
    meta_path = assets_dir / "art.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"Museum metadata not found: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    all_keys = sorted(k for k in meta if (assets_dir / f"{k}.jpg").is_file())
    if len(all_keys) < 6:
        raise ValueError("At least 6 real museum artwork files are required in assets_dir")

    start_dt = start_at or datetime.now(ISTANBUL_TZ).replace(hour=9, minute=30, second=0, microsecond=0)
    slots = build_daily_cadence_schedule(
        start_dt,
        days=days,
        carousels_per_day=carousels_per_day,
        reels_per_day=reels_per_day,
    )

    # Rich rotation across BOTH Astra (100) and Neon (180) templates
    carousel_designs_1 = [
        "astra-001", "neon-009", "astra-013", "neon-180", "astra-061", "neon-173",
        "astra-041", "neon-103", "astra-031", "neon-100", "astra-066", "neon-132",
        "astra-071", "neon-122", "astra-012", "neon-151", "astra-042", "neon-174",
        "astra-004", "neon-051", "astra-093", "neon-124", "astra-076", "neon-091",
        "astra-011", "neon-067", "astra-068", "neon-177", "astra-048", "neon-082",
    ]
    carousel_designs_2 = ["astra-021", "neon-060", "astra-027", "neon-025", "astra-028", "neon-031"]
    carousel_designs_3 = ["astra-094", "neon-004"]
    reel_designs = [
        "astra-083", "neon-050", "astra-051", "neon-113", "astra-053",
        "neon-168", "astra-057", "neon-171", "astra-059", "neon-069",
        "astra-081", "neon-111", "astra-056", "neon-163", "astra-086",
    ]
    motion_presets: tuple[ReelMotionPreset, ...] = (
        "detail_to_full",
        "slow_pan_horizontal",
        "palette_reveal",
        "chiaroscuro_zoom",
    )

    def _art(key: str) -> dict:
        artist, title, date, url, _acc, institution = meta[key]
        return {
            "source_key": key,
            "image_path": str((assets_dir / f"{key}.jpg").resolve()),
            "artist": artist,
            "title": title,
            "date": date,
            "institution": institution,
            "museum_url": url,
        }

    def _short_title(text: str, limit: int) -> str:
        clean = " ".join(str(text).split())
        if len(clean) <= limit:
            return clean
        cut = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-(")
        return cut or clean[:limit]

    def _hashtags(artist: str, fmt: str) -> str:
        last = re.sub(r"[^a-z0-9]", "", artist.split()[-1].lower()) or "fineart"
        tag_fmt = "#artreels" if fmt == "reel" else "#arthistory"
        return f"#{last} {tag_fmt} #museumart #artdetails #artfolio"

    history_keys = all_keys[-3:]
    raw_pool_keys = all_keys[:-3]
    ledger = ArtworkReuseLedger(reuse_ledger_path) if reuse_ledger_path else None
    diverse_arts, _pool_report = select_diverse_pool(
        [_art(k) for k in raw_pool_keys],
        max(len(raw_pool_keys), len(slots)),
        reuse_ledger=ledger,
        min_short_edge=300,
    )
    pool_keys = [item["source_key"] for item in diverse_arts] or raw_pool_keys
    posts: list[dict] = []
    project_slides_map: dict[str, list[Path]] = {}
    proj_queue = list(editorial_projects)
    c_i = r_i = 0

    for idx, (fmt, ts) in enumerate(slots, 1):
        pid = f"post-{idx:03d}"
        slot_dt = datetime.fromisoformat(ts).astimezone(ISTANBUL_TZ)
        sig_day = SIGNATURE_DAYS[slot_dt.weekday()]
        if proj_queue and fmt == "carousel":
            pdir = proj_queue.pop(0)
            chosen_design = carousel_designs_1[c_i % len(carousel_designs_1)]
            spec, p_slides = _load_editorial_project_spec(pdir, pid, chosen_design, ts)
            spec["signature_day_label"] = sig_day["label"]
            spec["first_comment_en"] = compose_first_comment(spec["artworks"])
            posts.append(spec)
            project_slides_map[pid] = p_slides
            c_i += 1
            continue

        k1 = pool_keys[(idx - 1) % len(pool_keys)]
        k2 = pool_keys[(idx + 6) % len(pool_keys)]
        k3 = pool_keys[(idx + 12) % len(pool_keys)]
        a1 = _art(k1)

        if fmt == "carousel":
            if c_i % 6 == 2:
                did = carousel_designs_3[(c_i // 6) % len(carousel_designs_3)]
                a2, a3 = _art(k2), _art(k3)
                arts = [a1, a2, a3]
                headline = f"Three rooms: {a1['artist'].split()[-1]}, {a2['artist'].split()[-1]} & {a3['artist'].split()[-1]}"
            elif c_i % 6 == 4:
                did = carousel_designs_2[(c_i // 6) % len(carousel_designs_2)]
                a2 = _art(k2)
                arts = [a1, a2]
                headline = f"{a1['artist'].split()[-1]} / {a2['artist'].split()[-1]}"
            else:
                # Honor Friday Quiz & Weekly Signature Day hints on first carousel of each day
                if c_i % max(1, carousels_per_day) == 0 and slot_dt.weekday() == 4:
                    did = "neon-151"
                elif c_i % max(1, carousels_per_day) == 0 and sig_day["design_hint"] in carousel_designs_1:
                    did = sig_day["design_hint"]
                else:
                    did = carousel_designs_1[c_i % len(carousel_designs_1)]
                arts = [a1]
                headline = (
                    f"Guess the year: {a1['artist'].split()[-1]}"
                    if did == "neon-151"
                    else f"Inside {_short_title(a1['title'], 38)}"
                )
            is_quiz = did in {"neon-151", "neon-157"}
            caption = compose_curated_caption(post_format="carousel", artworks=arts, slot_index=idx, is_quiz=is_quiz)
            c_i += 1
            posts.append(
                {
                    "id": pid,
                    "post_format": "carousel",
                    "design_id": did,
                    "editorial_title": headline,
                    "caption_en": caption,
                    "first_comment_en": compose_first_comment(arts),
                    "scheduled_at": ts,
                    "artworks": arts,
                    "slide_count": 4,
                    "signature_day_label": sig_day["label"],
                    "approval_status": "approved" if idx <= 6 else "draft",
                }
            )
        else:
            did = reel_designs[r_i % len(reel_designs)]
            preset = motion_presets[r_i % len(motion_presets)]
            r_i += 1
            ann_note = ""
            kinetic_hook = ""
            # On-This-Day radar targets the prime-time 20:00 Reel slot (or any Reel if hour >= 18)
            if slot_dt.hour >= 18:
                anns = find_anniversaries_for_date(slot_dt.date())
                if anns:
                    ann = anns[0]
                    ann_note = ann.format_badge()
                    # Prioritize a matching artist from the pool if present
                    for cand_key in pool_keys:
                        cand_art = _art(cand_key)
                        if match_anniversary_artist(ann, cand_art["artist"]):
                            a1 = cand_art
                            break
                    kinetic_hook = f"On this day ({ann.year}): {ann.artist} — {ann.headline}."
            headline = (
                f"On This Day: {a1['artist'].split()[-1]}"
                if ann_note
                else f"Who holds the light in {a1['artist'].split()[-1]}?"
            )
            caption = compose_curated_caption(post_format="reel", artworks=[a1], slot_index=idx)
            posts.append(
                {
                    "id": pid,
                    "post_format": "reel",
                    "design_id": did,
                    "editorial_title": headline,
                    "caption_en": caption,
                    "first_comment_en": compose_first_comment([a1]),
                    "anniversary_note": ann_note,
                    "reel_kinetic_hook": kinetic_hook,
                    "scheduled_at": ts,
                    "artworks": [a1],
                    "slide_count": 1,
                    "reel_motion_preset": preset,
                    "signature_day_label": sig_day["label"],
                    "approval_status": "approved" if idx <= 6 else "draft",
                }
            )

    history = []
    for h_idx, hkey in enumerate(history_keys, 1):
        artist, title, date, url, _acc, institution = meta[hkey]
        history.append(
            {
                "id": f"hist-{h_idx:02d}",
                "post_format": "carousel",
                "title": title,
                "artist": artist,
                "date": date,
                "institution": institution,
                "museum_url": url,
                "caption_en": f"Earlier profile post — {artist}, {title} ({date}).",
                "source_image": str((assets_dir / f"{hkey}.jpg").resolve()),
                "published_at_label": f"Önceki profil · {h_idx}",
            }
        )

    return posts, history, project_slides_map


def create_monthly_plan_workspace(
    output_dir: str | Path,
    post_specs: list[dict],
    *,
    days: int = 30,
    carousels_per_day: int = 4,
    reels_per_day: int = 2,
    start_at: datetime | None = None,
    history_specs: list[dict] = (),
    pinned_ids: tuple[str, ...] = (),
    project_slides_map: dict[str, list[Path]] | None = None,
    profile_handle: str = "@artfolio.db",
) -> MonthlyPlanState:
    """Create a self-contained local planning workspace (Carousel + Reels ONLY)."""
    output = Path(output_dir).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Monthly workspace output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    project_slides_map = project_slides_map or {}

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".monthly-plan-") as tmp:
        stage = Path(tmp)
        (stage / "sources").mkdir()
        (stage / "renders").mkdir()
        (stage / "videos").mkdir()
        (stage / "history").mkdir()

        copied_sources: dict[str, str] = {}
        rendered_posts: list[MonthlyPlanPost] = []

        for spec in post_specs:
            arts: list[PilotArtworkRecord] = []
            for raw_art in spec["artworks"]:
                src_p = Path(raw_art["image_path"]).resolve()
                if not src_p.is_file():
                    raise FileNotFoundError(f"Artwork source not found: {src_p}")
                key = raw_art["source_key"]
                if key not in copied_sources:
                    rel_src = f"sources/{key}{src_p.suffix.lower() or '.jpg'}"
                    shutil.copyfile(src_p, stage / rel_src)
                    copied_sources[key] = rel_src
                arts.append(
                    PilotArtworkRecord(
                        source_key=key,
                        image_path=copied_sources[key],
                        artist=raw_art["artist"],
                        title=raw_art["title"],
                        date=raw_art.get("date", ""),
                        institution=raw_art.get("institution", ""),
                        museum_url=raw_art.get("museum_url", ""),
                        focus=raw_art.get("focus"),
                    )
                )

            existing_inner: list[str] = []
            if spec["id"] in project_slides_map and len(project_slides_map[spec["id"]]) > 1:
                for s_idx, p_slide in enumerate(project_slides_map[spec["id"]][1:], 2):
                    rel = f"renders/{spec['id']}-proj-slide-{s_idx:02d}.jpg"
                    shutil.copyfile(p_slide, stage / rel)
                    existing_inner.append(rel)

            raw_post = MonthlyPlanPost(
                id=spec["id"],
                post_format=spec["post_format"],
                design_id=spec["design_id"],
                editorial_title=spec["editorial_title"],
                caption_en=spec["caption_en"],
                first_comment_en=spec.get("first_comment_en", ""),
                alt_text_en=spec.get("alt_text_en", ""),
                anniversary_note=spec.get("anniversary_note", ""),
                comparison_mode=spec.get("comparison_mode", "none"),
                scheduled_at=spec["scheduled_at"],
                artworks=tuple(arts),
                detail_focuses=tuple(spec["detail_focuses"]) if spec.get("detail_focuses") else None,
                slides=tuple(["", *existing_inner]) if existing_inner else (),
                slide_count=spec.get("slide_count", 4 if spec["post_format"] == "carousel" else 1),
                reel_motion_preset=spec.get("reel_motion_preset", "detail_to_full"),
                reel_kinetic_hook=spec.get("reel_kinetic_hook", ""),
                reel_zoom_start=tuple(spec["reel_zoom_start"]) if spec.get("reel_zoom_start") else None,
                reel_zoom_end=tuple(spec["reel_zoom_end"]) if spec.get("reel_zoom_end") else None,
                reel_video_path=spec.get("reel_video_path", ""),
                approval_status=spec.get("approval_status", "draft"),
                editorial_project_dir=spec.get("editorial_project_dir", ""),
                dimensions_cm_text=spec.get("dimensions_cm_text", ""),
                signature_day_label=spec.get("signature_day_label", ""),
            )
            rendered_posts.append(
                _render_monthly_post(stage, raw_post, preserve_inner_slides=bool(existing_inner))
            )

        history_posts: list[PriorProfilePost] = []
        for hspec in history_specs:
            src_img = Path(hspec["source_image"]).resolve()
            if not src_img.is_file():
                raise FileNotFoundError(f"Missing history image: {src_img}")
            rel_hist = f"history/{hspec['id']}.jpg"
            with Image.open(src_img) as im:
                im.convert("RGB").save(stage / rel_hist, format="JPEG", quality=90, optimize=True)
            history_posts.append(
                PriorProfilePost(
                    id=hspec["id"],
                    post_format=hspec.get("post_format", "carousel"),
                    title=hspec["title"],
                    artist=hspec["artist"],
                    date=hspec.get("date", ""),
                    institution=hspec.get("institution", ""),
                    museum_url=hspec.get("museum_url", ""),
                    caption_en=hspec.get("caption_en", ""),
                    image_path=rel_hist,
                    published_at_label=hspec.get("published_at_label", ""),
                )
            )

        c_cnt = sum(p.post_format == "carousel" for p in rendered_posts)
        r_cnt = sum(p.post_format == "reel" for p in rendered_posts)
        start_iso = (start_at or datetime.fromisoformat(rendered_posts[0].scheduled_at)).isoformat()

        state = MonthlyPlanState(
            profile_handle=profile_handle,
            days=days,
            start_at=start_iso,
            carousels_per_day=carousels_per_day,
            reels_per_day=reels_per_day,
            carousel_count=c_cnt,
            reel_count=r_cnt,
            posts=tuple(rendered_posts),
            schedule_order=tuple(p.id for p in rendered_posts),
            pinned_ids=tuple(pinned_ids),
            history_posts=tuple(history_posts),
        )
        save_monthly_plan_state(stage, state)
        _atomic_write(stage / "index.html", render_monthly_editor_html(state, editor_token=None))
        stage.rename(output)
    return state


def update_monthly_post_design(workspace_dir: Path, post_id: str, design_id: str) -> MonthlyPlanState:
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    updated: list[MonthlyPlanPost] = []
    found = False
    for post in state.posts:
        if post.id == post_id:
            found = True
            candidate = post.model_copy(update={"design_id": design_id})
            updated.append(_render_monthly_post(root, candidate, preserve_inner_slides=True))
        else:
            updated.append(post)
    if not found:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state


def render_monthly_reel_video(
    workspace_dir: Path,
    post_id: str,
    *,
    preset: ReelMotionPreset | None = None,
    zoom_start: tuple[float, float] | None = None,
    zoom_end: tuple[float, float] | None = None,
    seamless_loop: bool = True,
    kinetic_hook: str | None = None,
) -> tuple[MonthlyPlanState, dict]:
    """Render a real 1080x1920 H.264 MP4 for a scheduled Reel post and mark it video_ready."""
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    updated: list[MonthlyPlanPost] = []
    render_info: dict | None = None
    for post in state.posts:
        if post.id == post_id:
            if post.post_format != "reel":
                raise ValueError(f"Post '{post_id}' is a {post.post_format}, not a reel")
            chosen_preset: ReelMotionPreset = preset or post.reel_motion_preset
            z_start = zoom_start if zoom_start is not None else post.reel_zoom_start
            z_end = zoom_end if zoom_end is not None else post.reel_zoom_end
            hook = kinetic_hook if kinetic_hook is not None else (post.reel_kinetic_hook or None)
            sources = _sources_from_post(root, post)
            rel_video = f"videos/{post.id}-{chosen_preset}.mp4"
            render_info = render_local_reel_mp4(
                sources[0],
                post.editorial_title,
                root / rel_video,
                preset=chosen_preset,
                zoom_start=z_start,
                zoom_end=z_end,
                seamless_loop=seamless_loop,
                kinetic_hook=hook,
            )
            updated.append(
                post.model_copy(
                    update={
                        "reel_motion_preset": chosen_preset,
                        "reel_zoom_start": z_start,
                        "reel_zoom_end": z_end,
                        "reel_kinetic_hook": render_info.get("kinetic_hook", ""),
                        "reel_video_path": rel_video,
                        "reel_status": "video_ready",
                    }
                )
            )
        else:
            updated.append(post)
    if render_info is None:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state, render_info


def apply_split_comparison_mode(
    workspace_dir: Path,
    post_id: str,
    mode: Literal["none", "study_vs_final", "before_after_restoration"],
) -> MonthlyPlanState:
    """Switch a 2-artwork carousel between normal design cover and vertical Split-Screen Before/After comparison."""
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    updated: list[MonthlyPlanPost] = []
    found = False
    for post in state.posts:
        if post.id == post_id:
            found = True
            if mode != "none" and len(post.artworks) < 2:
                raise ValueError("Split-screen comparison requires a 2-artwork post")
            candidate = post.model_copy(update={"comparison_mode": mode})
            updated.append(_render_monthly_post(root, candidate, preserve_inner_slides=True))
        else:
            updated.append(post)
    if not found:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state


def reorder_plan_by_color_bridge(workspace_dir: Path) -> MonthlyPlanState:
    """Reorder scheduled posts via Color-Bridge pigment chaining while preserving the 3-column Reels spine."""
    root = Path(workspace_dir).resolve()
    create_plan_snapshot(root, label="before_color_bridge")
    state = load_monthly_plan_state(root)
    post_map = {p.id: p for p in state.posts}
    entries: list[dict] = []
    for pid in state.schedule_order:
        p = post_map[pid]
        cover_path = root / p.slides[0]
        pri, sec = extract_primary_secondary_rgb(cover_path) if cover_path.is_file() else ((128, 128, 128), (128, 128, 128))
        entries.append(
            {
                "id": p.id,
                "post_format": p.post_format,
                "primary_rgb": pri,
                "secondary_rgb": sec,
            }
        )
    new_order = sort_schedule_by_color_bridge(entries, preserve_format_slots=True)
    return update_monthly_schedule_order(root, schedule_order=new_order, pinned_ids=state.pinned_ids)


def reorder_post_slides(workspace_dir: Path, post_id: str, new_slides: Sequence[str]) -> MonthlyPlanState:
    """Reorder carousel slides (#20) while verifying exact membership."""
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    updated = []
    found = False
    for post in state.posts:
        if post.id == post_id:
            found = True
            if set(new_slides) != set(post.slides) or len(new_slides) != len(post.slides):
                raise ValueError("Reordered slides must contain the exact same slide files")
            updated.append(post.model_copy(update={"slides": tuple(new_slides)}))
        else:
            updated.append(post)
    if not found:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state


def mark_post_meta_uploaded(workspace_dir: Path, post_id: str, uploaded: bool = True) -> MonthlyPlanState:
    """Mark a post as uploaded in Meta Business Suite (#45)."""
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    updated = []
    found = False
    for post in state.posts:
        if post.id == post_id:
            found = True
            updated.append(post.model_copy(update={"meta_uploaded": bool(uploaded)}))
        else:
            updated.append(post)
    if not found:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state


def create_plan_snapshot(workspace_dir: Path, *, label: str = "manual") -> Path:
    """Save a local revision snapshot of monthly_plan.json for Undo/Restore (#49)."""
    root = Path(workspace_dir).resolve()
    snap_dir = root / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    src = root / "monthly_plan.json"
    if not src.is_file():
        raise FileNotFoundError(f"Cannot snapshot missing state: {src}")
    stamp = datetime.now(ISTANBUL_TZ).strftime("%Y%m%d-%H%M%S-%f")
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "", label) or "snap"
    dest = snap_dir / f"{stamp}_{safe_label}.json"
    shutil.copyfile(src, dest)
    return dest


def restore_latest_snapshot(workspace_dir: Path) -> MonthlyPlanState:
    """Restore the most recent saved local snapshot (#49)."""
    root = Path(workspace_dir).resolve()
    snap_dir = root / "snapshots"
    snaps = sorted(snap_dir.glob("*.json")) if snap_dir.is_dir() else []
    if not snaps:
        raise FileNotFoundError("No local snapshots available to restore")
    latest = snaps[-1]
    restored = MonthlyPlanState.model_validate_json(latest.read_text(encoding="utf-8"))
    save_monthly_plan_state(root, restored)
    return restored


def attach_monthly_reel_video(workspace_dir: Path, post_id: str, video_path: str | Path) -> MonthlyPlanState:
    """Attach an external verified MP4/MOV video file to a scheduled Reel post."""
    root = Path(workspace_dir).resolve()
    src = Path(video_path).resolve()
    verify_reel_mp4(src)
    state = load_monthly_plan_state(root)
    updated: list[MonthlyPlanPost] = []
    found = False
    for post in state.posts:
        if post.id == post_id:
            if post.post_format != "reel":
                raise ValueError(f"Post '{post_id}' is not a reel")
            found = True
            rel_video = f"videos/{post.id}-attached{src.suffix.lower() or '.mp4'}"
            (root / "videos").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, root / rel_video)
            updated.append(post.model_copy(update={"reel_video_path": rel_video, "reel_status": "video_ready"}))
        else:
            updated.append(post)
    if not found:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state


def update_monthly_schedule_order(
    workspace_dir: Path,
    *,
    schedule_order: list[str] | tuple[str, ...],
    pinned_ids: list[str] | tuple[str, ...],
) -> MonthlyPlanState:
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    slots = build_daily_cadence_schedule(
        datetime.fromisoformat(state.start_at),
        days=state.days,
        carousels_per_day=state.carousels_per_day,
        reels_per_day=state.reels_per_day,
    )
    timestamps = [ts for _, ts in slots[: len(schedule_order)]]
    if len(timestamps) < len(schedule_order):
        last_dt = datetime.fromisoformat(timestamps[-1]) if timestamps else datetime.fromisoformat(state.start_at)
        while len(timestamps) < len(schedule_order):
            last_dt += timedelta(hours=4)
            timestamps.append(last_dt.isoformat())
    ts_by_id = dict(zip(schedule_order, timestamps, strict=True))
    updated_posts = tuple(post.model_copy(update={"scheduled_at": ts_by_id[post.id]}) for post in state.posts)
    new_state = MonthlyPlanState.model_validate(
        {
            **state.model_dump(mode="json"),
            "posts": [p.model_dump(mode="json") for p in updated_posts],
            "schedule_order": list(schedule_order),
            "pinned_ids": list(pinned_ids),
        }
    )
    save_monthly_plan_state(root, new_state)
    return new_state


def update_monthly_approval(
    workspace_dir: Path,
    *,
    post_id: str | None = None,
    approval_status: Literal["approved", "draft"] = "approved",
    approve_all: bool = False,
) -> MonthlyPlanState:
    root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(root)
    updated = []
    found = approve_all
    for post in state.posts:
        if approve_all or post.id == post_id:
            found = True
            updated.append(post.model_copy(update={"approval_status": approval_status}))
        else:
            updated.append(post)
    if not found:
        raise KeyError(f"Unknown post id: {post_id}")
    new_state = state.model_copy(update={"posts": tuple(updated)})
    save_monthly_plan_state(root, new_state)
    return new_state


def adjust_monthly_plan_cadence(
    workspace_dir: Path,
    *,
    days: int,
    carousels_per_day: int,
    reels_per_day: int,
) -> MonthlyPlanState:
    """Re-balance the Carousel + Reels schedule (0 singles), auto-snapshotting first (#49)."""
    root = Path(workspace_dir).resolve()
    create_plan_snapshot(root, label="before_replan")
    state = load_monthly_plan_state(root)
    slots = build_daily_cadence_schedule(
        datetime.fromisoformat(state.start_at),
        days=days,
        carousels_per_day=carousels_per_day,
        reels_per_day=reels_per_day,
    )

    by_fmt: dict[str, list[MonthlyPlanPost]] = {"carousel": [], "reel": []}
    post_map = {p.id: p for p in state.posts}
    for pid in state.schedule_order:
        p = post_map[pid]
        by_fmt[p.post_format].append(p)

    donor_pool = [post_map[pid] for pid in state.schedule_order]
    new_posts: list[MonthlyPlanPost] = []
    used_ids: set[str] = set()
    next_num = len(state.posts) + 1
    presets: tuple[ReelMotionPreset, ...] = ("detail_to_full", "slow_pan_horizontal", "palette_reveal", "chiaroscuro_zoom")

    for idx, (fmt, ts) in enumerate(slots):
        if by_fmt[fmt]:
            existing = by_fmt[fmt].pop(0)
            used_ids.add(existing.id)
            new_posts.append(existing.model_copy(update={"scheduled_at": ts}))
        else:
            donor = donor_pool[idx % len(donor_pool)]
            new_id = f"post-{next_num:03d}"
            while new_id in used_ids:
                next_num += 1
                new_id = f"post-{next_num:03d}"
            next_num += 1
            used_ids.add(new_id)
            art1 = donor.artworks[0]
            candidate = MonthlyPlanPost(
                id=new_id,
                post_format=fmt,
                design_id="astra-013" if fmt == "carousel" else "astra-083",
                editorial_title=donor.editorial_title,
                caption_en=donor.caption_en,
                scheduled_at=ts,
                artworks=(art1,),
                slide_count=4 if fmt == "carousel" else 1,
                reel_motion_preset=presets[idx % len(presets)],
                approval_status="draft",
            )
            new_posts.append(_render_monthly_post(root, candidate, preserve_inner_slides=False))

    valid_ids = {p.id for p in new_posts}.union(h.id for h in state.history_posts)
    kept_pins = tuple(pid for pid in state.pinned_ids if pid in valid_ids)[:MAX_PINNED_POSTS]

    new_state = MonthlyPlanState(
        profile_handle=state.profile_handle,
        days=days,
        start_at=state.start_at,
        carousels_per_day=carousels_per_day,
        reels_per_day=reels_per_day,
        carousel_count=sum(p.post_format == "carousel" for p in new_posts),
        reel_count=sum(p.post_format == "reel" for p in new_posts),
        posts=tuple(new_posts),
        schedule_order=tuple(p.id for p in new_posts),
        pinned_ids=kept_pins,
        history_posts=state.history_posts,
    )
    save_monthly_plan_state(root, new_state)
    return new_state


def _state_api_payload(state: MonthlyPlanState) -> dict:
    moments = []
    for k in range(1, len(state.schedule_order) + 1):
        pid = state.schedule_order[k - 1]
        p = next(item for item in state.posts if item.id == pid)
        dt = datetime.fromisoformat(p.scheduled_at).astimezone(ISTANBUL_TZ)
        moments.append(
            {
                "moment": k,
                "post_id": pid,
                "label": f"Slot {k:03d} · {dt.strftime('%d.%m %H:%M')} · {p.post_format.upper()}",
                "cells": compute_profile_at_moment(state, moment_index=k)[:9],
            }
        )
    full_cells = compute_profile_at_moment(state, moment_index=None)
    return {
        "days": state.days,
        "carousels_per_day": state.carousels_per_day,
        "reels_per_day": state.reels_per_day,
        "carousel_count": state.carousel_count,
        "reel_count": state.reel_count,
        "schedule_order": list(state.schedule_order),
        "pinned_ids": list(state.pinned_ids),
        "full_cells": full_cells,
        "moments": moments,
        "rhythm": analyze_grid_rhythm([c for c in full_cells if c["kind"] == "scheduled"]),
        "tempo_profiles": TEMPO_PROFILES,
    }


def render_monthly_editor_html(
    state: MonthlyPlanState,
    *,
    editor_token: str | None,
) -> str:
    """Render the interactive Carousel + Reels 6/day studio with 280 designs, 9:16 Reel cover & local MP4 renderer."""
    catalog = load_design_catalog()
    usable = list_usable_designs()
    payload_obj = {
        "token": editor_token,
        "profile_handle": state.profile_handle,
        **_state_api_payload(state),
        "reel_presets": REEL_MOTION_PRESETS,
        "usable_designs": [
            {
                "id": d.id,
                "name": d.name,
                "collection": d.collection,
                "family": d.family,
                "family_name": d.family_name,
                "required_sources": d.required_sources,
            }
            for d in usable
        ],
        "catalog_counts": {
            "total": len(catalog.designs),
            "astra": sum(d.collection == "astra" for d in catalog.designs),
            "neon": sum(d.collection == "neon" for d in catalog.designs),
            "ready": len(usable),
        },
    }
    payload = json.dumps(payload_obj, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")

    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artfolio Stüdyo · 4 Kaydırmalı + 2 Reels / Gün (280 Tasarım)</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f1e8;color:#1b1a17;font:14px/1.5 system-ui,-apple-system,sans-serif}}
header{{background:#17231d;color:#f5edd8;padding:15px 24px;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px}}
header h1{{margin:0;font:21px/1.2 Georgia,serif;font-weight:normal}}
header p{{margin:4px 0 0;font-size:12px;color:#cfc4a8}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;background:#2b4034;border:1px solid #486755;color:#f5edd8;margin-left:6px}}
.bar{{background:#fff;border-bottom:1px solid #dbd4c3;padding:10px 24px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}}
.plan-form{{display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
.plan-form label{{font-size:12px;font-weight:600;display:flex;align-items:center;gap:5px}}
.plan-form input{{width:56px;padding:5px 7px;border:1px solid #bfb7a4;border-radius:6px;font:13px system-ui}}
.btn{{padding:7px 12px;border-radius:7px;border:1px solid #1e2d25;background:#17231d;color:#fff9ed;font:13px system-ui;cursor:pointer}}
.btn.sec{{background:#fff;color:#1b1a17;border-color:#bfb7a4}}
.btn.gold{{background:#a96c22;border-color:#8a5516;color:#fff}}
.btn:disabled{{opacity:.45;cursor:default}}
.shell{{max-width:1460px;margin:18px auto 48px;padding:0 18px;display:grid;grid-template-columns:minmax(0,1.35fr) minmax(360px,.95fr);gap:22px;align-items:start}}
@media(max-width:1024px){{.shell{{grid-template-columns:1fr}}}}
.panel{{background:#fff;border:1px solid #dcd5c5;border-radius:14px;padding:16px;box-shadow:0 6px 24px rgba(20,18,14,.05)}}
.tabs{{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;justify-content:space-between}}
.tab-group{{display:flex;gap:6px}}
.tab{{padding:6px 12px;border-radius:8px;border:1px solid #c9c1ae;background:#faf8f2;cursor:pointer;font:13px system-ui}}
.tab.active{{background:#17231d;color:#fff9ed;border-color:#17231d}}
.moment-bar{{background:#faf7ef;border:1px solid #e4dccb;border-radius:10px;padding:10px 12px;margin-bottom:12px;display:none}}
.moment-bar.show{{display:block}}
.moment-bar input[type=range]{{width:100%}}
.scroll-grid{{max-height:74vh;overflow-y:auto;padding-right:4px}}
.scroll-grid.mobile-375{{max-width:375px;margin:0 auto;border:2px solid #2b4034;border-radius:16px;padding:8px;background:#faf8f2}}
.grid-3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}}
.cell{{position:relative;aspect-ratio:4/5;background:#eae4d6;overflow:hidden;cursor:pointer;border:2px solid transparent;padding:0;text-align:left}}
.cell.selected{{border-color:#1c533e;box-shadow:0 0 0 2px #1c533e}}
.cell img{{display:block;width:100%;height:100%;object-fit:cover}}
.heat-overlay{{position:absolute;inset:0;display:none;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.9);pointer-events:none;flex-direction:column;gap:2px}}
.heatmap-on .heat-overlay{{display:flex}}
.heat-dark{{background:rgba(15,24,42,.58)}}
.heat-mid{{background:rgba(128,95,40,.46)}}
.heat-light{{background:rgba(218,200,165,.48);color:#141311;text-shadow:none}}
.tags{{position:absolute;top:5px;left:5px;right:5px;display:flex;justify-content:space-between;gap:3px;pointer-events:none}}
.tag{{background:rgba(18,18,16,.82);color:#fff9ed;font-size:10px;padding:2px 5px;border-radius:4px;text-transform:uppercase}}
.tag.pin{{background:#b57c2a}}
.tag.ok{{background:#1f6e4a}}
.tag.done{{background:#1c4d7a}}
.tag.draft{{background:#8c3d2b}}
.tag.reel{{background:#5d2b6e}}
.foot{{position:absolute;left:0;right:0;bottom:0;padding:16px 6px 5px;background:linear-gradient(transparent,rgba(14,14,12,.86));color:#fff9ed;font-size:10.5px;line-height:1.25}}
.inspector{{position:sticky;top:14px}}
.inspector h2{{margin:0 0 4px;font:20px/1.25 Georgia,serif;font-weight:normal}}
.meta{{font-size:12px;color:#5a554a;margin:0 0 10px}}
.qa-box{{background:#fff3e0;border:1px solid #e2b97e;color:#6c4212;border-radius:8px;padding:6px 9px;font-size:12px;margin-bottom:8px;display:none}}
.qa-box.show{{display:block}}
.hero{{width:100%;max-height:330px;object-fit:contain;background:#f0ebe0;border-radius:8px;display:block;margin-bottom:8px}}
.reel-box{{background:#f7f2fa;border:1px solid #d9c9e3;border-radius:10px;padding:10px 12px;margin-bottom:10px;display:none}}
.reel-box.show{{display:block}}
.reel-box video{{width:100%;max-height:260px;border-radius:6px;background:#000;margin-top:8px}}
.zoom-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:6px}}
.zoom-grid label{{font-size:11px;display:flex;flex-direction:column;gap:2px}}
.zoom-grid input{{padding:4px 6px;border:1px solid #c5bca8;border-radius:5px;font:12px system-ui;width:100%}}
.slides-row{{display:flex;align-items:center;gap:6px;margin-bottom:10px}}
.slides-strip{{display:flex;gap:6px;overflow-x:auto;flex:1}}
.slides-strip img{{height:64px;width:auto;border-radius:5px;border:2px solid transparent;cursor:pointer}}
.slides-strip img.active{{border-color:#1c533e}}
.art-box{{background:#faf8f3;border:1px solid #e6dfd0;border-radius:8px;padding:8px 10px;font-size:12.5px;margin-bottom:10px}}
.art-box a{{color:#1d4c39}}
.filter-row{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:6px}}
select,textarea{{width:100%;padding:7px 9px;border:1px solid #c5bca8;border-radius:7px;font:13px system-ui;background:#fff}}
textarea{{min-height:80px;background:#faf8f3;resize:vertical;margin-top:6px}}
.actions{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
.status{{margin-top:8px;font-size:12.5px;color:#1f4b37;min-height:20px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>Artfolio · Kaydırmalı & Reels Stüdyosu (Tekli Kapalı)</h1>
    <p>Günde 6 gönderi (4 Kaydırmalı + 2 Reels · orta sütun Reels omurgası) · Yalnız yerel önizleme.</p>
  </div>
  <div>
    <span class="pill">100 Astra + 180 Neon = 280 Tasarım</span>
    <span class="pill" id="approved-pill">0 onaylı</span>
    <span class="pill" id="rhythm-pill">Ritim: —</span>
  </div>
</header>

<div class="bar">
  <div class="plan-form">
    <label>Tempo Profili
      <select id="sel-tempo-profile" style="width:auto">
        <option value="custom">Özel</option>
        <option value="2_per_day">2/gün (1K + 1R)</option>
        <option value="4_per_day">4/gün (3K + 1R)</option>
        <option value="6_per_day" selected>6/gün (4K + 2R)</option>
      </select>
    </label>
    <label>Gün <input id="in-days" type="number" min="1" max="40" value="{state.days}"></label>
    <label>Kaydırmalı / gün <input id="in-cday" type="number" min="0" max="8" value="{state.carousels_per_day}"></label>
    <label>Reels / gün <input id="in-rday" type="number" min="0" max="6" value="{state.reels_per_day}"></label>
    <button type="button" class="btn sec" id="btn-replan">Tempoyu Güncelle</button>
    <button type="button" class="btn sec" id="btn-color-bridge">Renk Köprüsü Sırala</button>
    <button type="button" class="btn sec" id="btn-snapshot">Yedek Al</button>
    <button type="button" class="btn sec" id="btn-restore">Geri Al (Undo)</button>
  </div>
  <div class="plan-form">
    <button type="button" class="btn sec" id="btn-approve-all">Tümünü Onayla</button>
    <label style="font-weight:500"><input id="chk-split-weeks" type="checkbox" style="width:auto"> 4 Haftaya Böl</label>
    <button type="button" class="btn gold" id="btn-export-meta">Meta Business Suite Paketi Dışa Aktar</button>
  </div>
</div>

<div class="shell">
  <section class="panel">
    <div class="tabs">
      <div class="tab-group">
        <button type="button" class="tab active" id="tab-month">Tüm Plan</button>
        <button type="button" class="tab" id="tab-today">Bugünün 6 Gönderisi</button>
        <button type="button" class="tab" id="tab-moment">Yayın Anında 3×3</button>
      </div>
      <div class="tab-group">
        <button type="button" class="tab" id="btn-heatmap">Isı Haritası</button>
        <button type="button" class="tab" id="btn-mobile-preview">Mobil 375px</button>
      </div>
      <div class="meta" id="grid-meta"></div>
    </div>

    <div class="moment-bar" id="moment-bar">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <strong id="moment-label">Yayın anı</strong>
        <span class="meta">Seçili yayın anındaki gerçek 3×3 profil</span>
      </div>
      <input type="range" id="moment-slider" min="1" max="{len(state.schedule_order)}" value="{len(state.schedule_order)}">
    </div>

    <div class="scroll-grid" id="scroll-grid">
      <div class="grid-3" id="grid-3"></div>
    </div>
  </section>

  <aside class="panel inspector">
    <h2 id="ins-title">Bir gönderi seçin</h2>
    <p class="meta" id="ins-meta"></p>
    <div class="qa-box" id="ann-box" style="background:#f5ecd6;border-color:#c9a14a;color:#4a3508"></div>
    <div class="qa-box" id="qa-box"></div>
    <img class="hero" id="ins-hero" alt="Önizleme">
    <div class="slides-row" id="slides-row" style="display:none">
      <div class="slides-strip" id="slides-strip"></div>
      <div style="display:flex;flex-direction:column;gap:4px">
        <button type="button" class="btn sec" id="btn-slide-left" title="Slaytı sola al">←</button>
        <button type="button" class="btn sec" id="btn-slide-right" title="Slaytı sağa al">→</button>
      </div>
    </div>

    <div class="reel-box" id="reel-box">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <strong>Reels Stüdyosu (9:16 + 4:5 Kırpım & MP4)</strong>
        <span class="meta" id="reel-status-txt" style="margin:0"></span>
      </div>
      <div class="actions" style="margin-top:0;margin-bottom:8px">
        <button type="button" class="btn sec" id="btn-cover-4x5">4:5 Grid Kapağı</button>
        <button type="button" class="btn sec" id="btn-cover-9x16">9:16 Reels Kapağı</button>
      </div>
      <select id="reel-preset-select"></select>
      <div class="zoom-grid">
        <label>Başlangıç X <input id="zoom-sx" type="number" step="0.05" min="0" max="1" placeholder="oto"></label>
        <label>Başlangıç Y <input id="zoom-sy" type="number" step="0.05" min="0" max="1" placeholder="oto"></label>
        <label>Bitiş X <input id="zoom-ex" type="number" step="0.05" min="0" max="1" placeholder="0.50"></label>
        <label>Bitiş Y <input id="zoom-ey" type="number" step="0.05" min="0" max="1" placeholder="0.50"></label>
      </div>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin-top:6px">
        <input id="chk-seamless-loop" type="checkbox" checked style="width:auto"> Kusursuz Döngü (Seamless Loop)
      </label>
      <label style="display:block;font-size:11.5px;margin-top:6px">
        3 sn Dinamik Alt Yazı (Sessiz İzleyici Kancası)
        <input id="in-kinetic-hook" type="text" maxlength="110" placeholder="Otomatik küratör notu…" style="width:100%;padding:5px 8px;border:1px solid #c5bca8;border-radius:6px;margin-top:3px;font:12.5px system-ui">
      </label>
      <div class="actions">
        <button type="button" class="btn" id="btn-render-reel">1080×1920 MP4 Reels Üret</button>
      </div>
      <video id="reel-video" controls style="display:none"></video>
    </div>

    <div class="art-box" id="ins-artworks"></div>

    <div id="draft-controls">
      <div class="filter-row">
        <select id="filter-collection">
          <option value="all">Koleksiyon: Tümü (280)</option>
          <option value="astra">Astra (100)</option>
          <option value="neon">Neon (180)</option>
        </select>
        <select id="filter-family"><option value="all">Aile: Tümü</option></select>
      </div>
      <select id="design-select"></select>
      <div class="actions">
        <button type="button" class="btn" id="btn-apply">Tasarımı Uygula</button>
        <button type="button" class="btn sec" id="btn-split-study" style="display:none">Eskiz / Final Bölünmüş</button>
        <button type="button" class="btn sec" id="btn-split-restore" style="display:none">Restorasyon Önce / Sonra</button>
        <button type="button" class="btn sec" id="btn-approve">Onayla / Taslak</button>
        <button type="button" class="btn sec" id="btn-uploaded">Meta'ya Yüklendi</button>
        <button type="button" class="btn sec" id="btn-pin">Sabitle (max 3)</button>
        <button type="button" class="btn sec" id="btn-earlier">Önceki Slot</button>
        <button type="button" class="btn sec" id="btn-later">Sonraki Slot</button>
      </div>
    </div>

    <div class="actions">
      <button type="button" class="btn sec" id="btn-copy-caption">Açıklamayı Kopyala</button>
      <button type="button" class="btn sec" id="btn-copy-comment">İlk Yorumu Kopyala</button>
      <button type="button" class="btn sec" id="btn-copy-alt">Alt-Text Kopyala</button>
    </div>
    <textarea id="ins-caption" readonly placeholder="İngilizce gönderi açıklaması"></textarea>
    <textarea id="ins-first-comment" readonly style="min-height:60px" placeholder="İlk yorum (müze künyesi ve katalog bağlantısı)"></textarea>
    <textarea id="ins-alt-text" readonly style="min-height:48px" placeholder="SEO & Erişilebilirlik Alt-Text (Meta Business Suite)"></textarea>
    <div class="status" id="status" role="status" aria-live="polite"></div>
  </aside>
</div>

<script type="application/json" id="monthly-data">{payload}</script>
<script>
let data = JSON.parse(document.getElementById('monthly-data').textContent);
let viewMode = 'month';
let currentMoment = data.moments.length;
let selectedId = data.full_cells.length ? data.full_cells[0].id : null;
let selectedSlideIndex = 0;

const scrollGrid = document.getElementById('scroll-grid');
const gridEl = document.getElementById('grid-3');
const gridMeta = document.getElementById('grid-meta');
const approvedPill = document.getElementById('approved-pill');
const rhythmPill = document.getElementById('rhythm-pill');
const momentBar = document.getElementById('moment-bar');
const momentSlider = document.getElementById('moment-slider');
const momentLabel = document.getElementById('moment-label');
const insTitle = document.getElementById('ins-title');
const insMeta = document.getElementById('ins-meta');
const annBox = document.getElementById('ann-box');
const qaBox = document.getElementById('qa-box');
const insHero = document.getElementById('ins-hero');
const slidesRow = document.getElementById('slides-row');
const slidesStrip = document.getElementById('slides-strip');
const reelBox = document.getElementById('reel-box');
const reelStatusTxt = document.getElementById('reel-status-txt');
const reelPresetSel = document.getElementById('reel-preset-select');
const reelVideo = document.getElementById('reel-video');
const insArtworks = document.getElementById('ins-artworks');
const insCaption = document.getElementById('ins-caption');
const insFirstComment = document.getElementById('ins-first-comment');
const insAltText = document.getElementById('ins-alt-text');
const filterCol = document.getElementById('filter-collection');
const filterFam = document.getElementById('filter-family');
const designSelect = document.getElementById('design-select');
const draftControls = document.getElementById('draft-controls');
const statusEl = document.getElementById('status');

for (const [slug, name] of [...new Map(data.usable_designs.map(d => [d.family, d.family_name])).entries()]) {{
  const opt = document.createElement('option'); opt.value = slug; opt.textContent = name; filterFam.appendChild(opt);
}}
for (const [k, info] of Object.entries(data.reel_presets)) {{
  const opt = document.createElement('option'); opt.value = k; opt.textContent = info.label + ' (' + info.duration_sec + 'sn)'; reelPresetSel.appendChild(opt);
}}

function activeCells() {{
  if (viewMode === 'moment' && data.moments.length) {{
    const idx = Math.max(0, Math.min(data.moments.length - 1, currentMoment - 1));
    momentLabel.textContent = data.moments[idx].label;
    return data.moments[idx].cells;
  }}
  if (viewMode === 'today') {{
    const perDay = Math.max(1, (data.carousels_per_day || 4) + (data.reels_per_day || 2));
    const firstDayIds = new Set(data.schedule_order.slice(0, perDay));
    return data.full_cells.filter(c => c.kind === 'scheduled' && firstDayIds.has(c.id));
  }}
  return data.full_cells;
}}

function renderGrid() {{
  const cells = activeCells();
  gridEl.innerHTML = '';
  const approvedCount = data.full_cells.filter(c => c.kind === 'scheduled' && c.approval_status === 'approved').length;
  const uploadedCount = data.full_cells.filter(c => c.kind === 'scheduled' && c.meta_uploaded).length;
  approvedPill.textContent = approvedCount + '/' + data.schedule_order.length + ' onaylı · ' + uploadedCount + ' yüklendi';
  if (data.rhythm) {{
    const warns = (data.rhythm.tone_clump_rows || []).length + (data.rhythm.type_crowded_rows || []).length;
    rhythmPill.textContent = 'Ritim: ' + (data.rhythm.balanced ? 'Dengeli' : warns + ' uyarı') + ' · Orta Reels %' + Math.round((data.rhythm.center_reel_ratio || 0) * 100);
  }}
  gridMeta.textContent = data.days + ' gün · ' + data.carousel_count + ' Kaydırmalı + ' + data.reel_count + ' Reels · ' + data.pinned_ids.length + '/3 sabit';

  for (const cell of cells) {{
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cell' + (cell.id === selectedId ? ' selected' : '');
    const leftBadge = cell.pinned
      ? '<span class="tag pin">SABİT</span>'
      : (cell.meta_uploaded
          ? '<span class="tag done">YÜKLENDİ</span>'
          : (cell.kind === 'scheduled'
              ? (cell.approval_status === 'approved' ? '<span class="tag ok">ONAYLI</span>' : '<span class="tag draft">TASLAK</span>')
              : '<span class="tag">GEÇMİŞ</span>'));
    const rightBadge = cell.post_format === 'reel'
      ? ('<span class="tag reel">' + (cell.reel_status === 'video_ready' ? 'REEL MP4' : 'REEL TASLAK') + '</span>')
      : ('<span class="tag">1/' + cell.slide_count + '</span>');
    const toneClass = cell.tone_band === 'dark' ? 'heat-dark' : (cell.tone_band === 'light' ? 'heat-light' : 'heat-mid');
    const heatHtml = '<div class="heat-overlay ' + toneClass + '"><span>' + (cell.tone_band || 'mid').toUpperCase() + ' · ' + (cell.temp_band || 'neutral').toUpperCase() + '</span><span>L=' + (cell.luminance ?? 128) + '</span></div>';
    btn.innerHTML =
      '<img src="' + cell.image_path + '" alt="">' +
      heatHtml +
      '<div class="tags">' + leftBadge + rightBadge + '</div>' +
      '<div class="foot">' + cell.scheduled_label + (cell.design_id ? ' · ' + cell.design_id : '') + '<br>' + cell.editorial_title + '</div>';
    btn.addEventListener('click', () => {{ selectedId = cell.id; selectedSlideIndex = 0; renderGrid(); renderInspector(); }});
    gridEl.appendChild(btn);
  }}
}}

function populateDesignOptions(cell) {{
  const req = cell.artworks.length;
  designSelect.innerHTML = '';
  for (const d of data.usable_designs) {{
    if (d.required_sources !== req) continue;
    if (filterCol.value !== 'all' && d.collection !== filterCol.value) continue;
    if (filterFam.value !== 'all' && d.family !== filterFam.value) continue;
    const opt = document.createElement('option');
    opt.value = d.id;
    opt.textContent = d.id + ' — ' + d.name + ' [' + d.collection.toUpperCase() + ' · ' + d.family_name + ']';
    if (d.id === cell.design_id) opt.selected = true;
    designSelect.appendChild(opt);
  }}
}}

function renderInspector() {{
  const cell = data.full_cells.find(c => c.id === selectedId) || activeCells()[0];
  if (!cell) return;
  selectedId = cell.id;
  insTitle.textContent = cell.editorial_title;
  const sig = cell.signature_day_label ? (' · ' + cell.signature_day_label) : '';
  insMeta.textContent = cell.scheduled_label + ' · ' + cell.post_format.toUpperCase() + (cell.design_id ? ' · ' + cell.design_id : '') + sig;
  annBox.classList.toggle('show', Boolean(cell.anniversary_note));
  annBox.textContent = cell.anniversary_note || '';
  const warns = cell.qa_warnings || [];
  qaBox.classList.toggle('show', warns.length > 0);
  qaBox.textContent = warns.length ? ('QA Uyarıları: ' + warns.join(' | ')) : '';

  const slides = cell.slides || [];
  if (selectedSlideIndex >= slides.length) selectedSlideIndex = 0;
  insHero.src = slides.length ? slides[selectedSlideIndex] : cell.image_path;
  insCaption.value = cell.caption_en || '';
  insFirstComment.value = cell.first_comment_en || '';
  insAltText.value = cell.alt_text_en || '';

  slidesStrip.innerHTML = '';
  if (slides.length > 1) {{
    slidesRow.style.display = 'flex';
    slides.forEach((src, i) => {{
      const img = document.createElement('img');
      img.src = src; img.className = i === selectedSlideIndex ? 'active' : '';
      img.addEventListener('click', () => {{ selectedSlideIndex = i; insHero.src = src; [...slidesStrip.children].forEach((el, idx) => el.classList.toggle('active', idx === i)); }});
      slidesStrip.appendChild(img);
    }});
  }} else {{
    slidesRow.style.display = 'none';
  }}

  const isReel = cell.kind === 'scheduled' && cell.post_format === 'reel';
  reelBox.classList.toggle('show', isReel);
  if (isReel) {{
    reelPresetSel.value = cell.reel_motion_preset || 'detail_to_full';
    reelStatusTxt.textContent = cell.reel_status === 'video_ready' ? 'MP4 Hazır' : 'MP4 Bekleniyor (Taslak)';
    document.getElementById('btn-render-reel').disabled = !data.token;
    document.getElementById('zoom-sx').value = cell.reel_zoom_start ? cell.reel_zoom_start[0] : '';
    document.getElementById('zoom-sy').value = cell.reel_zoom_start ? cell.reel_zoom_start[1] : '';
    document.getElementById('zoom-ex').value = cell.reel_zoom_end ? cell.reel_zoom_end[0] : '';
    document.getElementById('zoom-ey').value = cell.reel_zoom_end ? cell.reel_zoom_end[1] : '';
    document.getElementById('in-kinetic-hook').value = cell.reel_kinetic_hook || '';
    if (cell.reel_video_path) {{
      reelVideo.src = cell.reel_video_path;
      reelVideo.style.display = 'block';
    }} else {{
      reelVideo.style.display = 'none';
      reelVideo.removeAttribute('src');
    }}
  }}

  insArtworks.innerHTML = cell.artworks.map(a =>
    '<div><strong>' + a.title + '</strong> — ' + a.artist + (a.date ? ' (' + a.date + ')' : '') +
    (a.museum_url ? ' · <a href="' + a.museum_url + '" target="_blank" rel="noopener">Müze ↗</a>' : '') + '</div>'
  ).join('');

  const isSched = cell.kind === 'scheduled';
  const live = Boolean(data.token);
  draftControls.style.display = isSched ? 'block' : 'none';
  document.getElementById('btn-replan').disabled = !live;
  document.getElementById('btn-color-bridge').disabled = !live;
  document.getElementById('btn-snapshot').disabled = !live;
  document.getElementById('btn-restore').disabled = !live;
  document.getElementById('btn-approve-all').disabled = !live;
  document.getElementById('btn-export-meta').disabled = !live;
  document.getElementById('btn-slide-left').disabled = !live || !isSched || slides.length < 2;
  document.getElementById('btn-slide-right').disabled = !live || !isSched || slides.length < 2;

  if (isSched) {{
    populateDesignOptions(cell);
    const hasTwo = cell.post_format === 'carousel' && cell.artworks && cell.artworks.length === 2;
    document.getElementById('btn-split-study').style.display = hasTwo ? 'inline-block' : 'none';
    document.getElementById('btn-split-restore').style.display = hasTwo ? 'inline-block' : 'none';
    document.getElementById('btn-split-study').disabled = !live;
    document.getElementById('btn-split-restore').disabled = !live;
    document.getElementById('btn-apply').disabled = !live;
    document.getElementById('btn-approve').disabled = !live;
    document.getElementById('btn-approve').textContent = cell.approval_status === 'approved' ? 'Taslağa Çek' : 'Gönderiyi Onayla';
    document.getElementById('btn-uploaded').disabled = !live;
    document.getElementById('btn-uploaded').textContent = cell.meta_uploaded ? 'Yüklendi ✓' : "Meta'ya Yüklendi";
    document.getElementById('btn-pin').disabled = !live;
    document.getElementById('btn-pin').textContent = cell.pinned ? 'Sabitlemeyi Kaldır' : 'Üste Sabitle';
    const idx = data.schedule_order.indexOf(cell.id);
    document.getElementById('btn-earlier').disabled = !live || idx <= 0;
    document.getElementById('btn-later').disabled = !live || idx < 0 || idx >= data.schedule_order.length - 1;
  }}
}}

async function api(url, body) {{
  const res = await fetch(url, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'X-Artfolio-Editor': data.token }},
    body: JSON.stringify(body),
  }});
  const out = await res.json();
  if (!res.ok) throw new Error(out.error || 'İşlem başarısız');
  if (out.full_cells) {{
    Object.assign(data, out);
    momentSlider.max = String(data.moments.length);
    if (currentMoment > data.moments.length) currentMoment = data.moments.length;
    momentSlider.value = String(currentMoment);
    renderGrid();
    renderInspector();
  }}
  return out;
}}

function setActiveTab(id) {{
  ['tab-month', 'tab-today', 'tab-moment'].forEach(t => document.getElementById(t).classList.toggle('active', t === id));
}}
document.getElementById('tab-month').addEventListener('click', () => {{ viewMode = 'month'; setActiveTab('tab-month'); momentBar.classList.remove('show'); renderGrid(); }});
document.getElementById('tab-today').addEventListener('click', () => {{ viewMode = 'today'; setActiveTab('tab-today'); momentBar.classList.remove('show'); renderGrid(); }});
document.getElementById('tab-moment').addEventListener('click', () => {{ viewMode = 'moment'; setActiveTab('tab-moment'); momentBar.classList.add('show'); renderGrid(); }});
document.getElementById('btn-heatmap').addEventListener('click', (e) => {{
  gridEl.classList.toggle('heatmap-on');
  e.target.classList.toggle('active', gridEl.classList.contains('heatmap-on'));
}});
document.getElementById('btn-mobile-preview').addEventListener('click', (e) => {{
  scrollGrid.classList.toggle('mobile-375');
  e.target.classList.toggle('active', scrollGrid.classList.contains('mobile-375'));
}});
document.getElementById('sel-tempo-profile').addEventListener('change', (e) => {{
  const p = (data.tempo_profiles || {{}})[e.target.value];
  if (p) {{
    document.getElementById('in-cday').value = p.carousels_per_day;
    document.getElementById('in-rday').value = p.reels_per_day;
  }}
}});
momentSlider.addEventListener('input', () => {{ currentMoment = Number(momentSlider.value); renderGrid(); }});
filterCol.addEventListener('change', renderInspector);
filterFam.addEventListener('change', renderInspector);

document.getElementById('btn-cover-4x5').addEventListener('click', () => {{
  const cell = data.full_cells.find(c => c.id === selectedId);
  if (cell) insHero.src = cell.image_path;
}});
document.getElementById('btn-cover-9x16').addEventListener('click', () => {{
  const cell = data.full_cells.find(c => c.id === selectedId);
  if (cell && cell.reel_cover_9x16_path) insHero.src = cell.reel_cover_9x16_path;
}});

async function moveSlide(delta) {{
  const cell = data.full_cells.find(c => c.id === selectedId);
  if (!cell || !cell.slides || cell.slides.length < 2) return;
  const next = selectedSlideIndex + delta;
  if (next < 0 || next >= cell.slides.length) return;
  const arr = [...cell.slides];
  const [m] = arr.splice(selectedSlideIndex, 1);
  arr.splice(next, 0, m);
  selectedSlideIndex = next;
  try {{ await api('/api/reorder-slides', {{ post_id: selectedId, slides: arr }}); statusEl.textContent = 'Slayt sırası güncellendi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}}
document.getElementById('btn-slide-left').addEventListener('click', () => moveSlide(-1));
document.getElementById('btn-slide-right').addEventListener('click', () => moveSlide(1));

document.getElementById('btn-render-reel').addEventListener('click', async () => {{
  statusEl.textContent = '1080×1920 H.264 Reels MP4 videosu üretiliyor (FFmpeg)…';
  const sx = document.getElementById('zoom-sx').value;
  const sy = document.getElementById('zoom-sy').value;
  const ex = document.getElementById('zoom-ex').value;
  const ey = document.getElementById('zoom-ey').value;
  const body = {{
    post_id: selectedId,
    preset: reelPresetSel.value,
    seamless_loop: document.getElementById('chk-seamless-loop').checked,
    kinetic_hook: document.getElementById('in-kinetic-hook').value.trim() || null,
  }};
  if (sx !== '' && sy !== '') body.zoom_start = [Number(sx), Number(sy)];
  if (ex !== '' && ey !== '') body.zoom_end = [Number(ex), Number(ey)];
  try {{
    await api('/api/render-reel', body);
    statusEl.textContent = 'Reels MP4 videosu üretildi ve diske kaydedildi!';
  }} catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-apply').addEventListener('click', async () => {{
  statusEl.textContent = 'Gerçek 1080×1350 (+ Reels 9:16) kapak yeniden üretiliyor…';
  try {{ await api('/api/design', {{ post_id: selectedId, design_id: designSelect.value }}); statusEl.textContent = 'Tasarım güncellendi ve kaydedildi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-split-study').addEventListener('click', async () => {{
  statusEl.textContent = 'Eskiz / Final bölünmüş ekran kapağı üretiliyor…';
  try {{ await api('/api/split-compare', {{ post_id: selectedId, mode: 'study_vs_final' }}); statusEl.textContent = 'Eskiz / Final bölünmüş kapak uygulandı.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-split-restore').addEventListener('click', async () => {{
  statusEl.textContent = 'Önce / Sonra Restorasyon bölünmüş kapağı üretiliyor…';
  try {{ await api('/api/split-compare', {{ post_id: selectedId, mode: 'before_after_restoration' }}); statusEl.textContent = 'Restorasyon Önce/Sonra bölünmüş kapak uygulandı.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-color-bridge').addEventListener('click', async () => {{
  statusEl.textContent = 'Yedek alınıp Renk Köprüsü sıralaması hesaplanıyor…';
  try {{ await api('/api/color-bridge', {{}}); statusEl.textContent = 'Grid Renk Köprüsü akışına göre sıralandı (Reels omurgası korundu).'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-approve').addEventListener('click', async () => {{
  const cell = data.full_cells.find(c => c.id === selectedId);
  const next = cell.approval_status === 'approved' ? 'draft' : 'approved';
  try {{ await api('/api/approve', {{ post_id: selectedId, approval_status: next, approve_all: false }}); statusEl.textContent = 'Onay durumu kaydedildi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-uploaded').addEventListener('click', async () => {{
  const cell = data.full_cells.find(c => c.id === selectedId);
  try {{ await api('/api/mark-uploaded', {{ post_id: selectedId, uploaded: !cell.meta_uploaded }}); statusEl.textContent = 'Meta yükleme durumu güncellendi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-approve-all').addEventListener('click', async () => {{
  try {{ await api('/api/approve', {{ post_id: null, approval_status: 'approved', approve_all: true }}); statusEl.textContent = 'Tüm gönderiler onaylandı.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-pin').addEventListener('click', async () => {{
  let pins = [...data.pinned_ids];
  if (pins.includes(selectedId)) pins = pins.filter(x => x !== selectedId);
  else {{ if (pins.length >= 3) {{ statusEl.textContent = 'En fazla 3 gönderi sabitlenebilir.'; return; }} pins.push(selectedId); }}
  try {{ await api('/api/order', {{ schedule_order: data.schedule_order, pinned_ids: pins }}); statusEl.textContent = 'Sabitleme kaydedildi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

async function moveSlot(delta) {{
  const idx = data.schedule_order.indexOf(selectedId);
  const next = idx + delta;
  if (idx < 0 || next < 0 || next >= data.schedule_order.length) return;
  const ord = [...data.schedule_order];
  const [m] = ord.splice(idx, 1); ord.splice(next, 0, m);
  try {{ await api('/api/order', {{ schedule_order: ord, pinned_ids: data.pinned_ids }}); statusEl.textContent = 'Yayın sırası ve zamanları güncellendi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}}
document.getElementById('btn-earlier').addEventListener('click', () => moveSlot(-1));
document.getElementById('btn-later').addEventListener('click', () => moveSlot(1));

document.getElementById('btn-replan').addEventListener('click', async () => {{
  statusEl.textContent = 'Yedek alınıp günlük tempo yeniden hesaplanıyor…';
  try {{
    await api('/api/replan', {{
      days: Number(document.getElementById('in-days').value),
      carousels_per_day: Number(document.getElementById('in-cday').value),
      reels_per_day: Number(document.getElementById('in-rday').value),
    }});
    statusEl.textContent = 'Plan güncellendi ve kaydedildi (önceki durum yedeklendi).';
  }} catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-snapshot').addEventListener('click', async () => {{
  try {{ const out = await api('/api/snapshot', {{ label: 'manual' }}); statusEl.textContent = 'Yedek kaydedildi: ' + out.snapshot_path; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-restore').addEventListener('click', async () => {{
  try {{ await api('/api/restore-snapshot', {{}}); statusEl.textContent = 'Son yedek geri yüklendi.'; }}
  catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

document.getElementById('btn-copy-caption').addEventListener('click', async () => {{
  try {{ await navigator.clipboard.writeText(insCaption.value); statusEl.textContent = 'Açıklama panoya kopyalandı.'; }}
  catch (e) {{ statusEl.textContent = 'Kopyalama başarısız: ' + e.message; }}
}});
document.getElementById('btn-copy-comment').addEventListener('click', async () => {{
  try {{ await navigator.clipboard.writeText(insFirstComment.value); statusEl.textContent = 'İlk yorum panoya kopyalandı.'; }}
  catch (e) {{ statusEl.textContent = 'Kopyalama başarısız: ' + e.message; }}
}});
document.getElementById('btn-copy-alt').addEventListener('click', async () => {{
  try {{ await navigator.clipboard.writeText(insAltText.value); statusEl.textContent = 'Alt-Text panoya kopyalandı.'; }}
  catch (e) {{ statusEl.textContent = 'Kopyalama başarısız: ' + e.message; }}
}});

document.getElementById('btn-export-meta').addEventListener('click', async () => {{
  statusEl.textContent = 'Meta Business Suite paketi dışa aktarılıyor…';
  try {{
    const out = await api('/api/export-meta', {{ split_weeks: document.getElementById('chk-split-weeks').checked }});
    statusEl.textContent = 'Dışa aktarıldı: ' + out.export_dir + ' (' + out.ready_count + ' hazır, ' + out.draft_reels_awaiting_video + ' MP4 bekleyen Reels taslağı)';
  }} catch (e) {{ statusEl.textContent = 'Hata: ' + e.message; }}
}});

renderGrid();
renderInspector();
</script>
</body>
</html>
"""


def monthly_plan_server(workspace_dir: str | Path, *, port: int = 38150) -> HTTPServer:
    """Loopback-only HTTP server for the Carousel + Reels 6/day studio & Meta exporter."""
    root = Path(workspace_dir).resolve()
    load_monthly_plan_state(root)
    session_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_allowed(self) -> bool:
            ap = self.server.server_port
            return self.headers.get("Host") in {f"127.0.0.1:{ap}", f"localhost:{ap}"}

        def _validate_post(self) -> dict:
            ap = self.server.server_port
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            token = self.headers.get("X-Artfolio-Editor")
            if host not in {f"127.0.0.1:{ap}", f"localhost:{ap}"} or origin != f"http://{host}":
                raise PermissionError("Open the editor from its local address")
            if not token or not token.isascii() or not hmac.compare_digest(token, session_token):
                raise PermissionError("Editor session expired; reload the page")
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise ValueError("Expected JSON body")
            length = self.headers.get("Content-Length")
            if length is None or not length.isdecimal() or not 1 <= int(length) <= MAX_API_BYTES:
                raise ValueError("Invalid content length")
            return json.loads(self.rfile.read(int(length)))

        def do_GET(self) -> None:
            if not self._host_allowed():
                self._json(403, {"error": "Invalid host"})
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                state = load_monthly_plan_state(root)
                self._send(200, render_monthly_editor_html(state, editor_token=session_token).encode("utf-8"), "text/html; charset=utf-8")
                return
            if re.fullmatch(r"/(renders|history|sources|videos)/[a-zA-Z0-9._-]+\.(jpg|jpeg|png|mp4|mov)", path):
                asset = (root / path.lstrip("/")).resolve()
                if asset.is_relative_to(root) and asset.is_file() and asset.stat().st_size <= 80_000_000:
                    ext = asset.suffix.lower()
                    mime = "video/mp4" if ext == ".mp4" else ("video/quicktime" if ext == ".mov" else ("image/png" if ext == ".png" else "image/jpeg"))
                    self._send(200, asset.read_bytes(), mime)
                    return
            self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            allowed = {
                "/api/design", "/api/order", "/api/approve", "/api/replan",
                "/api/render-reel", "/api/export-meta", "/api/reorder-slides",
                "/api/mark-uploaded", "/api/snapshot", "/api/restore-snapshot",
                "/api/split-compare", "/api/color-bridge",
            }
            if path not in allowed:
                self._json(404, {"error": "Not found"})
                return
            try:
                payload = self._validate_post()
                if path == "/api/design":
                    state = update_monthly_post_design(root, payload["post_id"], payload["design_id"])
                    self._json(200, _state_api_payload(state))
                elif path == "/api/order":
                    state = update_monthly_schedule_order(
                        root,
                        schedule_order=payload["schedule_order"],
                        pinned_ids=payload["pinned_ids"],
                    )
                    self._json(200, _state_api_payload(state))
                elif path == "/api/approve":
                    state = update_monthly_approval(
                        root,
                        post_id=payload.get("post_id"),
                        approval_status=payload.get("approval_status", "approved"),
                        approve_all=bool(payload.get("approve_all", False)),
                    )
                    self._json(200, _state_api_payload(state))
                elif path == "/api/replan":
                    state = adjust_monthly_plan_cadence(
                        root,
                        days=int(payload["days"]),
                        carousels_per_day=int(payload["carousels_per_day"]),
                        reels_per_day=int(payload["reels_per_day"]),
                    )
                    self._json(200, _state_api_payload(state))
                elif path == "/api/render-reel":
                    zs = tuple(payload["zoom_start"]) if payload.get("zoom_start") else None
                    ze = tuple(payload["zoom_end"]) if payload.get("zoom_end") else None
                    state, info = render_monthly_reel_video(
                        root,
                        payload["post_id"],
                        preset=payload.get("preset"),
                        zoom_start=zs,
                        zoom_end=ze,
                        seamless_loop=bool(payload.get("seamless_loop", True)),
                        kinetic_hook=payload.get("kinetic_hook"),
                    )
                    self._json(200, {**_state_api_payload(state), "reel_render": info})
                elif path == "/api/split-compare":
                    state = apply_split_comparison_mode(root, payload["post_id"], payload["mode"])
                    self._json(200, _state_api_payload(state))
                elif path == "/api/color-bridge":
                    state = reorder_plan_by_color_bridge(root)
                    self._json(200, _state_api_payload(state))
                elif path == "/api/reorder-slides":
                    state = reorder_post_slides(root, payload["post_id"], payload["slides"])
                    self._json(200, _state_api_payload(state))
                elif path == "/api/mark-uploaded":
                    state = mark_post_meta_uploaded(root, payload["post_id"], bool(payload.get("uploaded", True)))
                    self._json(200, _state_api_payload(state))
                elif path == "/api/snapshot":
                    snap = create_plan_snapshot(root, label=str(payload.get("label", "manual")))
                    self._json(200, {"snapshot_path": str(snap)})
                elif path == "/api/restore-snapshot":
                    state = restore_latest_snapshot(root)
                    self._json(200, _state_api_payload(state))
                else:
                    export_target = root / "meta-business-suite-export"
                    report = export_meta_business_suite_package(
                        root,
                        export_target,
                        overwrite=True,
                        split_weeks=bool(payload.get("split_weeks", False)),
                    )
                    self._json(200, report)
            except PermissionError as exc:
                self._json(403, {"error": str(exc)})
            except (ValueError, KeyError, ValidationError, FileNotFoundError, FileExistsError, RuntimeError) as exc:
                self._json(422, {"error": str(exc)})

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.editor_token = session_token  # type: ignore[attr-defined]
    return server
