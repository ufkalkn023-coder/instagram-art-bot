"""Curatorial brief builder & natural English editorial voice for @artfolio.db.

Grounds every post's copy, crop, slide progression, Alt-Text, first comment, and Reel subtitle
in a single verified curatorial decision derived strictly from museum metadata and visible image analysis.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Sequence

from PIL import Image

from src.artwork_intelligence import can_support_detail_crop, detect_smart_focus
from src.editorial_copy_rules import _describe_palette_words, build_targeted_hashtags
from src.grid_choreography import compute_cover_tone, extract_primary_secondary_rgb
from src.neon_kit import hexcol, short_museum

COPY_VERSION = "artfolio-voice-v3"

_ATTRIBUTION_PAREN_RE = re.compile(r"\s*\(([^)]*)\)")
_TRAILING_NUM_RE = re.compile(r"\s+\d+$")
_DANGLING_WORDS = frozenset(
    {"in", "the", "of", "at", "with", "for", "and", "a", "an", "on", "to", "from", "by", "de", "van", "prickly"}
)


def derive_museum_url(canonical_id: str, existing_url: str = "") -> str:
    """Return verified museum object URL, deriving canonical official links when local handoffs omitted them."""
    clean = (existing_url or "").strip()
    if clean.startswith(("http://", "https://")):
        return clean
    cid = (canonical_id or "").strip()
    if cid.startswith("met_"):
        return f"https://www.metmuseum.org/art/collection/search/{cid[4:]}"
    if cid.startswith("aic_"):
        return f"https://www.artic.edu/artworks/{cid[4:]}"
    if cid.startswith("rijksmuseum_"):
        return f"https://www.rijksmuseum.nl/en/collection/{cid[12:]}"
    if cid.startswith("cleveland_"):
        return f"https://www.clevelandart.org/art/{cid[10:]}"
    return ""


def clean_artist_record(raw_artist: str) -> dict:
    """Clean noisy museum artist strings while strictly preserving attribution uncertainty.

    Examples:
      'Abraham Mignon (mentioned on object)' -> display='Abraham Mignon (attributed / mentioned on object)',
          short_name='Abraham Mignon', surname='Mignon', attribution_note='mentioned on object', is_anonymous=False
      'Banavari 1' -> display='Banavari', short_name='Banavari', surname='Banavari'
      'anonymous' / 'Unknown Artist' -> display='Unknown artist', short_name='Unknown artist', is_anonymous=True
    """
    raw = " ".join(str(raw_artist or "").split()).strip()
    if not raw or raw.lower() in {"anonymous", "unknown", "unknown artist", "unidentified artist"}:
        return {
            "raw": raw or "Unknown artist",
            "display": "Unknown artist",
            "short_name": "Unknown artist",
            "surname": "Unknown artist",
            "attribution_note": "unattributed",
            "is_anonymous": True,
        }

    notes = [m.group(1).strip() for m in _ATTRIBUTION_PAREN_RE.finditer(raw) if m.group(1).strip()]
    base = _ATTRIBUTION_PAREN_RE.sub("", raw).strip()
    base = _TRAILING_NUM_RE.sub("", base).strip(" ,;-")
    if not base or base.lower() in {"anonymous", "unknown"}:
        return {
            "raw": raw,
            "display": "Unknown artist",
            "short_name": "Unknown artist",
            "surname": "Unknown artist",
            "attribution_note": "; ".join(notes) or "unattributed",
            "is_anonymous": True,
        }

    # Extract clean surname (never 'object)', 'artist)', or a trailing digit)
    parts = [p.strip(".,;") for p in base.split() if p.strip(".,;")]
    surname = parts[-1] if parts else base
    if len(parts) >= 2 and parts[-2].lower() in {"van", "de", "da", "del", "della", "la", "le", "von"}:
        surname = f"{parts[-2]} {parts[-1]}"

    attr_note = "; ".join(notes)
    if any(k in attr_note.lower() for k in ("mentioned on object", "attributed", "circle of", "workshop", "follower")):
        display = f"{base}, attributed"
    else:
        display = base

    return {
        "raw": raw,
        "display": display,
        "short_name": base,
        "surname": surname,
        "attribution_note": attr_note,
        "is_anonymous": False,
    }


def is_exact_year_date(date_text: str) -> bool:
    """True only when museum date is a single certain 4-digit year (safe for 'Guess the year' quizzes)."""
    return bool(re.fullmatch(r"\s*\d{4}\s*", str(date_text or "")))


def clean_editorial_title(title: str, max_len: int = 50) -> str:
    """Trim overly long archival titles cleanly at phrase boundaries without dangling words or broken quotes."""
    clean = " ".join(str(title or "").split()).strip()
    if not clean:
        return "Untitled Work"
    # If museum title has a long archival sub-biography after a period, keep the primary title clause
    if len(clean) > max_len and ". " in clean[: max_len + 12]:
        first_clause = clean.split(". ", 1)[0].strip()
        if len(first_clause) >= 10:
            clean = first_clause
    if len(clean) <= max_len:
        return clean
    cut = clean[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-(")
    words = cut.split()
    while len(words) > 2 and words[-1].lower().strip(".,;:") in _DANGLING_WORDS:
        words.pop()
    res = " ".join(words) or clean[:max_len]
    # Balance double quotes if trimming split a quoted title
    if res.count('"') % 2 == 1:
        res = res.rstrip('"') + '"'
    return res


def detect_series_or_study_relation(title: str) -> str:
    """Detect verifiable study/sketch/series relations stated directly in the museum title."""
    t = (title or "").lower()
    if any(k in t for k in ("sketch for", "study for", "unfinished study", "preliminary")):
        return "preparatory_study"
    if any(k in t for k in ("from an ", "from the ", "series", "album", "book of")):
        return "series_or_manuscript"
    if any(k in t for k in ("pendant", "pair", "diptych", "triptych")):
        return "paired_work"
    return ""


def clean_museum_artwork(raw: dict) -> dict:
    """Normalize a verified museum record without losing attribution uncertainty, date range, or source URL."""
    artist_info = clean_artist_record(raw.get("artist", ""))
    canonical_id = raw.get("canonical_id") or raw.get("source_key") or ""
    museum_url = derive_museum_url(canonical_id, raw.get("museum_url", ""))
    return {
        **raw,
        "canonical_id": canonical_id,
        "artist": artist_info["display"],
        "artist_short": artist_info["short_name"],
        "artist_surname": artist_info["surname"],
        "attribution_note": artist_info["attribution_note"],
        "is_anonymous": artist_info["is_anonymous"],
        "title_clean": clean_editorial_title(raw.get("title", ""), max_len=50),
        "date": " ".join(str(raw.get("date") or "").split()).strip(),
        "museum_url": museum_url,
        "date_is_exact_year": is_exact_year_date(raw.get("date", "")),
        "series_relation": detect_series_or_study_relation(raw.get("title", "")),
    }


def _support_noun(medium: str, *, plural: bool = False) -> str:
    """Return accurate physical support noun ('panel', 'sheet', 'canvas', or 'work') from museum medium."""
    m = (medium or "").lower()
    if any(tok in m for tok in ("panel", "wood", "board", "paneel", "oak", "poplar")):
        return "panels" if plural else "panel"
    if any(tok in m for tok in ("paper", "silk", "scroll", "parchment", "vellum", "hanging")):
        return "sheets" if plural else "sheet"
    if "canvas" in m or "doek" in m or "linen" in m:
        return "canvases" if plural else "canvas"
    return "works" if plural else "work"


_EVENT_DATE_RE = re.compile(
    r"\b(\d{1,2}(?:[-–]\d{1,2})?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\b",
    re.IGNORECASE,
)


def _extract_event_date_from_text(title: str, description: str, creation_date: str) -> str:
    """Extract historical event date (e.g. '19 November 1421') when distinct from artwork creation date."""
    combined = f"{title or ''} {description or ''}"
    m = _EVENT_DATE_RE.search(combined)
    if not m:
        return ""
    event_date = m.group(1).strip()
    yr_m = re.search(r"\d{4}", event_date)
    if yr_m and yr_m.group(0) in (creation_date or ""):
        return ""
    return event_date


def _focus_region_phrase(fx: float, fy: float) -> str:
    v = "upper" if fy < 0.38 else ("lower" if fy > 0.62 else "middle")
    h = "left" if fx < 0.38 else ("right" if fx > 0.62 else "center")
    if v == "middle" and h == "center":
        return "the center of the composition"
    return f"the {v}-{h} section of the frame"


def decide_slide_two_intent(
    cleaned_arts: Sequence[dict],
    *,
    focus: tuple[float, float],
    width: int,
    height: int,
    cover_shows_full: bool = True,
    use_color_card: bool = False,
) -> dict:
    """Decide what the viewer sees for the FIRST TIME on Slide 2 before template/slide selection."""
    lead = cleaned_arts[0]
    focus_phrase = _focus_region_phrase(focus[0], focus[1])
    support_sg = _support_noun(lead.get("medium", ""), plural=False)
    can_crop, crop_reason = can_support_detail_crop(width, height)

    if len(cleaned_arts) >= 2:
        a0, a1 = cleaned_arts[0], cleaned_arts[1]
        can_crop2 = True
        p2 = Path(a1.get("image_path", ""))
        if p2.is_file():
            with Image.open(p2) as im2:
                can_crop2, _ = can_support_detail_crop(*im2.size, zoom=1.85, min_crop_edge=380)
        if can_crop and can_crop2:
            return {
                "spec": "duet_detail_compare",
                "role": "detail_compare",
                "label": "Karşılaştırmalı Detay",
                "is_cropped": True,
                "focus": list(focus),
                "needs_locator": False,
                "fallback_reason": "",
                "use_color_card": use_color_card,
                "gain_en": f"Side-by-side close comparison of '{a0['title_clean']}' and '{a1['title_clean']}' before opening each full {support_sg}.",
                "gain_tr": f"İki eserin odak bölgelerini yan yana açarak pozu, fırçayı ve kompozisyon bağını doğrudan gösterir.",
            }
        return {
            "spec": "duet_work_1",
            "role": "full_artwork",
            "label": f"Eser 1 · {a0['artist_surname']}",
            "is_cropped": False,
            "focus": list(focus),
            "needs_locator": False,
            "fallback_reason": crop_reason or "partner_resolution_too_low_for_side_by_side_crop",
            "use_color_card": use_color_card,
            "gain_en": f"Full uncropped view of '{a0['title_clean']}' to begin the pair comparison.",
            "gain_tr": "Kaynak çözünürlüğü kırpımı desteklemediğinden ilk eseri tam ve kırpılmamış olarak açar.",
        }

    # Single-work Carousel: Cover already tight close-up -> Slide 2 opens the full artwork!
    if not cover_shows_full:
        return {
            "spec": "full_plate",
            "role": "full_artwork",
            "label": "Tam Kırpılmamış Eser",
            "is_cropped": False,
            "focus": list(focus),
            "needs_locator": False,
            "fallback_reason": "",
            "use_color_card": use_color_card,
            "gain_en": f"Steps back from the cover close-up to show the full uncropped {support_sg} of '{lead['title_clean']}'.",
            "gain_tr": "Kapaktaki yakın plandan geri çekilerek ayrıntının bütün eserdeki yerini gösterir.",
        }

    # Single-work Carousel: Cover shows general view -> Slide 2 zooms into meaningful detail if resolution allows!
    if can_crop:
        return {
            "spec": "detail_callout",
            "role": "detail_callout",
            "label": "Yakın Detay İncelemesi",
            "is_cropped": True,
            "focus": list(focus),
            "needs_locator": True,
            "fallback_reason": "",
            "use_color_card": use_color_card,
            "gain_en": f"Zooms into {focus_phrase} of '{lead['title_clean']}' with a locator box showing where the detail sits.",
            "gain_tr": f"Genel görünümdeki kapağın ardından {focus_phrase} bölgesini konum kutusuyla yakından gösterir.",
        }

    return {
        "spec": "full_plate",
        "role": "full_artwork",
        "label": "Tam Kırpılmamış Eser",
        "is_cropped": False,
        "focus": list(focus),
        "needs_locator": False,
        "fallback_reason": crop_reason,
        "use_color_card": use_color_card,
        "gain_en": f"Presents the full uncropped {support_sg} without forcing a blurry zoom.",
        "gain_tr": f"Kaynak çözünürlüğü ({width}x{height}) yakın kırpımı kaldırmadığı için eserin bütününü net gösterir.",
    }


def _date_phrase(date_str: str) -> str:
    d = (date_str or "").strip()
    if not d:
        return ""
    if re.fullmatch(r"\d{4}", d):
        return f"from {d}"
    return f"dated {d}"


_SITTER_RELATION_RE = re.compile(
    r"\b(?:half[-\s]?brother|half[-\s]?sister|brother|sister|wife|husband|son|daughter)\s+of\s+([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){1,3})",
    re.IGNORECASE,
)


def extract_sitter_relation(title_or_desc_a: str, title_b: str) -> str:
    """Detect explicit family/companion sitter relation in work A pointing to work B's title."""
    tb_low = (title_b or "").lower()
    for m in _SITTER_RELATION_RE.finditer(title_or_desc_a or ""):
        person = m.group(1).strip(" .,;:")
        if person and person.lower() in tb_low:
            return m.group(0).strip()
    return ""


def compose_multi_work_headline(arts: Sequence[dict], conn: dict | None = None) -> str:
    """Compose a concise, readable English cover headline for 2- or 3-work Carousels (never 'Panels / Panels')."""
    if len(arts) >= 3:
        surnames = [a.get("artist_surname") or "Archive" for a in arts[:3]]
        if len(set(s.lower() for s in surnames)) == 3:
            return f"{surnames[0]}, {surnames[1]} & {surnames[2]}"
        return f"{arts[0]['title_clean'][:28]} · 3 Works"

    a0, a1 = arts[0], arts[1]
    s0 = (a0.get("artist_surname") or "Archive").strip()
    s1 = (a1.get("artist_surname") or "Archive").strip()
    if s0.lower() != s1.lower():
        return f"{s0} / {s1}"

    # Same artist or shared surname: NEVER produce 'Surname / Surname'
    combined_0 = f"{a0.get('title', '')} {a0.get('description', '')}"
    combined_1 = f"{a1.get('title', '')} {a1.get('description', '')}"
    rel_01 = extract_sitter_relation(combined_0, a1.get("title", ""))
    rel_10 = extract_sitter_relation(combined_1, a0.get("title", ""))
    rel = rel_01 or rel_10
    d0, d1 = (a0.get("date") or "").strip(), (a1.get("date") or "").strip()
    if rel and ("half-brother" in rel.lower() or "half brother" in rel.lower() or "half-sister" in rel.lower() or "half sister" in rel.lower()):
        if d0 and d0 == d1 and re.fullmatch(r"\d{4}", d0):
            return f"Half-Siblings, Painted in {d0}"
        return "Half-Siblings: Two Portraits"

    desc_both = f"{a0.get('description', '')} {a1.get('description', '')}".lower()
    if any(k in desc_both for k in ("altarpiece", "outer panels", "outer wings", "vleugel")):
        base_t = clean_editorial_title(a0.get("title_clean") or a0.get("title", ""), max_len=34)
        return f"{base_t}: Outer Wings"

    t0 = clean_editorial_title(a0.get("title_clean") or a0.get("title", ""), max_len=24)
    t1 = clean_editorial_title(a1.get("title_clean") or a1.get("title", ""), max_len=24)
    if t0.lower() != t1.lower() and len(f"{t0} & {t1}") <= 46:
        return f"{t0} & {t1}"
    artist_lbl = a0.get("artist_short") or s0
    if d0 and d1 and d0 != d1:
        return f"{artist_lbl} ({d0} / {d1})"[:46]
    return f"{artist_lbl}: Two Works"[:46]


def build_curatorial_brief(
    artworks: Sequence[dict],
    *,
    post_format: str,
    slot_index: int,
    revisits_post_id: str = "",
    is_quiz: bool = False,
) -> dict:
    """Build ONE unified curatorial brief governing caption, first comment, Alt-Text, slide note & Reel motion.

    Guarantees:
    - No repeating the same sentence across caption / first comment / Alt-Text / Reel subtitle.
    - No 'pigment' claims for digital color swatches (uses 'color palette' / 'sampled tones').
    - No generic 'Look / Notice / Watch' verb-swapping.
    - Grounded strictly in museum metadata + measured visual properties (orientation, tone, focus, palette).
    """
    cleaned = [clean_museum_artwork(a) for a in artworks]
    lead = cleaned[0]
    img_path = Path(lead["image_path"])

    with Image.open(img_path) as im:
        w, h = im.size
        fx, fy = detect_smart_focus(im)
    aspect = w / max(1, h)
    orientation = "wide horizontal" if aspect >= 1.2 else ("vertical" if aspect <= 0.84 else "nearly square")
    tone_info = compute_cover_tone(img_path)
    pri_rgb, sec_rgb = extract_primary_secondary_rgb(img_path)
    palette_hex = [hexcol(pri_rgb), hexcol(sec_rgb)]
    palette_words = _describe_palette_words(palette_hex)
    focus_phrase = _focus_region_phrase(fx, fy)
    museum_short = short_museum(lead.get("institution", "Museum Collection"))
    date_txt = (lead.get("date") or "").strip()
    date_part = _date_phrase(date_txt)
    support_sg = _support_noun(lead.get("medium", ""), plural=False)
    support_pl = _support_noun(lead.get("medium", ""), plural=True)
    # Color card only when color/tone contrast is genuinely central (not default on every post)
    color_spread = sum(abs(int(a) - int(b)) for a, b in zip(pri_rgb, sec_rgb))
    use_color_card = color_spread >= 165 or (slot_index % 4 == 0 and color_spread >= 110)
    slide_two_intent = decide_slide_two_intent(
        cleaned,
        focus=(fx, fy),
        width=w,
        height=h,
        cover_shows_full=True,
        use_color_card=use_color_card,
    )

    # 1) Determine primary curatorial angle grounded in evidence
    if revisits_post_id and post_format == "reel":
        angle = "revisit_motion_discovery"
        basis = f"Revisits {revisits_post_id} in motion: opens tightly on {focus_phrase}, then pulls back to the full {orientation} frame."
    elif is_quiz and lead["date_is_exact_year"]:
        angle = "exact_year_quiz"
        basis = f"Verified exact creation year ({date_txt}) supports a fair dating quiz."
    elif len(cleaned) >= 3:
        angle = "three_work_dialogue"
        basis = f"Three works from {len({a.get('institution') for a in cleaned})} collection(s) placed in sequence for direct comparison."
    elif len(cleaned) == 2:
        angle = "dialogue_pair"
        basis = f"Two works paired side by side ({cleaned[0]['artist_surname']} and {cleaned[1]['artist_surname']}) to contrast framing and tonal weight."
    elif lead["series_relation"] == "preparatory_study":
        angle = "study_process"
        basis = f"Museum record identifies this work as a preparatory study/sketch ({lead['title_clean']})."
    elif lead["series_relation"] == "series_or_manuscript":
        angle = "series_context"
        basis = f"Museum title places this work inside a larger series or manuscript ({lead['title_clean']})."
    elif aspect >= 1.25:
        angle = "composition_whole"
        basis = f"Wide {orientation} format ({w}x{h}px) rewards seeing the full horizontal span before isolating details."
    else:
        angle = "visual_detail"
        basis = f"High-contrast focal density around {focus_phrase} anchors the {tone_info['tone_band']} tonal structure."

    # Deterministic per-post seed so phrasing varies naturally without repeating the same opening/closing
    seed = int(hashlib.sha256(f"{lead['canonical_id']}:{post_format}:{slot_index}".encode("utf-8")).hexdigest()[:8], 16)

    # 2) Compose natural human English Caption (artwork-specific, varied endings: some quiet observations, some natural questions)
    artist_label = "an unattributed hand" if lead["is_anonymous"] else lead["artist_short"]
    title_q = f'"{lead["title_clean"]}"'
    tone_word = {"dark": "deep shadows", "light": "open daylight", "mid": "measured mid-tones"}[tone_info["tone_band"]]

    if angle == "revisit_motion_discovery":
        opening = f'Starting tightly on {focus_phrase} of {title_q} before stepping back to the full canvas.'
        body = f"Earlier we shared {artist_label}'s work ({date_txt or 'date unrecorded'}) as still slides; in motion you can see how that single area anchors the entire {orientation} balance."
        closing = "Did that opening crop match what you expected when the full work appeared?"
    elif angle == "exact_year_quiz":
        opening = f'{title_q} by {artist_label} — before swiping to the answer card, what decade would you place this in?'
        body = f"Held by {museum_short}, the {orientation} work leans into {palette_words} and {tone_word}."
        closing = "Drop your year guess before checking Slide 2."
    elif angle == "three_work_dialogue":
        a0, a1, a2 = cleaned[0], cleaned[1], cleaned[2]
        opening = f"Three rooms in one post: {a0['artist_surname']}'s \"{a0['title_clean']}\", {a1['artist_surname']}'s \"{a1['title_clean']}\", and {a2['artist_surname']}'s \"{a2['title_clean']}\"."
        body = f"Swipe through each work uncropped — moving from {palette_words} in the opening piece across three distinct museum rooms."
        closings = (
            f"If you could linger in front of only one of these three in a gallery, which room would hold you?",
            "Each work carries its own pace when seen at full height.",
            "Which of the three feels closest to the mood of today?",
        )
        closing = closings[seed % len(closings)]
    elif angle == "dialogue_pair":
        a0, a1 = cleaned[0], cleaned[1]
        opening = f'Putting "{a0["title_clean"]}" ({a0["artist_surname"]}) directly beside "{a1["title_clean"]}" ({a1["artist_surname"]}).'
        body = f"The first opens in {palette_words} with {tone_word}; swiping separates the two so each composition stands on its own terms."
        closings = (
            f"Does {a0['artist_surname']} or {a1['artist_surname']} pull your eye first?",
            "Seen side by side, their contrast in framing does the talking.",
            "Which half of this pairing would you keep on a quiet wall?",
        )
        closing = closings[seed % len(closings)]
    elif angle == "study_process":
        opening = f'{title_q} is not a finished salon picture — it is a working study by {artist_label} {date_part}.'.replace("  ", " ").strip()
        body = f"Preserved at {museum_short}, the {orientation} {support_sg} keeps the raw decisions visible in {palette_words} and {tone_word}."
        closing = "Preparatory studies often reveal more honesty than the finished painting ever does."
    elif angle == "series_context":
        opening = f'A single page from a wider world: {title_q} ({artist_label}, {date_txt or "date unrecorded"}).'
        body = f"From {museum_short}, the {orientation} composition packs narrative density around {focus_phrase} in {palette_words}."
        closing = f"Swipe to Slide 2 for a closer look at {focus_phrase} without text over the {support_sg}."
    elif angle == "composition_whole":
        openings = (
            f'A wide {orientation} span in {title_q} ({artist_label}{", " + date_txt if date_txt else ""}) — built to be read from edge to edge.',
            f'{title_q} by {artist_label} stretches across a broad horizontal field of {palette_words}.',
            f'In {museum_short}, {title_q} rewards stepping back so the full horizon holds together.',
        )
        opening = openings[seed % len(openings)]
        body = f"The visual weight gathers near {focus_phrase}, balanced against {tone_word} across the wider surface."
        closings = (
            f"Slide 2 zooms into {focus_phrase} so you can inspect the figure and brushwork up close.",
            f"Does the weight near {focus_phrase} anchor the scene for you, or does the open edge pull you away?",
            f"Swipe past the cover to see {focus_phrase} isolated, then the full uncropped {support_sg}.",
        )
        closing = closings[seed % len(closings)]
    else:  # visual_detail
        openings = (
            f'In {title_q} ({artist_label}{", " + date_txt if date_txt else ""}), the sharpest tension sits around {focus_phrase}.',
            f'{artist_label.capitalize()} anchors {title_q} with {tone_word} and {palette_words}.',
            f'From {museum_short}: {title_q} — a {orientation} study where {focus_phrase} carries the room.',
            f'Few details settle a frame like {focus_phrase} in {title_q} ({date_txt or museum_short}).',
        )
        opening = openings[seed % len(openings)]
        if slide_two_intent["spec"] == "detail_callout":
            body = f"Slide 2 isolates {focus_phrase} with a small locator box so you can see exactly where that passage sits on the {support_sg}."
        else:
            body = f"Held by {museum_short}, the {orientation} {support_sg} balances {tone_word} against {palette_words}."
        closings = (
            f"The full uncropped {support_sg} waits inside — no text sitting over the work.",
            f"Would you spend longer with {focus_phrase} or the wider setting around it?",
            "Some paintings ask for a question; this one mostly asks for a quiet minute.",
            f"In a museum room, would {title_q} stop you mid-step?",
        )
        closing = closings[seed % len(closings)]

    tags = build_targeted_hashtags(lead["artist_short"], lead.get("institution", ""), post_format)
    caption_en = f"{opening}\n\n{body}\n\n{closing}\n\n{tags}"

    # 3) First Comment: STRICTLY archival museum provenance (zero overlap with caption prose)
    fc_lines = ["Museum Provenance & Source Record:"]
    for a in cleaned:
        d_txt = a.get("date") or "Date unrecorded"
        med = f" · {a['medium']}" if a.get("medium") else ""
        ev_date = _extract_event_date_from_text(a.get("title", ""), a.get("description", ""), d_txt)
        date_detail = f"Created: {d_txt} · Event depicted: {ev_date}" if ev_date else d_txt
        fc_lines.append(f"• {a['artist']} — {a['title']} ({date_detail}){med}")
        fc_lines.append(f"  {a.get('institution', 'Public Collection')} · {a.get('rights_basis', 'Public Domain')} · {a.get('museum_url', '')}")
    first_comment_en = "\n".join(fc_lines)

    # 4) Alt-Text: Objective visual description for accessibility & Discover SEO (never copies caption)
    if len(cleaned) >= 2:
        alt_text_en = (
            f"Editorial multi-work fine art layout comparing '{cleaned[0]['title_clean']}' by {cleaned[0]['artist_short']} "
            f"and '{cleaned[1]['title_clean']}' by {cleaned[1]['artist_short']} ({museum_short}), "
            f"featuring {tone_word} and sampled digital tones of {palette_words}."
        )
    else:
        alt_text_en = (
            f"A {orientation} digitized artwork titled '{lead['title_clean']}' by {lead['artist_short']} "
            f"({date_txt or 'date unrecorded'}, {museum_short}), showing {tone_word} and sampled tones of {palette_words} "
            f"with visual focus near {focus_phrase}."
        )
    if len(alt_text_en) > 275:
        alt_text_en = alt_text_en[:274].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
    elif not alt_text_en.endswith("."):
        alt_text_en = alt_text_en + "."

    # 5) Reel 3-second mute-friendly subtitle (distinct from caption opening, <= 68 chars)
    if angle == "revisit_motion_discovery":
        reel_subtitle_en = f"Starting at {focus_phrase} — watch where it sits."
    elif angle == "study_process":
        reel_subtitle_en = f"A working study by {lead['artist_surname']} ({date_txt or museum_short})."
    elif lead["is_anonymous"]:
        reel_subtitle_en = f"Unattributed ({date_txt or museum_short}) · {focus_phrase}."
    else:
        reel_subtitle_en = f"{lead['artist_surname']} ({date_txt or museum_short}) · Focus: {focus_phrase}."
    reel_subtitle_en = reel_subtitle_en[:68]

    # 6) Slide progression rationale (explains why inner slides exist)
    slide_note_en = (
        f"Curatorial angle: {angle}. {basis} Inner slides reveal the uncropped museum plate and sampled digital color palette."
    )

    # 7) Artwork-specific Reel motion start -> end coordinates
    motion_start = (round(fx, 3), round(fy, 3))
    motion_end = (0.5, 0.5)

    return {
        "copy_version": COPY_VERSION,
        "curatorial_angle": angle,
        "curatorial_basis": basis,
        "focus_region": focus_phrase,
        "motion_start": motion_start,
        "motion_end": motion_end,
        "palette_hex": palette_hex,
        "use_color_card": use_color_card,
        "slide_two_intent": slide_two_intent,
        "cleaned_artworks": cleaned,
        "caption_en": caption_en,
        "first_comment_en": first_comment_en,
        "alt_text_en": alt_text_en,
        "reel_subtitle_en": reel_subtitle_en,
        "slide_note_en": slide_note_en,
    }
