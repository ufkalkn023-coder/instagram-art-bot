"""Permanent Carousel production & weekly delivery engine:
- 2-tier general curatorial pairing (documented museum relation first, then concrete visible contrast)
- Automatic fallback to single-artwork multi-slide Carousel when no strong partner exists
- Single-source-of-truth finalized slide list driving JPG page numbers, file order, per-slide Alt-Text, caption & HTML
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Sequence

from PIL import Image

from src.astra_catalog import get_design
from src.astra_renderer import AstraArtworkInput, render_astra_design
from src.curatorial_brief import (
    COPY_VERSION,
    _extract_event_date_from_text,
    _support_noun,
    build_curatorial_brief,
    clean_museum_artwork,
    compose_multi_work_headline,
    decide_slide_two_intent,
    extract_sitter_relation,
)
from src.design_selector import extract_artwork_visual_profile, select_profile_design
from src.editorial_copy_rules import build_targeted_hashtags
from src.grid_choreography import compute_cover_tone
from src.monthly_planner import (
    MonthlyPlanPost,
    PilotArtworkRecord,
    _render_inner_carousel_slides,
)
from src.museum_pool import ArtworkReuseLedger, calculate_carousel_only_artwork_need
from src.neon_kit import parse_year, short_museum
from src.preflight_and_feedback import run_preflight_checks

MOTIF_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("winter_snow", "Winter & Snow", ("winter", "frozen", "snow", "ice", "skate", "skating")),
    ("still_life", "Still Life & Botanicals", ("still life", "stilleven", "bouquet", "flower", "fruit", "oyster", "vase", "peach", "pears", "tulip")),
    ("water_coast", "Water, Rivers & Harbors", ("sea", "harbor", "harbour", "river", "waterway", "coast", "anchorage", "bay", "falls", "canal", "bridge", "marne", "boat", "ship", "beach", "tunis")),
    ("portrait", "Portraits & Human Presence", ("portrait", "portret", "self-portrait", "woman", "man", "girl", "boy", "monk", "lady", "baron", "head of", "bust", "sitter", "peasant", "old woman", "child")),
    ("interior_sacred", "Interiors & Sacred Spaces", ("interior", "church", "st. mark", "holy family", "virgin", "saints", "calvary", "nativity", "room", "piano", "kitchen", "pancake", "triptych", "chapel", "temple")),
    ("study_sketch", "Preparatory Studies & Sketches", ("sketch", "study", "unfinished", "preliminary")),
    ("landscape", "Landscapes & Open Terrain", ("landscape", "view of", "valley", "mountain", "hill", "road", "forest", "wood", "tree", "village", "pastoral", "meadow", "field", "château", "allée")),
)

# Cover families that already show the entire artwork uncropped -> concise 3-slide arc (no redundant full-plate repeat)
FULL_FRAME_COVER_FAMILIES = frozenset({"gallery", "archive", "colors", "story"})
CLOSEUP_COVER_FAMILIES = frozenset({"detail", "cinema", "experimental"})
VERIFIED_RIGHTS = frozenset({"CONFIRMED_PUBLIC_DOMAIN", "CONFIRMED_OPEN_ACCESS", "CC0", "Public Domain"})


def classify_artwork_motif(artwork: dict) -> tuple[str, str]:
    """Return (motif_slug, motif_label) grounded directly in the museum title."""
    title = (artwork.get("title") or "").lower()
    for slug, label, keywords in MOTIF_RULES:
        if any(k in title for k in keywords):
            return slug, label
    return "general_collection", "Museum Collection"


_SEASONAL_PENDANT_PAIRS: tuple[frozenset[str], ...] = (
    frozenset({"summer", "winter"}),
    frozenset({"spring", "autumn"}),
    frozenset({"morning", "evening"}),
    frozenset({"day", "night"}),
)


def _extract_series_key(artwork: dict) -> tuple[str, str, str]:
    """Return (series_key, relation_source_field, relation_evidence) for explicit museum-documented links."""
    if artwork.get("pair_group"):
        val = str(artwork["pair_group"]).strip()
        return f"pair:{val.lower()}", "metadata.pair_group", f"metadata.pair_group={val}"
    if artwork.get("series_id"):
        val = str(artwork["series_id"]).strip()
        return f"series:{val.lower()}", "metadata.series_id", f"metadata.series_id={val}"
    title = (artwork.get("title") or "").strip()
    if re.search(r"\(\s*one of a pair\s*\)", title, re.IGNORECASE):
        artist_norm = (artwork.get("artist_normalized") or artwork.get("artist") or "").strip().lower()
        inst = (artwork.get("institution") or "").strip()
        return (
            f"one_of_a_pair:{artist_norm}:{inst.lower()}",
            "title:one_of_a_pair",
            f"title:one_of_a_pair='{title}' ({inst})",
        )
    m_from = re.search(r"from (?:an?|the)\s+([A-Za-z0-9' -]{4,40})", title, re.IGNORECASE)
    if m_from:
        series_name = m_from.group(1).strip()
        return (
            f"series:{series_name.lower()}",
            "title:series_reference",
            f"title:series_reference='{series_name}'",
        )
    m_study = re.search(r"(?:sketch|study)\s+for\s+[\"'“]?([^\"'”.,;]{4,40})", title, re.IGNORECASE)
    if m_study:
        target = m_study.group(1).strip()
        return (
            f"study:{target.lower()}",
            "title:preparatory_study",
            f"title:preparatory_study='{title}'",
        )
    return "", "", ""


def _artwork_visual_facts(artwork: dict) -> dict:
    """Measure orientation, luminance band, and creation year for concrete contrast checks."""
    yr = parse_year(artwork.get("date", "") or "")
    p = Path(artwork.get("image_path", ""))
    if p.is_file():
        tone = compute_cover_tone(p)
        with Image.open(p) as im:
            w, h = im.size
        aspect = w / max(1, h)
        orient = "landscape" if aspect >= 1.18 else ("portrait" if aspect <= 0.85 else "square")
        return {"year": yr, "tone_band": tone["tone_band"], "orientation": orient, "min_edge": min(w, h)}
    return {"year": yr, "tone_band": "mid", "orientation": "square", "min_edge": 1000}


def evaluate_multi_work_connection(artworks: Sequence[dict]) -> dict:
    """Strictly separate museum-documented pairs/series from editorial pairings.

    1) documented_pair_or_series (ONLY when explicit museum linking evidence exists):
       - metadata.pair_group / metadata.series_id
       - metadata.companion_catalog_ids linking the works' catalog/canonical IDs
       - metadata.museum_relation_note explicitly naming the companion/pair
       - Museum title '(one of a pair)' shared by same artist & institution
       - Preparatory study ('Sketch/Study for X') matched with finished work 'X'
       Records `relation_source_field` AND `relation_evidence`!
    2) editorial_same_artist:
       - Two works by the same non-anonymous artist WITHOUT an explicit museum catalog/series link
         (including same-year pendant candidates like Summer/Winter used for candidate discovery only).
       - Records `editorial_bond_en` describing what the viewer concretely sees.
    3) editorial_visual_contrast:
       - Shared concrete motif across different artists + measurable visible contrast (>=80yr era gap, dark vs daylight, or vertical vs horizontal).
       - Records `editorial_bond_en`.
    """
    if len(artworks) < 2:
        return {
            "strong": False,
            "tier": "none",
            "relation_source_field": "",
            "relation_evidence": "",
            "candidate_discovery_hint": "",
            "editorial_bond_en": "",
            "motif_slug": "single",
            "theme_label": "",
            "reason_en": "",
        }

    cleaned = [clean_museum_artwork(a) for a in artworks]
    series_info = [_extract_series_key(a) for a in cleaned]
    series_keys = [s[0] for s in series_info]
    series_fields = [s[1] for s in series_info]
    series_evidences = [s[2] for s in series_info]
    facts = [_artwork_visual_facts(a) for a in cleaned]
    years = [f["year"] for f in facts if f["year"] is not None]
    tones = {f["tone_band"] for f in facts}
    orients = {f["orientation"] for f in facts}

    # --- 1A: Explicit museum-documented pair/series/one-of-a-pair ---
    non_empty_series = [k for k in series_keys if k]
    if len(non_empty_series) == len(cleaned) and len(set(non_empty_series)) == 1:
        src_field = series_fields[0]
        evidence = series_evidences[0]
        label = non_empty_series[0].split(":", 1)[1].replace(":", " · ").title()
        return {
            "strong": True,
            "tier": "documented_pair_or_series",
            "relation_source_field": src_field,
            "relation_evidence": evidence,
            "candidate_discovery_hint": "",
            "editorial_bond_en": "",
            "motif_slug": "documented_series",
            "theme_label": f"Documented Pair / Series ({label[:42]})",
            "reason_en": (
                f"Museum records ({src_field}) explicitly link these works, allowing the viewer to see "
                f"the companion canvases together as intended."
            ),
        }

    # --- 1B: Explicit companion_catalog_ids or museum_relation_note cross-reference ---
    if len(cleaned) == 2:
        id_sets = [
            {str(x).strip() for x in (a.get("canonical_id"), a.get("source_id"), a.get("source_key")) if x}
            for a in cleaned
        ]
        comp_0 = {str(x).strip() for x in (cleaned[0].get("companion_catalog_ids") or []) if x}
        comp_1 = {str(x).strip() for x in (cleaned[1].get("companion_catalog_ids") or []) if x}
        if comp_0.intersection(id_sets[1]) or comp_1.intersection(id_sets[0]):
            matched = sorted(comp_0.intersection(id_sets[1]) or comp_1.intersection(id_sets[0]))
            return {
                "strong": True,
                "tier": "documented_pair_or_series",
                "relation_source_field": "metadata.companion_catalog_ids",
                "relation_evidence": f"metadata.companion_catalog_ids={matched}",
                "candidate_discovery_hint": "",
                "editorial_bond_en": "",
                "motif_slug": "documented_series",
                "theme_label": "Documented Companion Pair",
                "reason_en": "Museum catalog IDs explicitly cross-reference these two works as documented companions.",
            }

        note_0 = str(cleaned[0].get("museum_relation_note") or cleaned[0].get("description") or "").strip()
        note_1 = str(cleaned[1].get("museum_relation_note") or cleaned[1].get("description") or "").strip()
        t0_low, t1_low = cleaned[0]["title"].lower(), cleaned[1]["title"].lower()
        if (note_0 and t1_low in note_0.lower()) or (note_1 and t0_low in note_1.lower()):
            ev_note = note_0 if (note_0 and t1_low in note_0.lower()) else note_1
            return {
                "strong": True,
                "tier": "documented_pair_or_series",
                "relation_source_field": "metadata.museum_relation_note",
                "relation_evidence": f"metadata.museum_relation_note='{ev_note[:160]}'",
                "candidate_discovery_hint": "",
                "editorial_bond_en": "",
                "motif_slug": "documented_series",
                "theme_label": "Documented Companion Pair",
                "reason_en": "The museum's own catalog description explicitly identifies these two works as companions.",
            }

        # --- 1B-bis: Shared museum description explicitly linking outer panels / wings / altarpiece / pair ---
        if note_0 and note_1:
            norm_n0 = " ".join(note_0.lower().split())[:120]
            norm_n1 = " ".join(note_1.lower().split())[:120]
            if norm_n0 == norm_n1 and any(
                tok in norm_n0 for tok in ("outer panels", "altarpiece", "outer wings", "pair", "pendant", "companion")
            ):
                return {
                    "strong": True,
                    "tier": "documented_pair_or_series",
                    "relation_source_field": "metadata.museum_description",
                    "relation_evidence": f"metadata.museum_description='{note_0[:200]}'",
                    "candidate_discovery_hint": "",
                    "editorial_bond_en": "",
                    "motif_slug": "documented_series",
                    "theme_label": "Documented Altarpiece / Companion Panels",
                    "reason_en": "Both museum catalog records share the exact curator note linking these outer panels as one work.",
                }

        # --- 1B-ter: Explicit sitter/family relation in museum title or description ---
        rel_01 = extract_sitter_relation(f"{cleaned[0].get('title', '')} {note_0}", cleaned[1].get("title", ""))
        rel_10 = extract_sitter_relation(f"{cleaned[1].get('title', '')} {note_1}", cleaned[0].get("title", ""))
        sitter_rel = rel_01 or rel_10
        if sitter_rel:
            return {
                "strong": True,
                "tier": "documented_pair_or_series",
                "relation_source_field": "title_or_description:sitter_relation",
                "relation_evidence": f"sitter_relation='{sitter_rel}'",
                "candidate_discovery_hint": "",
                "editorial_bond_en": "",
                "motif_slug": "documented_series",
                "theme_label": "Documented Family / Companion Portraits",
                "reason_en": f"Museum records explicitly connect the two sitters ({sitter_rel}).",
            }

        # --- 1C: Study-to-finished link ---
        for study_s, fin_s in ((t0_low, t1_low), (t1_low, t0_low)):
            m = re.search(r"(?:sketch|study)\s+for\s+[\"'“]?([^\"'”.,;]{4,40})", study_s)
            if m and m.group(1).strip().lower() in fin_s:
                return {
                    "strong": True,
                    "tier": "documented_pair_or_series",
                    "relation_source_field": "title:preparatory_study_match",
                    "relation_evidence": f"title:preparatory_study_match='{study_s}' -> '{fin_s}'",
                    "candidate_discovery_hint": "",
                    "editorial_bond_en": "",
                    "motif_slug": "study_vs_finished",
                    "theme_label": "Preparatory Study & Finished Work",
                    "reason_en": "Putting the preparatory study beside the finished composition lets the viewer spot which lines and figures survived into the final painting.",
                }

    # --- 2: Editorial Same-Artist Pairing (includes year+artist+title pendant candidates without explicit catalog link!) ---
    non_anon_artists = [a["artist_short"].lower() for a in cleaned if not a["is_anonymous"]]
    same_artist = len(non_anon_artists) == len(cleaned) and len(set(non_anon_artists)) == 1
    same_year = len(years) == len(cleaned) and len(set(years)) == 1
    if same_artist:
        artist_name = cleaned[0]["artist_short"]
        discovery_hint = ""
        bonds: list[str] = []
        if len(cleaned) == 2 and same_year:
            t_words = [set(re.findall(r"[a-z]+", a["title"].lower())) for a in cleaned]
            for pair_words in _SEASONAL_PENDANT_PAIRS:
                w_list = sorted(pair_words)
                if (w_list[0] in t_words[0] and w_list[1] in t_words[1]) or (w_list[1] in t_words[0] and w_list[0] in t_words[1]):
                    discovery_hint = "year_artist_title_pendant_candidate"
                    bonds.append(
                        f"places {artist_name}'s {years[0]} {w_list[0]} and {w_list[1]} canvases side by side (shared year/artist/title motif, curated editorially without an explicit catalog pair ID)"
                    )
                    break
        if len(years) >= 2 and max(years) != min(years):
            bonds.append(f"traces {artist_name}'s shift across {max(years) - min(years)} years ({min(years)}–{max(years)})")
        if "dark" in tones and "light" in tones:
            bonds.append("contrasts shadow-heavy lighting against open daylight")
        if "portrait" in orients and "landscape" in orients:
            bonds.append("contrasts vertical vs. horizontal framing")
        if not bonds:
            bonds.append("lets the viewer compare brushwork density, horizon height, and tonal balance side by side")
        bond_text = "; ".join(bonds)
        return {
            "strong": True,
            "tier": "editorial_same_artist",
            "relation_source_field": "",
            "relation_evidence": "",
            "candidate_discovery_hint": discovery_hint,
            "editorial_bond_en": f"Editorial pairing of two separate {artist_name} works: {bond_text}.",
            "motif_slug": "same_artist_dialogue",
            "theme_label": f"Editorial Study · {artist_name}",
            "reason_en": f"Editorial pairing of two separate {artist_name} works: {bond_text}.",
        }

    # --- 3: Editorial Cross-Artist Visual Contrast (shared motif + measurable visible contrast) ---
    motifs = [classify_artwork_motif(a) for a in cleaned]
    slugs = [m[0] for m in motifs]
    labels = {m[0]: m[1] for m in motifs}

    same_motif = (len(set(slugs)) == 1 and slugs[0] != "general_collection")
    if same_motif:
        theme = labels[slugs[0]]
        has_era_gap = len(years) >= 2 and (max(years) - min(years)) >= 80
        has_tone_contrast = ("dark" in tones and "light" in tones)
        has_orient_contrast = ("portrait" in orients and "landscape" in orients)

        if has_era_gap:
            span = max(years) - min(years)
            bond = f"Separated by roughly {span} years ({min(years)} to {max(years)}), these {theme.lower()} works let the viewer see how brushwork, lighting, and framing shifted over centuries."
            return {
                "strong": True,
                "tier": "editorial_visual_contrast",
                "relation_source_field": "",
                "relation_evidence": "",
                "candidate_discovery_hint": "",
                "editorial_bond_en": bond,
                "motif_slug": slugs[0],
                "theme_label": f"{theme} · {span} Years Apart",
                "reason_en": bond,
            }
        if has_tone_contrast:
            bond = f"Both works explore {theme.lower()}, yet one is built in deep chiaroscuro shadow while the other opens into bright daylight — making the lighting contrast immediately visible."
            return {
                "strong": True,
                "tier": "editorial_visual_contrast",
                "relation_source_field": "",
                "relation_evidence": "",
                "candidate_discovery_hint": "",
                "editorial_bond_en": bond,
                "motif_slug": slugs[0],
                "theme_label": f"{theme} · Dark vs. Daylight",
                "reason_en": bond,
            }
        if has_orient_contrast:
            bond = f"Both turn toward {theme.lower()}, contrasting a tall vertical crop against an expansive horizontal field so the viewer notices how format steers the eye."
            return {
                "strong": True,
                "tier": "editorial_visual_contrast",
                "relation_source_field": "",
                "relation_evidence": "",
                "candidate_discovery_hint": "",
                "editorial_bond_en": bond,
                "motif_slug": slugs[0],
                "theme_label": f"{theme} · Vertical vs. Wide Framing",
                "reason_en": bond,
            }

    return {
        "strong": False,
        "tier": "none",
        "relation_source_field": "",
        "relation_evidence": "",
        "candidate_discovery_hint": "",
        "editorial_bond_en": "",
        "motif_slug": slugs[0],
        "theme_label": "",
        "reason_en": "No documented museum relation or concrete visible contrast (era, lighting, or framing) found; convert to focused single-work Carousel.",
    }


def plan_final_carousel_slides(
    *,
    post_id: str,
    design_id: str,
    design_family: str,
    arts: Sequence[dict],
    is_quiz: bool = False,
    dimensions_cm_text: str = "",
    slide_two_intent: dict | None = None,
    use_color_card: bool = False,
) -> list[dict]:
    """Single source of truth for a Carousel's finalized slide sequence (cover + inner slides).

    Slide 2 function is decided BEFORE template/slide rendering, and every JPG page stamp
    (e.g. 02/03, 03/03), exported filename, per-slide Alt-Text, caption reference, and HTML preview
    is derived strictly from this finalized list.
    """
    from src.artwork_intelligence import should_show_monumental_scale_slide

    show_scale, _ = should_show_monumental_scale_slide(dimensions_cm_text)
    closing_spec = "save_card" if use_color_card else "credit_plate"
    closing_role = "archival_save_card" if use_color_card else "credit_plate"
    closing_label = "Arşiv & Renk Kartı" if use_color_card else "Tam Eser & Müze Künyesi"

    plan: list[dict] = [
        {
            "spec": "cover",
            "role": "cover",
            "label": "Kapak (Profil Görseli)",
            "artwork": arts[0],
            "is_cropped": design_family in CLOSEUP_COVER_FAMILIES,
        }
    ]

    if is_quiz and len(arts) == 1:
        plan.append({"spec": "quiz_reveal", "role": "quiz_reveal", "label": "Cevap Kartı", "artwork": arts[0], "is_cropped": False})
        plan.append({"spec": "full_plate", "role": "full_artwork", "label": "Tam Kırpılmamış Eser", "artwork": arts[0], "is_cropped": False})
        plan.append({"spec": closing_spec, "role": closing_role, "label": closing_label, "artwork": arts[0], "is_cropped": False})
    elif len(arts) >= 3:
        plan.append({"spec": "room_1_gallery", "role": "room_work", "label": f"Oda 01 · {arts[0]['artist_surname']}", "artwork": arts[0], "is_cropped": False})
        plan.append({"spec": "room_2_archive", "role": "room_work", "label": f"Oda 02 · {arts[1]['artist_surname']}", "artwork": arts[1], "is_cropped": False})
        plan.append({"spec": "room_3_colors", "role": "room_work", "label": f"Oda 03 · {arts[2]['artist_surname']}", "artwork": arts[2], "is_cropped": False})
    elif len(arts) == 2:
        if slide_two_intent and slide_two_intent.get("spec") == "duet_detail_compare":
            plan.append(
                {
                    "spec": "duet_detail_compare",
                    "role": "detail_compare",
                    "label": "Karşılaştırmalı Detay",
                    "artwork": arts[0],
                    "is_cropped": True,
                }
            )
        plan.append({"spec": "duet_work_1", "role": "full_artwork", "label": f"Eser 1 · {arts[0]['artist_surname']}", "artwork": arts[0], "is_cropped": False})
        plan.append({"spec": "duet_work_2", "role": "full_artwork", "label": f"Eser 2 · {arts[1]['artist_surname']}", "artwork": arts[1], "is_cropped": False})
        if use_color_card or not (slide_two_intent and slide_two_intent.get("spec") == "duet_detail_compare"):
            plan.append({"spec": closing_spec, "role": closing_role, "label": closing_label, "artwork": arts[1], "is_cropped": False})
    else:
        # Single-work Carousel: Slide 2 comes from slide_two_intent FIRST!
        s2_spec = (slide_two_intent or {}).get("spec") or "detail_callout"
        s2_role = (slide_two_intent or {}).get("role") or ("detail_callout" if s2_spec == "detail_callout" else "full_artwork")
        s2_label = (slide_two_intent or {}).get("label") or ("Yakın Detay İncelemesi" if s2_spec == "detail_callout" else "Tam Kırpılmamış Eser")
        s2_cropped = bool((slide_two_intent or {}).get("is_cropped", s2_spec == "detail_callout"))
        plan.append({"spec": s2_spec, "role": s2_role, "label": s2_label, "artwork": arts[0], "is_cropped": s2_cropped})

        # Add full_plate on Slide 3 ONLY if Slide 2 was a cropped detail AND Cover didn't already show the full artwork cleanly
        if s2_cropped and design_family not in FULL_FRAME_COVER_FAMILIES:
            plan.append({"spec": "full_plate", "role": "full_artwork", "label": "Tam Kırpılmamış Eser", "artwork": arts[0], "is_cropped": False})

        if show_scale:
            plan.append({"spec": "monumental_scale", "role": "monumental_scale", "label": "Anıtsal Ölçek (180+ cm)", "artwork": arts[0], "is_cropped": False})
        plan.append({"spec": closing_spec, "role": closing_role, "label": closing_label, "artwork": arts[0], "is_cropped": False})

    total = len(plan)
    for idx, item in enumerate(plan, 1):
        item["slide_index"] = idx
        item["total_slides"] = total
        item["page_sig"] = f"{idx:02d} / {total:02d}"
    return plan


def _compose_strong_multi_work_caption(
    artworks: Sequence[dict],
    conn: dict,
    slot_index: int,
    total_slides: int,
    *,
    has_detail_compare: bool = False,
) -> str:
    """Write a natural English caption citing the concrete connection, accurate support noun, & exact slides."""
    cleaned = [clean_museum_artwork(a) for a in artworks]
    theme = conn["theme_label"]
    takeaway = conn["reason_en"]
    n = len(cleaned)
    seed = int(hashlib.sha256(f"{cleaned[0]['canonical_id']}:{theme}:{slot_index}".encode("utf-8")).hexdigest()[:8], 16)
    support_sg = _support_noun(cleaned[0].get("medium", ""), plural=False)
    support_pl = _support_noun(cleaned[0].get("medium", ""), plural=True)

    if n == 2:
        a0, a1 = cleaned[0], cleaned[1]
        d0 = a0.get("date") or "date unrecorded"
        d1 = a1.get("date") or "date unrecorded"
        openings = (
            f'{theme}: "{a0["title_clean"]}" ({a0["artist_surname"]}, {d0}) beside "{a1["title_clean"]}" ({a1["artist_surname"]}, {d1}).',
            f'Why place {a0["artist_surname"]} ({d0}) next to {a1["artist_surname"]} ({d1})? {takeaway}',
        )
        opening = openings[seed % len(openings)]
        if has_detail_compare:
            body = (
                f"Slide 2 places both focal regions side by side before Slides 3 and 4 isolate each uncropped {support_sg} "
                f"({short_museum(a0['institution'])} and {short_museum(a1['institution'])})."
            )
        else:
            body = (
                f"Slides 2 and 3 isolate each {support_sg} uncropped ({short_museum(a0['institution'])} and {short_museum(a1['institution'])}), "
                f"before Slide {total_slides} closes with the museum record."
            )
        closings = (
            f"Between {a0['title_clean']} and {a1['title_clean']}, which side holds your eye longer?",
            f"Seeing both full {support_pl} in sequence makes the dialogue unmistakable.",
        )
        closing = closings[seed % len(closings)]
    else:
        a0, a1, a2 = cleaned[0], cleaned[1], cleaned[2]
        opening = f'{theme}: {a0["artist_surname"]}, {a1["artist_surname"]}, and {a2["artist_surname"]} in one {total_slides}-slide sequence.'
        body = f"{takeaway} Slides 2 to {total_slides} open each museum room uncropped."
        closing = f"Room 1 ({a0['artist_surname']}), Room 2 ({a1['artist_surname']}), or Room 3 ({a2['artist_surname']}) — which room slows you down most?"

    tags = build_targeted_hashtags(cleaned[0]["artist_short"], cleaned[0].get("institution", ""), "carousel")
    return f"{opening}\n\n{body}\n\n{closing}\n\n{tags}"


def build_per_slide_alt_texts(post: dict, slide_plan: Sequence[dict]) -> list[dict]:
    """Generate distinct, concrete English Alt-Text for EVERY slide derived from the finalized slide_plan."""
    arts = post["artworks"]
    lead = arts[0]
    museum_short = short_museum(lead.get("institution", "Museum Collection"))
    tone_word = {"dark": "deep shadows", "light": "bright daylight", "mid": "balanced mid-tones"}.get(
        post.get("tone_band", "mid"), "balanced mid-tones"
    )
    out: list[dict] = []
    for item in slide_plan:
        idx = item["slide_index"]
        total = item["total_slides"]
        role = item["role"]
        aw = item.get("artwork") or lead
        aw_museum = short_museum(aw.get("institution", museum_short))
        aw_date = aw.get("date") or "date unrecorded"
        if role == "cover":
            if len(arts) >= 2:
                txt = (
                    f"Slide {idx} of {total} (Cover): Editorial {len(arts)}-artwork layout pairing '{arts[0]['title_clean']}' by {arts[0]['artist_short']} "
                    f"with '{arts[1]['title_clean']}' by {arts[1]['artist_short']} ({post.get('pairing_theme', 'curatorial dialogue')})."
                )
            else:
                txt = (
                    f"Slide {idx} of {total} (Cover): {post['design_id']} editorial cover featuring '{aw['title_clean']}' by {aw['artist_short']} "
                    f"({aw_date}, {aw_museum}) in {tone_word}."
                )
        elif role == "detail_compare" and len(arts) >= 2:
            txt = (
                f"Slide {idx} of {total} (Side-by-Side Detail Comparison): Cropped focal regions of '{arts[0]['title_clean']}' and "
                f"'{arts[1]['title_clean']}' placed side by side to highlight pose, brushwork, and motif contrast."
            )
        elif role == "detail_callout":
            txt = (
                f"Slide {idx} of {total} (Detail Study): Close-up detail crop of {post.get('focus_region', 'the focal area')} in '{aw['title_clean']}' "
                f"by {aw['artist_short']}, paired with a small locator box showing its place in the full painting."
            )
        elif role == "quiz_reveal":
            txt = (
                f"Slide {idx} of {total} (Answer Reveal): Full view of '{aw['title_clean']}' by {aw['artist_short']} confirming creation year {aw_date} ({aw_museum})."
            )
        elif role == "full_artwork":
            txt = (
                f"Slide {idx} of {total} (Full Uncropped Plate): Uncropped view of '{aw['title_clean']}' by {aw['artist_short']} ({aw_date}, {aw_museum}) "
                f"with archival credit below the frame."
            )
        elif role == "room_work":
            txt = (
                f"Slide {idx} of {total} (Exhibition Room): Dedicated uncropped plate of '{aw['title_clean']}' by {aw['artist_short']} "
                f"({aw_date}, {aw_museum})."
            )
        elif role == "editorial_study":
            txt = (
                f"Slide {idx} of {total} (Curatorial Plate): Framed presentation of '{aw['title_clean']}' by {aw['artist_short']} "
                f"highlighting composition and {tone_word} ({aw_museum})."
            )
        elif role == "monumental_scale":
            txt = (
                f"Slide {idx} of {total} (Monumental Scale): Proportional museum wall view of '{aw['title_clean']}' beside a 170 cm visitor silhouette."
            )
        elif role == "credit_plate":
            txt = (
                f"Slide {idx} of {total} (Museum Record): Full uncropped view of '{aw['title_clean']}' by {aw['artist_short']} ({aw_date}, {aw_museum}) "
                f"with archival provenance caption."
            )
        else:
            txt = (
                f"Slide {idx} of {total} (Archival Reference Card): Complete view of '{aw['title_clean']}' by {aw['artist_short']} ({aw_date}, {aw_museum}) "
                f"above five sampled digital color swatches with hex codes."
            )
        if not txt.endswith("."):
            txt += "."
        out.append(
            {
                "slide_index": idx,
                "total_slides": total,
                "page_sig": item["page_sig"],
                "role": role,
                "role_label": item["label"],
                "is_cropped": bool(item.get("is_cropped", False)),
                "alt_text_en": txt,
            }
        )
    return out


def _week_folder_name(day_num: int, total_days: int = 30) -> str:
    if day_num <= 7:
        return "Week-01"
    if day_num <= 14:
        return "Week-02"
    if day_num <= 21:
        return "Week-03"
    if day_num <= 28:
        return "Week-04"
    if total_days <= 29:
        return "Week-05-Day-29"
    return f"Week-05-Days-29-{max(29, total_days)}"


def _slugify(text: str, limit: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:limit].rstrip("-")) or "artwork"


def _find_strong_partner_group(
    lead: dict,
    desired_total: int,
    pool_cleaned: Sequence[dict],
    used_counts: Counter[str],
    *,
    excluded_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[dict], dict] | None:
    """Search the verified pool for a Tier-1 (documented) or Tier-2 (concrete visible contrast) partner group.

    Never borrows artworks already reserved in excluded_ids (zero within-month repeats).
    Returns (group, connection_dict) or None if no strong partner exists (triggering single-work fallback).
    """
    lead_id = lead["canonical_id"]
    lead_series, _, _ = _extract_series_key(lead)
    lead_artist = lead["artist_short"].lower() if not lead["is_anonymous"] else ""
    lead_motif, _ = classify_artwork_motif(lead)

    # Candidate ordering: prefer documented relations first, then same artist, then same motif, then low usage
    def _rank(c: dict) -> tuple:
        c_series, _, _ = _extract_series_key(c)
        same_series = 0 if (lead_series and c_series == lead_series) else 1
        same_art = 0 if (lead_artist and not c["is_anonymous"] and c["artist_short"].lower() == lead_artist) else 1
        c_motif, _ = classify_artwork_motif(c)
        same_m = 0 if (lead_motif != "general_collection" and c_motif == lead_motif) else 1
        return (same_series, same_art, same_m, used_counts[c["canonical_id"]], c["canonical_id"])

    others = sorted(
        (c for c in pool_cleaned if c["canonical_id"] != lead_id and c["canonical_id"] not in excluded_ids),
        key=_rank,
    )

    # Try Tier 1 + Tier 2 pairs/trios greedily from top ranked candidates
    chosen = [lead]
    for cand in others[:80]:
        trial = [*chosen, cand]
        conn = evaluate_multi_work_connection(trial)
        if conn["strong"]:
            chosen = trial
            if len(chosen) == desired_total:
                return chosen, conn
    return None


def _check_material_sufficiency(arts: Sequence[dict]) -> str:
    """Only flag a post when source URL, rights status, or image quality is genuinely insufficient."""
    for a in arts:
        if not (a.get("museum_url") or "").startswith(("http://", "https://")):
            return f"missing_source_url:{a.get('canonical_id')}"
        rights = (a.get("rights_status") or a.get("rights_basis") or "").strip()
        if rights and rights not in VERIFIED_RIGHTS and "public domain" not in rights.lower() and "open" not in rights.lower():
            return f"unverified_rights:{rights}"
        p = Path(a.get("image_path", ""))
        if not p.is_file():
            return f"missing_source_image:{a.get('canonical_id')}"
        with Image.open(p) as im:
            if min(im.size) < 600:
                return f"low_resolution:{im.size[0]}x{im.size[1]}"
    return ""


def build_weekly_carousel_delivery(
    v2_rehearsal_dir: str | Path,
    pool_index_path: str | Path,
    output_dir: str | Path,
    *,
    reuse_ledger_path: str | Path | None = None,
    cooldown_days: int = 180,
    plan_id: str | None = None,
    ledger_status: str = "reserved",
    supply_report: dict | None = None,
    requested_days: int = 30,
    carousels_per_day: int = 4,
) -> dict:
    """Normal permanent Carousel delivery builder:
    - Applies strict documented vs editorial curatorial pairing (+ automatic single-work fallback).
    - Computes finalized slide plan FIRST so JPG page numbers (02/03, 03/03), filenames, per-slide Alt-Text, caption & HTML always match.
    - Respects month-scoped ArtworkReuseLedger so re-opening the same month never self-blocks while Month 2 skips Month 1's works.
    """
    src_root = Path(v2_rehearsal_dir).resolve()
    out_root = Path(output_dir).resolve()
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    effective_plan_id = plan_id or out_root.name
    v2_manifest = json.loads((src_root / "rehearsal_manifest.json").read_text(encoding="utf-8"))
    pool_raw = json.loads(Path(pool_index_path).read_text(encoding="utf-8"))["artworks"]
    pool_cleaned = [clean_museum_artwork(a) for a in pool_raw if Path(a.get("image_path", "")).is_file()]
    pool_by_id = {a["canonical_id"]: a for a in pool_cleaned}
    ledger = ArtworkReuseLedger(reuse_ledger_path) if reuse_ledger_path else None
    partner_pool = (
        [
            a
            for a in pool_cleaned
            if not ledger.is_blocked(a["canonical_id"], cooldown_days=cooldown_days, current_plan_id=effective_plan_id)
        ]
        if ledger is not None
        else pool_cleaned
    )

    used_in_carousels: Counter[str] = Counter()
    month_reserved_ids: set[str] = set()
    for p in v2_manifest["posts"]:
        if p["post_format"] == "carousel":
            if p["artworks"]:
                # Pre-register every Carousel's lead artwork BEFORE partner repair so no repair borrows another Carousel's lead!
                month_reserved_ids.add(p["artworks"][0]["canonical_id"])
            for a in p["artworks"]:
                used_in_carousels[a["canonical_id"]] += 1

    reels_dir = out_root / "reels_covers"
    reels_dir.mkdir(parents=True, exist_ok=True)
    tmp_render_root = out_root / "_tmp_renders"
    tmp_render_root.mkdir(parents=True, exist_ok=True)

    all_profile_posts: list[dict] = []
    usage_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    reused_cover_count = 0
    regenerated_cover_count = 0
    reused_inner_posts = 0
    regenerated_inner_posts = 0
    repaired_multi_work_count = 0
    fallback_to_single_count = 0
    material_issues: list[dict] = []
    week_counts: Counter[str] = Counter()
    slide_count_dist: Counter[int] = Counter()
    day_carousel_seq: Counter[int] = Counter()

    for post in v2_manifest["posts"]:
        if post["post_format"] == "reel":
            src_cover = src_root / post["image_path"]
            rel_reel = f"reels_covers/{post['id']}-{post['design_id']}.jpg"
            if src_cover.is_file():
                shutil.copyfile(src_cover, out_root / rel_reel)
            all_profile_posts.append(
                {
                    "id": post["id"],
                    "slot_number": post["slot_number"],
                    "day": post["day"],
                    "week": _week_folder_name(post["day"], total_days=requested_days),
                    "scheduled_at": post.get("scheduled_at", ""),
                    "scheduled_label": post["scheduled_label"],
                    "post_format": "reel",
                    "design_id": post["design_id"],
                    "family": post["family"],
                    "editorial_title": post["editorial_title"],
                    "image_path": rel_reel,
                    "slides": [rel_reel],
                    "slide_count": 1,
                    "reel_delivery_status": post.get("reel_delivery_status", "cover_ready_mp4_pending"),
                    "artworks": post["artworks"],
                }
            )
            continue

        day_num = post["day"]
        week_name = _week_folder_name(day_num, total_days=requested_days)
        day_carousel_seq[day_num] += 1
        seq_in_day = day_carousel_seq[day_num]
        arts = [clean_museum_artwork(pool_by_id.get(a["canonical_id"], a)) for a in post["artworks"]]
        design_id = post["design_id"]
        design_family = post["family"]
        headline = post["editorial_title"]
        caption_en = post["caption_en"]
        first_comment_en = post["first_comment_en"]
        focus_region = post.get("focus_region", "")
        curatorial_angle = post.get("curatorial_angle", "")
        curatorial_basis = post.get("curatorial_basis", "")
        pairing_theme = ""
        pairing_reason_en = ""
        relation_source_field = ""
        relation_evidence = ""
        candidate_discovery_hint = ""
        editorial_bond_en = ""
        pairing_decision = "single_artwork"
        re_render_cover = False
        force_re_render_inner = False

        # Guard against non-exact-year quiz template
        is_quiz = design_id in {"neon-151", "neon-157"} and len(arts) == 1 and arts[0]["date_is_exact_year"]
        if design_id in {"neon-151", "neon-157"} and not is_quiz:
            lead_prof = extract_artwork_visual_profile(arts[0]["image_path"], arts[0]["title_clean"])
            design_id, dec = select_profile_design(
                post_format="carousel",
                required_sources=1,
                lead_profile=lead_prof,
                placed_posts=all_profile_posts,
                usage_counts=usage_counts,
                family_counts=family_counts,
                is_quiz=False,
            )
            design_family = dec["family"]
            headline = arts[0]["title_clean"]
            re_render_cover = True
            force_re_render_inner = True

        # --- REQUIREMENT #2: Strict 2-tier general curatorial pairing + zero within-month repeats + single-work fallback ---
        if len(arts) >= 2:
            conn = evaluate_multi_work_connection(arts)
            secondary_collision = any(a["canonical_id"] in month_reserved_ids for a in arts[1:])
            if conn["strong"] and not secondary_collision:
                pairing_decision = f"kept_{conn['tier']}"
                for c in arts[1:]:
                    month_reserved_ids.add(c["canonical_id"])
            else:
                found = _find_strong_partner_group(
                    arts[0],
                    len(arts),
                    partner_pool,
                    used_in_carousels,
                    excluded_ids=month_reserved_ids,
                )
                if found is not None:
                    arts, conn = found
                    repaired_multi_work_count += 1
                    pairing_decision = f"repaired_{conn['tier']}"
                    re_render_cover = True
                    force_re_render_inner = True
                    for c in arts[1:]:
                        used_in_carousels[c["canonical_id"]] += 1
                        month_reserved_ids.add(c["canonical_id"])
                else:
                    # Automatic fallback: keep lead artwork and convert to focused single-artwork multi-slide Carousel!
                    arts = [arts[0]]
                    fallback_to_single_count += 1
                    pairing_decision = "fallback_single_carousel"
                    pairing_reason_en = "No unused strong museum partner found; converted automatically to a focused single-artwork multi-slide Carousel."
                    lead_prof = extract_artwork_visual_profile(arts[0]["image_path"], arts[0]["title_clean"])
                    design_id, dec = select_profile_design(
                        post_format="carousel",
                        required_sources=1,
                        lead_profile=lead_prof,
                        placed_posts=all_profile_posts,
                        usage_counts=usage_counts,
                        family_counts=family_counts,
                        is_quiz=False,
                    )
                    design_family = dec["family"]
                    headline = arts[0]["title_clean"]
                    brief = build_curatorial_brief(arts, post_format="carousel", slot_index=post["slot_number"], is_quiz=False)
                    caption_en = brief["caption_en"]
                    first_comment_en = brief["first_comment_en"]
                    focus_region = brief["focus_region"]
                    curatorial_angle = brief["curatorial_angle"]
                    curatorial_basis = f"{pairing_reason_en} {brief['curatorial_basis']}"
                    re_render_cover = True
                    force_re_render_inner = True

            if len(arts) >= 2:
                pairing_theme = conn["theme_label"]
                pairing_reason_en = conn["reason_en"]
                relation_source_field = conn.get("relation_source_field", "")
                relation_evidence = conn.get("relation_evidence", "")
                candidate_discovery_hint = conn.get("candidate_discovery_hint", "")
                editorial_bond_en = conn.get("editorial_bond_en", "")
                headline = compose_multi_work_headline(arts, conn)
                fc_lines = ["Museum Provenance & Source Record:"]
                for a in arts:
                    d_txt = a.get("date") or "Date unrecorded"
                    med = f" · {a['medium']}" if a.get("medium") else ""
                    ev_date = _extract_event_date_from_text(a.get("title", ""), a.get("description", ""), d_txt)
                    date_detail = f"Created: {d_txt} · Event depicted: {ev_date}" if ev_date else d_txt
                    fc_lines.append(f"• {a['artist']} — {a['title']} ({date_detail}){med}")
                    fc_lines.append(f"  {a.get('institution', 'Public Collection')} · {a.get('rights_basis', 'Public Domain')} · {a.get('museum_url', '')}")
                first_comment_en = "\n".join(fc_lines)

        # Check genuine material sufficiency (source URL, rights, image resolution)
        mat_err = _check_material_sufficiency(arts)
        if mat_err:
            material_issues.append({"post_id": post["id"], "week": week_name, "day": day_num, "reason": mat_err})

        # --- REQUIREMENT #1 & #2: Decide Slide 2 intent FIRST, then finalized slide list (Single Source of Truth!) ---
        lead_prof_final = extract_artwork_visual_profile(arts[0]["image_path"], arts[0]["title_clean"])
        brief_final = build_curatorial_brief(arts, post_format="carousel", slot_index=post["slot_number"], is_quiz=is_quiz)
        focus_pt = tuple(post.get("motion_start") or brief_final["motion_start"])
        s2_intent = decide_slide_two_intent(
            arts,
            focus=focus_pt,
            width=lead_prof_final["width"],
            height=lead_prof_final["height"],
            cover_shows_full=(design_family not in CLOSEUP_COVER_FAMILIES),
            use_color_card=brief_final.get("use_color_card", False),
        )
        final_slide_plan = plan_final_carousel_slides(
            post_id=post["id"],
            design_id=design_id,
            design_family=design_family,
            arts=arts,
            is_quiz=is_quiz,
            dimensions_cm_text=arts[0].get("dimensions_cm", ""),
            slide_two_intent=s2_intent,
            use_color_card=brief_final.get("use_color_card", False),
        )
        final_inner_specs = [item["spec"] for item in final_slide_plan[1:]]
        final_total_slides = len(final_slide_plan)

        if len(arts) >= 2:
            caption_en = _compose_strong_multi_work_caption(
                arts,
                conn,
                post["slot_number"],
                final_total_slides,
                has_detail_compare=("duet_detail_compare" in final_inner_specs),
            )
        elif re_render_cover:
            caption_en = brief_final["caption_en"]
            first_comment_en = brief_final["first_comment_en"]

        folder_slug = f"D{day_num:02d}-C{seq_in_day}_{post['id']}_{_slugify(arts[0]['title_clean'])}"
        post_dir = out_root / "weeks" / week_name / folder_slug
        post_dir.mkdir(parents=True, exist_ok=True)

        sources = [
            AstraArtworkInput(
                image_path=Path(a["image_path"]),
                title=a["title_clean"],
                artist=a["artist"],
                date=a.get("date", ""),
                institution=a.get("institution", ""),
                museum_url=a.get("museum_url", ""),
                focus=tuple(post["motion_start"]) if post.get("motion_start") else None,
            )
            for a in arts
        ]

        # Slide 1: Reuse unaffected cover; re-render only if design/pairing changed
        slide_1_dest = post_dir / "01_slide.jpg"
        existing_cover = src_root / post["image_path"]
        if not re_render_cover and existing_cover.is_file():
            shutil.copyfile(existing_cover, slide_1_dest)
            reused_cover_count += 1
        else:
            render_astra_design(design_id, sources, headline, slide_1_dest)
            regenerated_cover_count += 1

        # Inner slides (2..N): Reuse ONLY if count AND specs match exactly; if specs changed, ALWAYS re-render!
        existing_slides = [src_root / rel for rel in post.get("slides", [])[1:]]
        existing_inner_specs = list(post.get("slide_specs", [])[1:])
        can_reuse_inner = (
            not force_re_render_inner
            and len(existing_slides) == len(final_inner_specs)
            and existing_inner_specs == final_inner_specs
            and all(p.is_file() for p in existing_slides)
        )

        if can_reuse_inner:
            inner_files = list(existing_slides)
            reused_inner_posts += 1
        else:
            stub_post = MonthlyPlanPost(
                id=post["id"],
                post_format="carousel",
                design_id=design_id,
                editorial_title=headline,
                caption_en=caption_en,
                scheduled_at=post["scheduled_at"],
                artworks=tuple(
                    PilotArtworkRecord(
                        source_key=str(a["source_key"])[:64],
                        image_path=a["image_path"],
                        artist=str(a["artist"])[:160],
                        title=str(a["title_clean"])[:240],
                        date=str(a.get("date", ""))[:80],
                        institution=str(a.get("institution", ""))[:160],
                        museum_url=str(a.get("museum_url", ""))[:512],
                    )
                    for a in arts
                ),
                slide_count=final_total_slides,
                dimensions_cm_text=arts[0].get("dimensions_cm", ""),
            )
            rel_inners = _render_inner_carousel_slides(
                tmp_render_root,
                stub_post,
                sources,
                explicit_slide_specs=final_inner_specs,
            )
            inner_files = [tmp_render_root / rel for rel in rel_inners]
            regenerated_inner_posts += 1

        rel_slides = [slide_1_dest.relative_to(out_root).as_posix()]
        for s_idx, src_f in enumerate(inner_files, 2):
            dest_s = post_dir / f"{s_idx:02d}_slide.jpg"
            shutil.copyfile(src_f, dest_s)
            rel_slides.append(dest_s.relative_to(out_root).as_posix())

        tone = compute_cover_tone(slide_1_dest)
        usage_counts[design_id] += 1
        family_counts[design_family] += 1

        post_entry = {
            "id": post["id"],
            "slot_number": post["slot_number"],
            "day": day_num,
            "week": week_name,
            "folder_rel": post_dir.relative_to(out_root).as_posix(),
            "scheduled_at": post.get("scheduled_at", ""),
            "scheduled_label": post["scheduled_label"],
            "post_format": "carousel",
            "design_id": design_id,
            "family": design_family,
            "editorial_title": headline,
            "curatorial_angle": curatorial_angle,
            "curatorial_basis": pairing_reason_en or curatorial_basis,
            "focus_region": focus_region,
            "pairing_decision": pairing_decision,
            "pairing_theme": pairing_theme,
            "pairing_reason_en": pairing_reason_en,
            "relation_source_field": relation_source_field,
            "relation_evidence": relation_evidence,
            "candidate_discovery_hint": candidate_discovery_hint,
            "editorial_bond_en": editorial_bond_en,
            "caption_en": caption_en,
            "first_comment_en": first_comment_en,
            "tone_band": tone["tone_band"],
            "image_path": rel_slides[0],
            "slides": rel_slides,
            "slide_count": final_total_slides,
            "slide_specs": [item["spec"] for item in final_slide_plan],
            "artworks": [
                {
                    "canonical_id": a["canonical_id"],
                    "artist": a["artist"],
                    "artist_short": a["artist_short"],
                    "artist_surname": a["artist_surname"],
                    "title": a["title"],
                    "title_clean": a["title_clean"],
                    "date": a.get("date", ""),
                    "institution": a.get("institution", ""),
                    "museum_url": a.get("museum_url", ""),
                    "rights_basis": a.get("rights_basis", ""),
                }
                for a in arts
            ],
        }
        per_slide_alts = build_per_slide_alt_texts(post_entry, final_slide_plan)
        post_entry["slide_alt_texts_en"] = per_slide_alts
        post_entry["alt_text_en"] = per_slide_alts[0]["alt_text_en"]

        (post_dir / "caption_en.txt").write_text(caption_en.strip() + "\n", encoding="utf-8")
        (post_dir / "first_comment_en.txt").write_text(first_comment_en.strip() + "\n", encoding="utf-8")
        alt_lines = [
            f"{item['slide_index']:02d}_slide.jpg [{item['page_sig']} · {item['role_label']}]\n{item['alt_text_en']}\n"
            for item in per_slide_alts
        ]
        (post_dir / "alt_texts_by_slide_en.txt").write_text("\n".join(alt_lines), encoding="utf-8")
        (post_dir / "post_meta.json").write_text(
            json.dumps(post_entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        week_counts[week_name] += 1
        slide_count_dist[final_total_slides] += 1
        all_profile_posts.append(post_entry)

    shutil.rmtree(tmp_render_root, ignore_errors=True)

    carousels_only = [p for p in all_profile_posts if p["post_format"] == "carousel"]
    reels_only = [p for p in all_profile_posts if p["post_format"] == "reel"]
    exported_cids = sorted({a["canonical_id"] for p in carousels_only for a in p["artworks"]})
    # Compute schedule window strictly from scheduled_at (sorted) and filter by post_format
    all_sched_sorted = sorted((p for p in all_profile_posts if p.get("scheduled_at")), key=lambda x: x["scheduled_at"])
    carousels_sched_sorted = sorted((p for p in carousels_only if p.get("scheduled_at")), key=lambda x: x["scheduled_at"])
    sched_start = all_sched_sorted[0]["scheduled_at"][:10] if all_sched_sorted else ""
    sched_end = all_sched_sorted[-1]["scheduled_at"][:10] if all_sched_sorted else ""
    first_carousel_info = (
        {
            "id": carousels_sched_sorted[0]["id"],
            "scheduled_at": carousels_sched_sorted[0]["scheduled_at"],
            "scheduled_label": carousels_sched_sorted[0]["scheduled_label"],
        }
        if carousels_sched_sorted
        else None
    )
    last_carousel_info = (
        {
            "id": carousels_sched_sorted[-1]["id"],
            "scheduled_at": carousels_sched_sorted[-1]["scheduled_at"],
            "scheduled_label": carousels_sched_sorted[-1]["scheduled_label"],
        }
        if carousels_sched_sorted
        else None
    )
    calendar_slot_collisions = (
        ledger.find_schedule_collisions(sched_start, sched_end, exclude_plan_id=effective_plan_id)
        if ledger is not None and sched_start and sched_end
        else []
    )
    if ledger is not None and exported_cids:
        ledger.mark_used(
            exported_cids,
            plan_id=effective_plan_id,
            status=ledger_status,
            replace_plan=True,
            schedule_start=sched_start,
            schedule_end=sched_end,
        )

    # Verify zero unintended within-month repeats across different Carousels
    cid_post_map: dict[str, list[str]] = {}
    for p in carousels_only:
        for a in p["artworks"]:
            cid_post_map.setdefault(a["canonical_id"], []).append(p["id"])
    within_month_dups = {cid: pids for cid, pids in cid_post_map.items() if len(pids) > 1}

    reel_cids = {a["canonical_id"] for p in reels_only for a in p["artworks"]}
    carousel_cid_set = set(exported_cids)
    carousel_need = calculate_carousel_only_artwork_need(
        days=requested_days,
        carousels_per_day=carousels_per_day,
        reels_per_day=max(0, round(len(reels_only) / max(1, requested_days))),
        fallback_single_count=fallback_to_single_count,
    )
    day_counts_c = Counter(p["day"] for p in carousels_only)
    missing_carousel_days = [
        {"day": d, "completed_carousels": day_counts_c.get(d, 0), "missing_carousels": max(0, carousels_per_day - day_counts_c.get(d, 0))}
        for d in range(1, requested_days + 1)
        if day_counts_c.get(d, 0) < carousels_per_day
    ]
    expected_carousels = requested_days * carousels_per_day
    preflight = run_preflight_checks(carousels_only, out_root)
    for d in preflight["defects"]:
        if d["severity"] == "error":
            material_issues.append({"post_id": d["post_id"], "week": "", "day": 0, "reason": f"{d['code']}: {d['fix_hint']}"})

    summary = {
        "schema_version": "artfolio-carousel-delivery-v2",
        "profile_handle": "@artfolio.db",
        "portable": True,
        "plan_id": effective_plan_id,
        "ledger_status": ledger_status,
        "reuse_ledger_path": str(ledger.path) if ledger is not None else "",
        "schedule_window": {
            "start_date": sched_start,
            "end_date": sched_end,
            "days": requested_days,
            "first_carousel": first_carousel_info,
            "last_carousel": last_carousel_info,
        },
        "calendar_slot_collisions": calendar_slot_collisions,
        "within_month_duplicate_carousel_artworks": len(within_month_dups),
        "within_month_duplicate_details": within_month_dups,
        "expected_carousels": expected_carousels,
        "total_carousels": len(carousels_only),
        "shareable_carousels": len(carousels_only) - len({x["post_id"] for x in material_issues}),
        "missing_carousels_count": max(0, expected_carousels - len(carousels_only)),
        "missing_carousel_days": missing_carousel_days,
        "actual_unique_carousel_artworks_used": len(exported_cids),
        "carousel_artwork_need": carousel_need,
        "reel_preview_consumption": {
            "reel_covers_total": len(reels_only),
            "reel_unique_artworks_in_preview": len(reel_cids),
            "reel_only_artworks_not_in_carousels": len(reel_cids - carousel_cid_set),
            "reel_reusing_carousel_artworks": len(reel_cids.intersection(carousel_cid_set)),
            "excluded_from_carousel_shortfall": True,
        },
        "auto_supply_report": supply_report or {"auto_fetch_triggered": False},
        "total_carousel_slides": sum(p["slide_count"] for p in carousels_only),
        "slide_count_distribution": dict(sorted(slide_count_dist.items())),
        "week_breakdown": dict(sorted(week_counts.items())),
        "reused_v2_covers": reused_cover_count,
        "regenerated_covers": regenerated_cover_count,
        "reused_inner_slide_posts": reused_inner_posts,
        "regenerated_inner_slide_posts": regenerated_inner_posts,
        "repaired_multi_work_carousels": repaired_multi_work_count,
        "auto_fallback_to_single_carousels": fallback_to_single_count,
        "reel_neighbor_covers_included": len(reels_only),
        "preflight": {
            "passed": preflight["passed"],
            "error_count": preflight["error_count"],
            "warning_count": preflight["warning_count"],
        },
        "material_or_preflight_issues": material_issues,
    }
    manifest = {"summary": summary, "posts": all_profile_posts}
    (out_root / "delivery_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    html_path = out_root / "index.html"
    html_path.write_text(_render_portable_delivery_html(manifest), encoding="utf-8")
    return {
        "output_dir": str(out_root),
        "html_path": str(html_path),
        "manifest_path": str(out_root / "delivery_manifest.json"),
        "summary": summary,
    }


def _render_portable_delivery_html(manifest: dict) -> str:
    """100% self-contained portable preview (relative paths only — works on any Mac/PC without a server)."""
    payload = json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    s = manifest["summary"]
    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@artfolio.db · 30 Günlük Kaydırmalı Teslim Paketi (v2 · Kalıcı Üretim)</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f3f0e7;color:#1a1916;font:14px/1.5 system-ui,-apple-system,sans-serif}}
header{{background:#15211b;color:#f5edd8;padding:15px 22px;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px}}
header h1{{margin:0;font:20px/1.2 Georgia,serif;font-weight:normal}}
header p{{margin:4px 0 0;font-size:12px;color:#cfc4a8}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;background:#273b30;border:1px solid #456352;color:#f5edd8;margin-left:6px}}
.toolbar{{background:#fff;border-bottom:1px solid #dbd4c3;padding:10px 22px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}}
.btn-group{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.btn{{padding:6px 11px;border-radius:8px;border:1px solid #c5bca8;background:#faf8f2;color:#1a1916;font:12.5px system-ui;cursor:pointer}}
.btn.active{{background:#15211b;color:#fff9ed;border-color:#15211b}}
.layout{{max-width:1320px;margin:18px auto 48px;padding:0 16px;display:grid;grid-template-columns:minmax(360px,430px) minmax(0,1fr);gap:22px;align-items:start}}
@media(max-width:920px){{.layout{{grid-template-columns:1fr}}}}
.phone-frame{{background:#fff;border:2px solid #222;border-radius:26px;padding:12px 10px 16px;box-shadow:0 12px 32px rgba(0,0,0,.09)}}
.ig-header{{display:flex;align-items:center;gap:12px;padding:4px 6px 10px;border-bottom:1px solid #ece7db;margin-bottom:8px}}
.ig-avatar{{width:44px;height:44px;border-radius:50%;background:#15211b;color:#e5c377;display:flex;align-items:center;justify-content:center;font:15px Georgia,serif}}
.ig-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2px;max-height:74vh;overflow-y:auto}}
.ig-grid.ratio-3x4 .cell{{aspect-ratio:3/4}}
.cell{{position:relative;aspect-ratio:4/5;background:#e8e2d4;border:2px solid transparent;padding:0;cursor:pointer;overflow:hidden}}
.cell.selected{{border-color:#b57c2a;box-shadow:0 0 0 1px #b57c2a}}
.cell.reel-cell{{opacity:.88}}
.cell img{{width:100%;height:100%;object-fit:cover;display:block}}
.badge-tr{{position:absolute;top:4px;right:4px;background:rgba(16,16,14,.84);color:#fff;font-size:9px;padding:2px 4px;border-radius:3px}}
.badge-tr.reel{{background:#5d2b6e}}
.badge-bl{{position:absolute;bottom:4px;left:4px;background:rgba(16,16,14,.76);color:#f3e7c6;font-size:8.5px;padding:1px 4px;border-radius:3px}}
.panel{{background:#fff;border:1px solid #dcd5c5;border-radius:14px;padding:16px;box-shadow:0 6px 24px rgba(20,18,14,.05)}}
.inspector{{position:sticky;top:14px}}
.hero{{width:100%;max-height:420px;object-fit:contain;background:#f0ebe0;border-radius:8px;display:block;margin:8px 0}}
.swipe-bar{{display:none;align-items:center;justify-content:space-between;gap:8px;margin:6px 0}}
.swipe-bar.show{{display:flex}}
.slides-strip{{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;margin-bottom:8px}}
.slides-strip img{{height:68px;width:auto;border-radius:5px;border:2px solid transparent;cursor:pointer;opacity:.72}}
.slides-strip img.active{{border-color:#15211b;opacity:1}}
.box{{background:#f2f6f3;border:1px solid #c6d7cc;border-radius:8px;padding:8px 10px;font-size:12px;margin:8px 0}}
.art-card{{background:#faf8f3;border:1px solid #e6dfd0;border-radius:8px;padding:8px 10px;font-size:12.5px;margin:6px 0}}
.art-card a{{color:#1b4d39}}
.meta{{font-size:12px;color:#5a554a;margin:0 0 6px}}
label.lbl{{display:flex;justify-content:space-between;align-items:center;font-size:11px;font-weight:700;color:#4a463d;margin-top:8px;text-transform:uppercase}}
textarea{{width:100%;min-height:74px;padding:8px;border:1px solid #c9c1ae;border-radius:7px;background:#faf8f3;font:12.5px system-ui;margin-top:4px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>@artfolio.db · 30 Günlük Kaydırmalı Teslim Paketi (v2)</h1>
    <p>Kesinleşmiş slayt sırası · 2 kademeli eşleştirme + otomatik tekli dönüşüm · Taşınabilir paket</p>
  </div>
  <div>
    <span class="pill">{s['shareable_carousels']}/{s['total_carousels']} paylaşılabilir</span>
    <span class="pill">{s['total_carousel_slides']} slayt</span>
    <span class="pill">Preflight: {s['preflight']['error_count']} hata</span>
  </div>
</header>

<div class="toolbar">
  <div class="btn-group">
    <strong>Hafta:</strong>
    <button type="button" class="btn active" data-week="ALL">Tüm Profil</button>
    <button type="button" class="btn" data-week="Week-01">1. Hafta</button>
    <button type="button" class="btn" data-week="Week-02">2. Hafta</button>
    <button type="button" class="btn" data-week="Week-03">3. Hafta</button>
    <button type="button" class="btn" data-week="Week-04">4. Hafta</button>
    <button type="button" class="btn" data-week="Week-05-Days-29-30">Gün 29–30</button>
  </div>
  <div class="btn-group">
    <button type="button" class="btn active" id="btn-3x4">3:4 Profil</button>
    <button type="button" class="btn" id="btn-4x5">4:5 Tam</button>
  </div>
</div>

<div class="layout">
  <section class="phone-frame">
    <div class="ig-header">
      <div class="ig-avatar">af.</div>
      <div>
        <strong>artfolio.db</strong>
        <div class="meta" id="ig-stats" style="margin:0"></div>
      </div>
    </div>
    <div class="ig-grid ratio-3x4" id="ig-grid"></div>
  </section>

  <aside class="panel inspector">
    <h2 id="ins-title" style="margin:0 0 4px;font:20px Georgia,serif">Bir gönderi seçin</h2>
    <p class="meta" id="ins-meta"></p>
    <div class="box" id="ins-brief" style="display:none"></div>
    <img class="hero" id="ins-hero" alt="Slayt önizleme">
    <div class="swipe-bar" id="swipe-bar">
      <button type="button" class="btn" id="btn-prev">◀ Önceki Slayt</button>
      <strong id="slide-label" style="font-size:12.5px">1 / 3</strong>
      <button type="button" class="btn" id="btn-next">Sonraki Slayt ▶</button>
    </div>
    <div class="slides-strip" id="slides-strip"></div>

    <div id="carousel-fields">
      <label class="lbl"><span>Seçili Slaytın Alt Metni (Alt-Text)</span><button type="button" class="btn" id="copy-alt" style="padding:2px 8px;font-size:11px">Kopyala</button></label>
      <textarea id="ins-slide-alt" readonly style="min-height:52px"></textarea>

      <label class="lbl"><span>İngilizce Caption</span><button type="button" class="btn" id="copy-cap" style="padding:2px 8px;font-size:11px">Kopyala</button></label>
      <textarea id="ins-caption" readonly></textarea>

      <label class="lbl"><span>Kaynaklı İlk Yorum</span><button type="button" class="btn" id="copy-fc" style="padding:2px 8px;font-size:11px">Kopyala</button></label>
      <textarea id="ins-fc" readonly style="min-height:58px"></textarea>
    </div>
    <div id="ins-artworks"></div>
    <div class="meta" id="copy-status" style="margin-top:6px;color:#1b4d39"></div>
  </aside>
</div>

<script type="application/json" id="delivery-data">{payload}</script>
<script>
const data = JSON.parse(document.getElementById('delivery-data').textContent);
let activeWeek = 'ALL';
let selectedId = data.posts.find(p => p.post_format === 'carousel')?.id || data.posts[0]?.id;
let slideIdx = 0;

const gridEl = document.getElementById('ig-grid');
const statsEl = document.getElementById('ig-stats');
const swipeBar = document.getElementById('swipe-bar');
const slideLabel = document.getElementById('slide-label');
const slidesStrip = document.getElementById('slides-strip');

function filteredPosts() {{
  const base = activeWeek === 'ALL'
    ? data.posts
    : data.posts.filter(p => p.week === activeWeek && p.post_format === 'carousel');
  return [...base].reverse();
}}

function renderGrid() {{
  const list = filteredPosts();
  const cCount = list.filter(p => p.post_format === 'carousel').length;
  const rCount = list.filter(p => p.post_format === 'reel').length;
  statsEl.textContent = cCount + ' Kaydırmalı' + (rCount ? (' + ' + rCount + ' Reels komşusu') : '') + ' · ' + activeWeek;
  gridEl.innerHTML = '';
  for (const p of list) {{
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cell' + (p.id === selectedId ? ' selected' : '') + (p.post_format === 'reel' ? ' reel-cell' : '');
    const tag = p.post_format === 'reel' ? '<span class="badge-tr reel">REEL</span>' : '<span class="badge-tr">1/' + p.slide_count + '</span>';
    btn.innerHTML = '<img src="' + p.image_path + '" alt="">' + tag + '<span class="badge-bl">G' + p.day + '</span>';
    btn.addEventListener('click', () => {{ selectedId = p.id; slideIdx = 0; renderGrid(); renderInspector(); }});
    gridEl.appendChild(btn);
  }}
}}

function showSlide(p, idx) {{
  const slides = p.slides || [p.image_path];
  slideIdx = Math.max(0, Math.min(slides.length - 1, idx));
  document.getElementById('ins-hero').src = slides[slideIdx];
  const altObj = (p.slide_alt_texts_en || [])[slideIdx];
  slideLabel.textContent = (altObj ? altObj.page_sig : ((slideIdx + 1) + ' / ' + slides.length)) + (altObj ? (' · ' + altObj.role_label) : '');
  document.getElementById('ins-slide-alt').value = altObj ? altObj.alt_text_en : '';
  [...slidesStrip.children].forEach((el, i) => el.classList.toggle('active', i === slideIdx));
}}

function renderInspector() {{
  const p = data.posts.find(x => x.id === selectedId) || filteredPosts()[0];
  if (!p) return;
  selectedId = p.id;
  document.getElementById('ins-title').textContent = p.editorial_title;
  document.getElementById('ins-meta').textContent =
    p.week + ' · Gün ' + p.day + ' · Planlanan yayın tarihi: ' + (p.scheduled_at ? p.scheduled_at.slice(0, 16).replace('T', ' ') : p.scheduled_label) + ' · ' + p.post_format.toUpperCase() +
    ' · ' + p.design_id + ' [' + p.family + ']' + (p.pairing_decision ? (' · Karar: ' + p.pairing_decision) : '');

  const briefEl = document.getElementById('ins-brief');
  if (p.pairing_reason_en) {{
    briefEl.style.display = 'block';
    briefEl.innerHTML = '<strong>Eşleştirme Kararı (' + (p.pairing_theme || p.pairing_decision) + '):</strong> ' + p.pairing_reason_en;
  }} else if (p.curatorial_basis) {{
    briefEl.style.display = 'block';
    briefEl.innerHTML = '<strong>Küratör Gerekçesi:</strong> ' + p.curatorial_basis;
  }} else {{
    briefEl.style.display = 'none';
  }}

  const isCarousel = p.post_format === 'carousel';
  document.getElementById('carousel-fields').style.display = isCarousel ? 'block' : 'none';
  if (isCarousel) {{
    document.getElementById('ins-caption').value = p.caption_en || '';
    document.getElementById('ins-fc').value = p.first_comment_en || '';
  }}

  const slides = p.slides || [p.image_path];
  if (slideIdx >= slides.length) slideIdx = 0;
  slidesStrip.innerHTML = '';
  swipeBar.classList.toggle('show', slides.length > 1);
  if (slides.length > 1) {{
    slides.forEach((src, i) => {{
      const img = document.createElement('img');
      img.src = src; img.alt = 'Slayt ' + (i + 1);
      img.addEventListener('click', () => showSlide(p, i));
      slidesStrip.appendChild(img);
    }});
  }}
  showSlide(p, slideIdx);

  document.getElementById('ins-artworks').innerHTML = (p.artworks || []).map(a =>
    '<div class="art-card"><strong>' + a.title + '</strong> — ' + a.artist + (a.date ? ' (' + a.date + ')' : '') +
    '<br><span class="meta">' + a.institution + ' · ' + (a.rights_basis || 'Public Domain') + '</span>' +
    (a.museum_url ? ' · <a href="' + a.museum_url + '" target="_blank" rel="noopener">Müze ↗</a>' : '') + '</div>'
  ).join('');
}}

document.querySelectorAll('[data-week]').forEach(btn => {{
  btn.addEventListener('click', () => {{
    activeWeek = btn.dataset.week;
    document.querySelectorAll('[data-week]').forEach(b => b.classList.toggle('active', b === btn));
    const vis = filteredPosts();
    if (vis.length) {{ selectedId = vis[0].id; slideIdx = 0; }}
    renderGrid(); renderInspector();
  }});
}});
document.getElementById('btn-3x4').addEventListener('click', () => {{ gridEl.classList.add('ratio-3x4'); document.getElementById('btn-3x4').classList.add('active'); document.getElementById('btn-4x5').classList.remove('active'); }});
document.getElementById('btn-4x5').addEventListener('click', () => {{ gridEl.classList.remove('ratio-3x4'); document.getElementById('btn-4x5').classList.add('active'); document.getElementById('btn-3x4').classList.remove('active'); }});
document.getElementById('btn-prev').addEventListener('click', () => {{ const p = data.posts.find(x => x.id === selectedId); if (p) showSlide(p, slideIdx - 1); }});
document.getElementById('btn-next').addEventListener('click', () => {{ const p = data.posts.find(x => x.id === selectedId); if (p) showSlide(p, slideIdx + 1); }});
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {{
    const p = data.posts.find(x => x.id === selectedId);
    if (p && p.slides && p.slides.length > 1) showSlide(p, slideIdx + (e.key === 'ArrowRight' ? 1 : -1));
  }}
}});
async function copyField(id, label) {{
  try {{ await navigator.clipboard.writeText(document.getElementById(id).value); document.getElementById('copy-status').textContent = label + ' kopyalandı.'; }}
  catch (e) {{ document.getElementById('copy-status').textContent = 'Kopyalama hatası: ' + e.message; }}
}}
document.getElementById('copy-alt').addEventListener('click', () => copyField('ins-slide-alt', 'Slayt Alt-Text'));
document.getElementById('copy-cap').addEventListener('click', () => copyField('ins-caption', 'Caption'));
document.getElementById('copy-fc').addEventListener('click', () => copyField('ins-fc', 'İlk yorum'));

renderGrid();
renderInspector();
</script>
</body>
</html>
"""
