"""Real-museum 30-day Instagram profile rehearsal generator & same-9 Old vs New 3x3 A/B comparator."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from src.art_calendar import find_anniversaries_for_date, match_anniversary_artist
from src.astra_catalog import get_design
from src.astra_renderer import AstraArtworkInput, render_astra_design
from src.design_selector import (
    audit_design_catalog_eligibility,
    extract_artwork_visual_profile,
    select_profile_design,
)
from src.editorial_copy_rules import compose_curated_caption, compose_first_comment, compose_seo_alt_text
from src.grid_choreography import SIGNATURE_DAYS, analyze_grid_rhythm, compute_cover_tone
from src.monthly_planner import (
    MonthlyPlanPost,
    PilotArtworkRecord,
    _render_inner_carousel_slides,
    build_daily_cadence_schedule,
)
from src.museum_pool import (
    calculate_schedule_artwork_need,
    flag_visual_near_duplicates,
    normalize_artist_name,
)
from src.neon_kit import F, GOLD, MUTED_LIGHT, OBSIDIAN, PURE_WHITE, SOFT_IVORY
from src.reel_studio import REEL_MOTION_PRESETS, ReelMotionPreset, render_local_reel_mp4, render_reel_9x16_cover

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# Legacy hardcoded shortlists preserved strictly for honest Old vs New 3x3 A/B comparison
LEGACY_CAROUSEL_1 = [
    "astra-001", "neon-009", "astra-013", "neon-180", "astra-061", "neon-173",
    "astra-041", "neon-103", "astra-031", "neon-100", "astra-066", "neon-132",
]
LEGACY_CAROUSEL_2 = ["astra-021", "neon-060", "astra-027", "neon-025", "astra-028", "neon-031"]
LEGACY_CAROUSEL_3 = ["astra-094", "neon-004"]
LEGACY_REELS = [
    "astra-083", "neon-050", "astra-051", "neon-113", "astra-053",
    "neon-168", "astra-057", "neon-171", "astra-059", "neon-069",
]


import re


def _clean_artist_surname(artist: str) -> str:
    """Extract clean surname without museum parenthetical qualifiers like '(mentioned on object)'."""
    base = re.sub(r"\s*\([^)]*\)", "", str(artist or "")).strip()
    parts = [p for p in base.replace(",", " ").split() if p]
    return parts[-1] if parts else "Archive"


def _short_title(text: str, limit: int = 46) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-(")
    dangling = {"in", "the", "of", "at", "with", "for", "and", "a", "an", "on", "to", "from", "by", "de", "van"}
    words = cut.split()
    while len(words) > 2 and words[-1].lower() in dangling:
        words.pop()
    return " ".join(words) or clean[:limit]


def _order_diverse_real_pool(artworks: Sequence[dict]) -> list[dict]:
    """Interleave verified artworks across museums and normalized artists without duplicating any work."""
    by_museum: dict[str, list[dict]] = {}
    for aw in artworks:
        if not Path(aw.get("image_path", "")).is_file():
            continue
        by_museum.setdefault(aw.get("institution", "Unknown"), []).append(aw)
    for bucket in by_museum.values():
        bucket.sort(key=lambda x: (x.get("artist_normalized", ""), x.get("canonical_id", "")))

    interleaved: list[dict] = []
    museums = sorted(by_museum.keys())
    while any(by_museum.values()):
        for m in museums:
            if by_museum[m]:
                interleaved.append(by_museum[m].pop(0))
    return interleaved


def build_real_museum_rehearsal(
    pool_artworks: Sequence[dict],
    output_dir: str | Path,
    *,
    days: int = 30,
    carousels_per_day: int = 4,
    reels_per_day: int = 2,
    start_at: datetime | None = None,
    render_sample_mp4_count: int = 2,
) -> dict:
    """Build a standalone, phone-width local 30-day Instagram profile rehearsal from REAL verified museum artworks.

    Never pads with demo/synthetic artworks. If the verified pool is smaller than the full 30-day need,
    builds only as many complete days as verified artworks support and reports exact shortfall.
    """
    root = Path(output_dir).resolve()
    renders_dir = root / "renders"
    videos_dir = root / "videos"
    renders_dir.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)

    need = calculate_schedule_artwork_need(
        days=days,
        carousels_per_day=carousels_per_day,
        reels_per_day=reels_per_day,
        reel_revisit_ratio=0.20,
    )
    pool = _order_diverse_real_pool(pool_artworks)
    vis_flags = flag_visual_near_duplicates(pool, max_distance=8)
    flag_by_id: dict[str, list[dict]] = {}
    for fl in vis_flags:
        flag_by_id.setdefault(fl["left_id"], []).append(fl)
        flag_by_id.setdefault(fl["right_id"], []).append(fl)

    # Determine how many full days can be honestly filled with zero fake padding
    per_day_posts = carousels_per_day + reels_per_day
    # Approximate unique works per day from need
    works_per_day_avg = need["total_unique_artworks_needed"] / max(1, days)
    if len(pool) >= need["total_unique_artworks_needed"]:
        build_days = days
        shortfall_works = 0
    else:
        build_days = max(1, int(len(pool) // max(1.0, works_per_day_avg)))
        shortfall_works = max(0, need["total_unique_artworks_needed"] - len(pool))

    start_dt = start_at or datetime(2026, 11, 24, 9, 30, tzinfo=ISTANBUL_TZ)
    slots = build_daily_cadence_schedule(
        start_dt,
        days=build_days,
        carousels_per_day=carousels_per_day,
        reels_per_day=reels_per_day,
    )

    pool_cursor = 0
    used_artwork_ids: set[str] = set()
    earlier_carousel_candidates: list[tuple[str, dict]] = []  # (post_id, artwork) for intentional Reel revisits
    placed_posts: list[dict] = []
    usage_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    motion_presets: tuple[ReelMotionPreset, ...] = (
        "detail_to_full",
        "slow_pan_horizontal",
        "palette_reveal",
        "chiaroscuro_zoom",
    )

    c_idx = 0
    r_idx = 0
    missing_posts: list[dict] = []
    mp4_rendered_ids: list[str] = []

    def _take_fresh(n: int) -> list[dict] | None:
        nonlocal pool_cursor
        if pool_cursor + n > len(pool):
            return None
        chunk = pool[pool_cursor : pool_cursor + n]
        pool_cursor += n
        for item in chunk:
            used_artwork_ids.add(item["canonical_id"])
        return chunk

    for slot_num, (fmt, ts) in enumerate(slots, 1):
        pid = f"post-{slot_num:03d}"
        slot_dt = datetime.fromisoformat(ts).astimezone(ISTANBUL_TZ)
        day_num = ((slot_dt.date() - start_dt.date()).days) + 1
        sig_day = SIGNATURE_DAYS[slot_dt.weekday()]
        revisits_post_id = ""
        ann_note = ""

        if fmt == "carousel":
            # Multi-work cadence matching calculate_schedule_artwork_need: 1 in 12 is 3-work, 1 in 6 is 2-work
            if c_idx % 12 == 5:
                req_n = 3
            elif c_idx % 6 == 3:
                req_n = 2
            else:
                req_n = 1
            arts = _take_fresh(req_n)
            if arts is None:
                # Graceful degradation to 1-work if 2/3-work couldn't fit remaining pool
                arts = _take_fresh(1)
                req_n = 1
            if arts is None:
                missing_posts.append({"id": pid, "day": day_num, "post_format": fmt, "reason": "insufficient_verified_artworks"})
                continue

            is_quiz = (req_n == 1 and c_idx % max(1, carousels_per_day) == 0 and slot_dt.weekday() == 4)
            lead_prof = extract_artwork_visual_profile(arts[0]["image_path"], arts[0]["title"])
            design_id, decision = select_profile_design(
                post_format="carousel",
                required_sources=req_n,
                lead_profile=lead_prof,
                placed_posts=placed_posts,
                usage_counts=usage_counts,
                family_counts=family_counts,
                is_quiz=is_quiz,
            )
            if req_n == 3:
                headline = f"{_clean_artist_surname(arts[0]['artist'])}, {_clean_artist_surname(arts[1]['artist'])} & {_clean_artist_surname(arts[2]['artist'])}"
            elif req_n == 2:
                headline = f"{_clean_artist_surname(arts[0]['artist'])} / {_clean_artist_surname(arts[1]['artist'])}"
            elif is_quiz:
                headline = f"Guess the year: {_clean_artist_surname(arts[0]['artist'])}"
            else:
                headline = _short_title(arts[0]["title"], 46)

            caption = compose_curated_caption(post_format="carousel", artworks=arts, slot_index=slot_num, is_quiz=is_quiz)
            if req_n == 1:
                earlier_carousel_candidates.append((pid, arts[0]))
            c_idx += 1
        else:
            # 1 in 5 Reels intentionally revisits an earlier Carousel work with a distinct detail focus
            want_revisit = (r_idx % 5 == 4 and len(earlier_carousel_candidates) > (r_idx // 5))
            if want_revisit:
                revisits_post_id, revisited_art = earlier_carousel_candidates[r_idx // 5]
                arts = [revisited_art]
            else:
                arts = _take_fresh(1)
                if arts is None and earlier_carousel_candidates:
                    # Do not invent fake works; mark as missing if pool exhausted
                    missing_posts.append({"id": pid, "day": day_num, "post_format": fmt, "reason": "insufficient_verified_artworks"})
                    continue
                if arts is None:
                    missing_posts.append({"id": pid, "day": day_num, "post_format": fmt, "reason": "insufficient_verified_artworks"})
                    continue

            if slot_dt.hour >= 18:
                anns = find_anniversaries_for_date(slot_dt.date())
                if anns:
                    ann_note = anns[0].format_badge()

            lead_prof = extract_artwork_visual_profile(arts[0]["image_path"], arts[0]["title"])
            design_id, decision = select_profile_design(
                post_format="reel",
                required_sources=1,
                lead_profile=lead_prof,
                placed_posts=placed_posts,
                usage_counts=usage_counts,
                family_counts=family_counts,
                is_quiz=False,
            )
            short_t = _short_title(arts[0]["title"], 34)
            if revisits_post_id:
                headline = f"Closer look: {short_t}"
            elif short_t.lower().startswith(("in ", "inside ")):
                headline = short_t
            else:
                headline = f"Inside {short_t}"
            caption = compose_curated_caption(post_format="reel", artworks=arts, slot_index=slot_num)
            preset = motion_presets[r_idx % len(motion_presets)]
            r_idx += 1

        # Render the real 1080x1350 cover (and 9:16 cover for Reels)
        sources = [
            AstraArtworkInput(
                image_path=Path(a["image_path"]),
                title=a["title"],
                artist=a["artist"],
                date=a.get("date", ""),
                institution=a.get("institution", ""),
                museum_url=a.get("museum_url", ""),
            )
            for a in arts
        ]
        cover_rel = f"renders/{pid}-{design_id}.jpg"
        render_astra_design(design_id, sources, headline, root / cover_rel)
        tone = compute_cover_tone(root / cover_rel)

        slides_rel: list[str] = [cover_rel]
        reel_9x16_rel = ""
        reel_mp4_rel = ""
        if fmt == "carousel":
            stub_post = MonthlyPlanPost(
                id=pid,
                post_format="carousel",
                design_id=design_id,
                editorial_title=headline,
                caption_en=caption,
                scheduled_at=ts,
                artworks=tuple(
                    PilotArtworkRecord(
                        source_key=a["source_key"],
                        image_path=a["image_path"],
                        artist=a["artist"],
                        title=a["title"],
                        date=a.get("date", ""),
                        institution=a.get("institution", ""),
                        museum_url=a.get("museum_url", ""),
                    )
                    for a in arts
                ),
                slide_count=4,
                dimensions_cm_text=arts[0].get("dimensions_cm", ""),
            )
            inner_rels = _render_inner_carousel_slides(root, stub_post, sources)
            slides_rel.extend(inner_rels)
        else:
            reel_9x16_rel = f"renders/{pid}-{design_id}-9x16.jpg"
            render_reel_9x16_cover(root / cover_rel, root / reel_9x16_rel)
            if len(mp4_rendered_ids) < render_sample_mp4_count:
                reel_mp4_rel = f"videos/{pid}-{preset}.mp4"
                render_local_reel_mp4(
                    sources[0],
                    headline,
                    root / reel_mp4_rel,
                    preset=preset,
                    fps=24,
                    seamless_loop=True,
                )
                mp4_rendered_ids.append(pid)

        usage_counts[design_id] += 1
        family_counts[decision["family"]] += 1

        art_sim_flags = []
        for a in arts:
            art_sim_flags.extend(flag_by_id.get(a["canonical_id"], []))

        placed_posts.append(
            {
                "id": pid,
                "slot_number": slot_num,
                "day": day_num,
                "scheduled_at": ts,
                "scheduled_label": slot_dt.strftime("%d.%m %H:%M"),
                "post_format": fmt,
                "design_id": design_id,
                "family": decision["family"],
                "decision_reasons": decision["reasons"],
                "editorial_title": headline,
                "caption_en": caption,
                "first_comment_en": compose_first_comment(arts),
                "alt_text_en": compose_seo_alt_text(arts, post_format=fmt, tone_band=tone["tone_band"]),
                "anniversary_note": ann_note,
                "signature_day_label": sig_day["label"],
                "revisits_post_id": revisits_post_id,
                "revisit_note": (
                    f"Revisits {revisits_post_id} with a dedicated vertical motion detail study"
                    if revisits_post_id
                    else ""
                ),
                "image_path": cover_rel,
                "slides": slides_rel,
                "slide_count": len(slides_rel),
                "reel_cover_9x16_path": reel_9x16_rel,
                "reel_mp4_path": reel_mp4_rel,
                "orientation": lead_prof["orientation"],
                "tone_band": tone["tone_band"],
                "temp_band": tone["temp_band"],
                "luminance": tone["luminance"],
                "visual_similarity_flags": art_sim_flags,
                "artworks": [
                    {
                        "canonical_id": a["canonical_id"],
                        "artist": a["artist"],
                        "artist_normalized": a.get("artist_normalized", normalize_artist_name(a["artist"])),
                        "title": a["title"],
                        "date": a.get("date", ""),
                        "institution": a.get("institution", ""),
                        "museum_url": a.get("museum_url", ""),
                        "rights_status": a.get("rights_status", ""),
                        "rights_basis": a.get("rights_basis", ""),
                        "dhash": a.get("dhash", ""),
                    }
                    for a in arts
                ],
            }
        )

    # Render Same-9 Old vs New 3x3 comparison using first 9 posts' EXACT real artworks & order
    ab_report = render_same_9_ab_comparison(placed_posts[:9], pool, root)

    # Compute comprehensive audit summary
    all_used_artists = {
        a["artist_normalized"] for p in placed_posts for a in p["artworks"] if a.get("artist_normalized")
    }
    museum_counts = Counter(a["institution"] for p in placed_posts for a in p["artworks"])
    rhythm = analyze_grid_rhythm(
        [
            {"post_format": p["post_format"], "tone_band": p["tone_band"], "design_family": p["family"]}
            for p in reversed(placed_posts)
        ]
    )
    summary = {
        "schema_version": "artfolio-profile-rehearsal-v1",
        "profile_handle": "@artfolio.db",
        "assumptions": {
            "pinned_posts": "Live Instagram pinned-post state is not readable offline; rehearsal defaults to 0 pinned posts (pure chronological newest-top-left) and lets you toggle up to 3 local pins in the preview.",
            "hidden_reels": "All Reels are assumed visible on the main profile grid using their 4:5 center crop cover (no Reels hidden from profile grid).",
        },
        "artwork_need_calculation": need,
        "catalog_eligibility": audit_design_catalog_eligibility(),
        "planned_posts": len(slots),
        "rendered_posts": len(placed_posts),
        "missing_posts_count": len(missing_posts),
        "missing_posts": missing_posts,
        "completed_days": max((p["day"] for p in placed_posts), default=0),
        "shortfall_unique_artworks": shortfall_works,
        "carousels_rendered": sum(1 for p in placed_posts if p["post_format"] == "carousel"),
        "reels_covers_rendered": sum(1 for p in placed_posts if p["post_format"] == "reel"),
        "reels_full_mp4_rendered": len(mp4_rendered_ids),
        "reels_revisiting_earlier_carousel": sum(1 for p in placed_posts if p["revisits_post_id"]),
        "unique_artworks_used": len(used_artwork_ids),
        "unique_artists_used": len(all_used_artists),
        "by_museum": dict(museum_counts.most_common()),
        "unique_designs_used": len(usage_counts),
        "unique_layout_families_used": len(family_counts),
        "layout_family_distribution": dict(family_counts.most_common()),
        "top_3_repeated_designs": usage_counts.most_common(3),
        "visual_similarity_flags_count": len(vis_flags),
        "visual_similarity_flags": vis_flags[:20],
        "grid_rhythm": rhythm,
        "ab_comparison": ab_report,
    }

    manifest = {"summary": summary, "posts": placed_posts}
    (root / "rehearsal_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    html_path = root / "index.html"
    html_path.write_text(_render_rehearsal_html(manifest), encoding="utf-8")
    return {
        "output_dir": str(root),
        "html_path": str(html_path),
        "manifest_path": str(root / "rehearsal_manifest.json"),
        "summary": summary,
    }


def render_same_9_ab_comparison(
    first_nine_posts: Sequence[dict],
    pool: Sequence[dict],
    output_dir: Path,
) -> dict:
    """Render Old Selector vs New Selector 3x3 Instagram grids using the EXACT SAME 9 real artworks & order."""
    if len(first_nine_posts) < 9:
        return {"generated": False}

    pool_by_id = {a["canonical_id"]: a for a in pool}
    old_dir = output_dir / "ab_compare"
    old_dir.mkdir(parents=True, exist_ok=True)

    c_i = 0
    r_i = 0
    old_cells: list[dict] = []
    new_cells: list[dict] = []

    for post in first_nine_posts:
        fmt = post["post_format"]
        req_n = len(post["artworks"])
        if fmt == "carousel":
            if req_n == 3:
                old_did = LEGACY_CAROUSEL_3[c_i % len(LEGACY_CAROUSEL_3)]
            elif req_n == 2:
                old_did = LEGACY_CAROUSEL_2[c_i % len(LEGACY_CAROUSEL_2)]
            else:
                old_did = LEGACY_CAROUSEL_1[c_i % len(LEGACY_CAROUSEL_1)]
            c_i += 1
        else:
            old_did = LEGACY_REELS[r_i % len(LEGACY_REELS)]
            r_i += 1

        sources = [
            AstraArtworkInput(
                image_path=Path(pool_by_id[a["canonical_id"]]["image_path"]),
                title=a["title"],
                artist=a["artist"],
                date=a.get("date", ""),
                institution=a.get("institution", ""),
                museum_url=a.get("museum_url", ""),
            )
            for a in post["artworks"]
        ]
        old_path = old_dir / f"old-{post['id']}-{old_did}.jpg"
        render_astra_design(old_did, sources, post["editorial_title"], old_path)

        old_cells.append(
            {
                "post_id": post["id"],
                "post_format": fmt,
                "design_id": old_did,
                "family": get_design(old_did).family,
                "title": post["artworks"][0]["title"],
                "artist": post["artworks"][0]["artist"],
                "image_path": old_path,
            }
        )
        new_cells.append(
            {
                "post_id": post["id"],
                "post_format": fmt,
                "design_id": post["design_id"],
                "family": post["family"],
                "title": post["artworks"][0]["title"],
                "artist": post["artworks"][0]["artist"],
                "image_path": output_dir / post["image_path"],
            }
        )

    def _compose_3x3(cells: list[dict], heading: str, dest: Path) -> Path:
        # Real Instagram order: newest (slot 9) at top-left, oldest (slot 1) at bottom-right
        ordered = list(reversed(cells))
        cw, ch, gap, pad, top_h = 340, 425, 6, 20, 72
        grid_w = pad * 2 + cw * 3 + gap * 2
        grid_h = top_h + pad + ch * 3 + gap * 2 + 24
        canvas = Image.new("RGB", (grid_w, grid_h), (246, 243, 236))
        draw = ImageDraw.Draw(canvas)
        draw.text((pad, 18), heading, font=F("sans_b", 22), fill=(23, 35, 29))
        draw.text((pad, 46), "En yeni sol üstte (Slot 9 → Slot 1) · Aynı 9 gerçek müze eseri", font=F("sans", 14), fill=(95, 90, 80))

        for i, cell in enumerate(ordered):
            row, col = divmod(i, 3)
            x0 = pad + col * (cw + gap)
            y0 = top_h + row * (ch + gap)
            with Image.open(cell["image_path"]) as im:
                thumb = im.convert("RGB").resize((cw, ch), Image.Resampling.LANCZOS)
            canvas.paste(thumb, (x0, y0))
            # Subtle bottom corner badge showing post_id + design_id + family
            badge = f"{cell['post_id']} · {cell['design_id']} ({cell['family']})"
            draw.rectangle((x0, y0 + ch - 26, x0 + cw, y0 + ch), fill=(18, 18, 16))
            draw.text((x0 + 8, y0 + ch - 21), badge[:40], font=F("mono", 12), fill=SOFT_IVORY)
        canvas.save(dest, format="JPEG", quality=92)
        return dest

    old_img = _compose_3x3(old_cells, "ESKİ SEÇİCİ (Sabit Liste + Modülo)", old_dir / "old_selector_3x3.jpg")
    new_img = _compose_3x3(new_cells, "YENİ SEÇİCİ (Eser + Komşu Duyarlı)", old_dir / "new_selector_3x3.jpg")

    with Image.open(old_img) as im_l, Image.open(new_img) as im_r:
        side = Image.new("RGB", (im_l.width + im_r.width + 36, max(im_l.height, im_r.height) + 24), (24, 24, 22))
        side.paste(im_l, (12, 12))
        side.paste(im_r, (im_l.width + 24, 12))
        combined = old_dir / "ab_3x3_old_vs_new.jpg"
        side.save(combined, format="JPEG", quality=92)

    return {
        "generated": True,
        "old_3x3_path": str(old_img),
        "new_3x3_path": str(new_img),
        "combined_ab_path": str(combined),
        "old_3x3_rel": "ab_compare/old_selector_3x3.jpg",
        "new_3x3_rel": "ab_compare/new_selector_3x3.jpg",
        "combined_ab_rel": "ab_compare/ab_3x3_old_vs_new.jpg",
        "slots": [
            {
                "post_id": o["post_id"],
                "post_format": o["post_format"],
                "title": o["title"],
                "artist": o["artist"],
                "old_design": o["design_id"],
                "old_family": o["family"],
                "new_design": n["design_id"],
                "new_family": n["family"],
            }
            for o, n in zip(old_cells, new_cells, strict=True)
        ],
    }


def _render_rehearsal_html(manifest: dict) -> str:
    """Render a standalone, file://-compatible phone-width 3-column Instagram profile rehearsal."""
    payload = json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    s = manifest["summary"]
    max_day = max(1, int(s.get("completed_days", 1)))
    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@artfolio.db · 30 Günlük Gerçek Müze Profil Provası</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f3f0e7;color:#1a1916;font:14px/1.5 system-ui,-apple-system,sans-serif}}
header{{background:#15211b;color:#f5edd8;padding:16px 22px;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px}}
header h1{{margin:0;font:20px/1.2 Georgia,serif;font-weight:normal}}
header p{{margin:4px 0 0;font-size:12px;color:#cfc4a8}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;background:#273b30;border:1px solid #456352;color:#f5edd8;margin-left:6px}}
.toolbar{{background:#fff;border-bottom:1px solid #dbd4c3;padding:10px 22px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}}
.btn-group{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.btn{{padding:6px 11px;border-radius:8px;border:1px solid #c5bca8;background:#faf8f2;color:#1a1916;font:12.5px system-ui;cursor:pointer}}
.btn.active{{background:#15211b;color:#fff9ed;border-color:#15211b}}
.layout{{max-width:1280px;margin:18px auto 48px;padding:0 16px;display:grid;grid-template-columns:minmax(360px,430px) minmax(0,1fr);gap:22px;align-items:start}}
.layout.wide{{grid-template-columns:minmax(0,1.25fr) minmax(340px,.85fr)}}
@media(max-width:920px){{.layout,.layout.wide{{grid-template-columns:1fr}}}}
.phone-frame{{background:#fff;border:2px solid #222;border-radius:26px;padding:12px 10px 16px;box-shadow:0 12px 32px rgba(0,0,0,.09)}}
.ig-header{{display:flex;align-items:center;gap:12px;padding:4px 6px 10px;border-bottom:1px solid #ece7db;margin-bottom:8px}}
.ig-avatar{{width:46px;height:46px;border-radius:50%;background:#15211b;color:#e5c377;display:flex;align-items:center;justify-content:center;font:15px Georgia,serif}}
.ig-stats{{font-size:11.5px;color:#5d584d}}
.ig-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2px;max-height:74vh;overflow-y:auto}}
.cell{{position:relative;aspect-ratio:4/5;background:#e8e2d4;border:2px solid transparent;padding:0;cursor:pointer;overflow:hidden}}
.cell.selected{{border-color:#b57c2a;box-shadow:0 0 0 1px #b57c2a}}
.cell img{{width:100%;height:100%;object-fit:cover;display:block}}
.badge-tr{{position:absolute;top:4px;right:4px;background:rgba(16,16,14,.8);color:#fff;font-size:9px;padding:2px 4px;border-radius:3px}}
.badge-tl{{position:absolute;top:4px;left:4px;background:#b57c2a;color:#fff;font-size:9px;padding:2px 4px;border-radius:3px}}
.panel{{background:#fff;border:1px solid #dcd5c5;border-radius:14px;padding:16px;box-shadow:0 6px 24px rgba(20,18,14,.05)}}
.inspector{{position:sticky;top:14px}}
.hero{{width:100%;max-height:380px;object-fit:contain;background:#f0ebe0;border-radius:8px;display:block;margin:8px 0}}
.meta{{font-size:12px;color:#5a554a;margin:0 0 8px}}
.note{{background:#faf5e6;border:1px solid #dfd0a8;border-radius:8px;padding:8px 10px;font-size:12px;margin:8px 0}}
.art-card{{background:#faf8f3;border:1px solid #e6dfd0;border-radius:8px;padding:9px 11px;font-size:12.5px;margin:8px 0}}
.art-card a{{color:#1b4d39}}
.ab-box{{margin-top:16px;border-top:1px solid #e6dfd0;padding-top:12px}}
.ab-box img{{width:100%;border-radius:8px;border:1px solid #d8d0be;margin-top:6px}}
.swipe-bar{{display:none;align-items:center;justify-content:space-between;gap:8px;margin:6px 0}}
.swipe-bar.show{{display:flex}}
.slides-strip{{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;margin-bottom:8px}}
.slides-strip img{{height:66px;width:auto;border-radius:5px;border:2px solid transparent;cursor:pointer;opacity:.75}}
.slides-strip img.active{{border-color:#15211b;opacity:1}}
textarea{{width:100%;min-height:84px;padding:8px;border:1px solid #c9c1ae;border-radius:7px;background:#faf8f3;font:12.5px system-ui;margin-top:6px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>@artfolio.db · 30 Günlük Gerçek Müze Profil Provası</h1>
    <p>4 Kaydırmalı + 2 Reels / gün · En yeni gönderi sol üstte · Yalnızca doğrulanmış kamu malı müze eserleri</p>
  </div>
  <div>
    <span class="pill">{s['rendered_posts']} gönderi ({s['completed_days']} gün)</span>
    <span class="pill">{s['unique_artworks_used']} benzersiz eser · {s['unique_artists_used']} sanatçı</span>
    <span class="pill">{s['unique_designs_used']} tasarım · {s['unique_layout_families_used']} aile</span>
  </div>
</header>

<div class="toolbar">
  <div class="btn-group">
    <strong>Profil Günü:</strong>
    <button type="button" class="btn" data-day="1">1. Gün Sonu</button>
    <button type="button" class="btn" data-day="7">7. Gün Sonu</button>
    <button type="button" class="btn" data-day="14">14. Gün Sonu</button>
    <button type="button" class="btn" data-day="{max(1, max_day - 1)}">{max(1, max_day - 1)}. Gün Sonu</button>
    <button type="button" class="btn active" data-day="{max_day}">{max_day}. Gün Sonu (Tüm Ay)</button>
    <label style="font-size:12px;margin-left:6px">Gün <input id="day-slider" type="range" min="1" max="{max_day}" value="{max_day}"> <strong id="day-val">{max_day}</strong></label>
  </div>
  <div class="btn-group">
    <button type="button" class="btn active" id="btn-phone">Telefon (390px)</button>
    <button type="button" class="btn" id="btn-wide">Geniş Görünüm</button>
  </div>
</div>

<div class="layout" id="layout">
  <section class="phone-frame">
    <div class="ig-header">
      <div class="ig-avatar">af.</div>
      <div>
        <strong>artfolio.db</strong>
        <div class="ig-stats" id="ig-stats"></div>
      </div>
    </div>
    <div class="ig-grid" id="ig-grid"></div>
    <div class="meta" style="margin:8px 4px 0;font-size:11px">
      Varsayım: Canlı sabitlenmiş gönderi ve gizli Reel ayarına çevrimdışı erişilemediği için varsayılan görünüm saf kronolojiktir (en yeni sol üstte); hücre içinden yerelde en fazla 3 sabitleme denenebilir.
    </div>
  </section>

  <aside class="panel inspector">
    <h2 id="ins-title" style="margin:0 0 4px;font:20px Georgia,serif">Bir gönderi seçin</h2>
    <p class="meta" id="ins-meta"></p>
    <div class="note" id="ins-note" style="display:none"></div>
    <img class="hero" id="ins-hero" alt="Önizleme">
    <div class="swipe-bar" id="swipe-bar">
      <button type="button" class="btn" id="btn-prev-slide">◀ Önceki Slayt</button>
      <strong id="slide-counter" style="font-size:12.5px">1 / 4</strong>
      <button type="button" class="btn" id="btn-next-slide">Sonraki Slayt ▶</button>
    </div>
    <div class="slides-strip" id="slides-strip"></div>
    <video id="ins-video" controls style="display:none;width:100%;max-height:300px;border-radius:8px;background:#000;margin:8px 0"></video>
    <div class="btn-group" style="margin-bottom:8px">
      <button type="button" class="btn" id="btn-pin-local">Yerelde Sabitle / Kaldır (max 3)</button>
      <button type="button" class="btn" id="btn-show-9x16" style="display:none">9:16 Reels Kapağını Gör</button>
      <button type="button" class="btn" id="btn-show-4x5" style="display:none">4:5 Grid Kapağını Gör</button>
    </div>
    <div id="ins-artworks"></div>
    <textarea id="ins-caption" readonly></textarea>

    <div class="ab-box">
      <strong>Aynı 9 Gerçek Eser · Eski Seçici vs Yeni Seçici (3×3 Karşılaştırma)</strong>
      <img src="{s['ab_comparison'].get('combined_ab_rel', '')}" alt="Eski ve Yeni Seçici 3x3 Karşılaştırması">
    </div>
  </aside>
</div>

<script type="application/json" id="rehearsal-data">{payload}</script>
<script>
const data = JSON.parse(document.getElementById('rehearsal-data').textContent);
let activeDay = {max_day};
let selectedId = data.posts.length ? data.posts[data.posts.length - 1].id : null;
let selectedSlideIdx = 0;
let localPins = [];

const gridEl = document.getElementById('ig-grid');
const statsEl = document.getElementById('ig-stats');
const daySlider = document.getElementById('day-slider');
const dayVal = document.getElementById('day-val');
const swipeBar = document.getElementById('swipe-bar');
const slideCounter = document.getElementById('slide-counter');
const slidesStrip = document.getElementById('slides-strip');
const insVideo = document.getElementById('ins-video');

function visiblePosts() {{
  const upToDay = data.posts.filter(p => p.day <= activeDay);
  const pinned = localPins.map(id => upToDay.find(p => p.id === id)).filter(Boolean);
  const rest = upToDay.filter(p => !localPins.includes(p.id)).reverse(); // newest top-left
  return [...pinned, ...rest];
}}

function renderGrid() {{
  const list = visiblePosts();
  statsEl.textContent = list.length + ' gönderi · Gün ' + activeDay + ' sonu · ' + localPins.length + ' sabit';
  gridEl.innerHTML = '';
  for (const p of list) {{
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cell' + (p.id === selectedId ? ' selected' : '');
    const pinTag = localPins.includes(p.id) ? '<span class="badge-tl">SABİT</span>' : '';
    const fmtTag = p.post_format === 'reel' ? '<span class="badge-tr">REEL</span>' : '<span class="badge-tr">1/' + (p.slide_count || 1) + '</span>';
    btn.innerHTML = '<img src="' + p.image_path + '" alt="">' + pinTag + fmtTag;
    btn.addEventListener('click', () => {{ selectedId = p.id; selectedSlideIdx = 0; renderGrid(); renderInspector(); }});
    gridEl.appendChild(btn);
  }}
}}

function showSlide(p, idx) {{
  const slides = (p.slides && p.slides.length) ? p.slides : [p.image_path];
  selectedSlideIdx = Math.max(0, Math.min(slides.length - 1, idx));
  document.getElementById('ins-hero').src = slides[selectedSlideIdx];
  slideCounter.textContent = (selectedSlideIdx + 1) + ' / ' + slides.length;
  [...slidesStrip.children].forEach((el, i) => el.classList.toggle('active', i === selectedSlideIdx));
}}

function renderInspector() {{
  const p = data.posts.find(x => x.id === selectedId) || visiblePosts()[0];
  if (!p) return;
  selectedId = p.id;
  document.getElementById('ins-title').textContent = p.editorial_title;
  document.getElementById('ins-meta').textContent =
    'Gün ' + p.day + ' · ' + p.scheduled_label + ' · ' + p.post_format.toUpperCase() +
    ' · Tasarım: ' + p.design_id + ' [' + p.family + '] · Sebep: ' + (p.decision_reasons || []).join(', ');
  document.getElementById('ins-caption').value = p.caption_en || '';

  const slides = (p.slides && p.slides.length) ? p.slides : [p.image_path];
  if (selectedSlideIdx >= slides.length) selectedSlideIdx = 0;
  slidesStrip.innerHTML = '';
  swipeBar.classList.toggle('show', slides.length > 1);
  if (slides.length > 1) {{
    slides.forEach((src, i) => {{
      const img = document.createElement('img');
      img.src = src;
      img.alt = 'Slayt ' + (i + 1);
      img.addEventListener('click', () => showSlide(p, i));
      slidesStrip.appendChild(img);
    }});
  }}
  showSlide(p, selectedSlideIdx);

  if (p.reel_mp4_path) {{
    insVideo.src = p.reel_mp4_path;
    insVideo.style.display = 'block';
  }} else {{
    insVideo.style.display = 'none';
    insVideo.removeAttribute('src');
  }}

  const notes = [];
  if (p.anniversary_note) notes.push('📅 ' + p.anniversary_note);
  if (p.revisit_note) notes.push('🔁 ' + p.revisit_note);
  if (p.visual_similarity_flags && p.visual_similarity_flags.length) {{
    notes.push('⚠️ Görsel benzerlik uyarısı (otomatik elenmedi): ' + p.visual_similarity_flags.map(f => f.left_id + ' ↔ ' + f.right_id + ' (d=' + f.dhash_distance + ')').join('; '));
  }}
  const noteEl = document.getElementById('ins-note');
  noteEl.style.display = notes.length ? 'block' : 'none';
  noteEl.textContent = notes.join(' | ');

  const isReel = p.post_format === 'reel' && Boolean(p.reel_cover_9x16_path);
  document.getElementById('btn-show-9x16').style.display = isReel ? 'inline-block' : 'none';
  document.getElementById('btn-show-4x5').style.display = isReel ? 'inline-block' : 'none';

  document.getElementById('ins-artworks').innerHTML = p.artworks.map(a =>
    '<div class="art-card">' +
      '<strong>' + a.title + '</strong> — ' + a.artist + (a.date ? ' (' + a.date + ')' : '') + '<br>' +
      '<span class="meta">' + a.institution + ' · ID: ' + a.canonical_id + ' · Hak: ' + a.rights_basis + '</span>' +
      (a.museum_url ? '<br><a href="' + a.museum_url + '" target="_blank" rel="noopener">Müze Kaynağı ↗</a>' : '') +
    '</div>'
  ).join('');
}}

document.querySelectorAll('[data-day]').forEach(btn => {{
  btn.addEventListener('click', () => {{
    activeDay = Number(btn.dataset.day);
    daySlider.value = String(activeDay);
    dayVal.textContent = String(activeDay);
    document.querySelectorAll('[data-day]').forEach(b => b.classList.toggle('active', b === btn));
    const vis = visiblePosts();
    if (vis.length) {{ selectedId = vis[0].id; selectedSlideIdx = 0; }}
    renderGrid(); renderInspector();
  }});
}});
daySlider.addEventListener('input', () => {{
  activeDay = Number(daySlider.value);
  dayVal.textContent = String(activeDay);
  const vis = visiblePosts();
  if (vis.length) {{ selectedId = vis[0].id; selectedSlideIdx = 0; }}
  renderGrid(); renderInspector();
}});
document.getElementById('btn-phone').addEventListener('click', () => {{
  document.getElementById('layout').classList.remove('wide');
  document.getElementById('btn-phone').classList.add('active');
  document.getElementById('btn-wide').classList.remove('active');
}});
document.getElementById('btn-wide').addEventListener('click', () => {{
  document.getElementById('layout').classList.add('wide');
  document.getElementById('btn-wide').classList.add('active');
  document.getElementById('btn-phone').classList.remove('active');
}});
document.getElementById('btn-pin-local').addEventListener('click', () => {{
  if (localPins.includes(selectedId)) localPins = localPins.filter(x => x !== selectedId);
  else if (localPins.length < 3) localPins.push(selectedId);
  renderGrid();
}});
document.getElementById('btn-show-9x16').addEventListener('click', () => {{
  const p = data.posts.find(x => x.id === selectedId);
  if (p && p.reel_cover_9x16_path) document.getElementById('ins-hero').src = p.reel_cover_9x16_path;
}});
document.getElementById('btn-show-4x5').addEventListener('click', () => {{
  const p = data.posts.find(x => x.id === selectedId);
  if (p) document.getElementById('ins-hero').src = p.image_path;
}});
document.getElementById('btn-prev-slide').addEventListener('click', () => {{
  const p = data.posts.find(x => x.id === selectedId);
  if (p) showSlide(p, selectedSlideIdx - 1);
}});
document.getElementById('btn-next-slide').addEventListener('click', () => {{
  const p = data.posts.find(x => x.id === selectedId);
  if (p) showSlide(p, selectedSlideIdx + 1);
}});
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {{
    const p = data.posts.find(x => x.id === selectedId);
    if (p && p.slides && p.slides.length > 1) showSlide(p, selectedSlideIdx + (e.key === 'ArrowRight' ? 1 : -1));
  }}
}});

renderGrid();
renderInspector();
</script>
</body>
</html>
"""
