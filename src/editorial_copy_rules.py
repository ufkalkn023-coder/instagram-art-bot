"""Editorial English copy engine: hooks, CTA questions, hashtags, anti-AI-cliché linter & first-comment card."""

from __future__ import annotations

import re
from typing import Sequence

BANNED_AI_CLICHES: tuple[str, ...] = (
    "delve",
    "tapestry",
    "testament",
    "captivating",
    "masterfully",
    "ethereal",
    "symphony of",
    "evokes a sense of",
    "invites the viewer",
    "timeless masterpiece",
    "breathtaking",
)

SAVE_CTA_VARIATIONS: tuple[str, ...] = (
    "Save this palette for your next project.",
    "Bookmark this work for your next museum visit.",
    "Save the final pigment card to revisit these tones.",
    "Keep this study in your saved collection.",
)

SINGLE_WORK_QUESTIONS: tuple[str, ...] = (
    "Which detail caught your eye first?",
    "Where does your gaze settle first — light or shadow?",
    "Would you pause longest on the foreground or the horizon?",
    "Which color holds this room together for you?",
)


def lint_english_caption(caption: str) -> dict:
    """Validate caption against anti-AI clichés, first-line hook length, CTA question, and hashtag budget."""
    text = str(caption or "").strip()
    low = text.lower()
    found_cliches = [phrase for phrase in BANNED_AI_CLICHES if phrase in low]

    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    hook_words = len(first_line.split())
    has_question = "?" in text
    hashtags = re.findall(r"#[A-Za-z0-9_]+", text)

    warnings = []
    if found_cliches:
        warnings.append(f"ai_cliches:{','.join(found_cliches)}")
    if hook_words > 16:
        warnings.append(f"long_first_line:{hook_words}_words")
    if not has_question:
        warnings.append("missing_cta_question")
    if not (3 <= len(hashtags) <= 6):
        warnings.append(f"hashtag_count:{len(hashtags)}")

    return {
        "passed": len(warnings) == 0,
        "warnings": warnings,
        "found_cliches": found_cliches,
        "hook_word_count": hook_words,
        "has_question": has_question,
        "hashtag_count": len(hashtags),
    }


def build_targeted_hashtags(artist: str, institution: str, post_format: str) -> str:
    """Build 5 clean targeted English hashtags (artist + museum/art + format + brand)."""
    artist_slug = re.sub(r"[^a-z0-9]", "", artist.split()[-1].lower()) if artist else "fineart"
    museum_low = (institution or "").lower()
    if "metropolitan" in museum_low:
        museum_tag = "#themet"
    elif "rijksmuseum" in museum_low:
        museum_tag = "#rijksmuseum"
    elif "chicago" in museum_low:
        museum_tag = "#artinstituteofchicago"
    elif "cleveland" in museum_low:
        museum_tag = "#clevelandmuseumofart"
    else:
        museum_tag = "#museumart"
    fmt_tag = "#artreels" if post_format == "reel" else "#arthistory"
    tags = [f"#{artist_slug}", fmt_tag, museum_tag, "#artdetails", "#artfolio"]
    # Deduplicate while preserving order
    unique = list(dict.fromkeys(tags))
    return " ".join(unique[:5])


def compose_curated_caption(
    *,
    post_format: str,
    artworks: Sequence[dict],
    slot_index: int,
    is_quiz: bool = False,
) -> str:
    """Compose a concise, cliché-free English caption with <=12-word hook, save CTA, question, and 5 hashtags."""
    a1 = artworks[0]
    save_line = SAVE_CTA_VARIATIONS[slot_index % len(SAVE_CTA_VARIATIONS)]
    tags = build_targeted_hashtags(a1["artist"], a1.get("institution", ""), post_format)

    if len(artworks) >= 3:
        a2, a3 = artworks[1], artworks[2]
        hook = "Three galleries, three light studies — pick your room."
        body = (
            f"1. {a1['artist']} — {a1['title']}\n"
            f"2. {a2['artist']} — {a2['title']}\n"
            f"3. {a3['artist']} — {a3['title']}\n\n"
            f"Room 1, 2, or 3 — which one do you stay in longest?\n{save_line}"
        )
    elif len(artworks) == 2:
        a2 = artworks[1]
        hook = f"{a1['artist'].split()[-1]} vs. {a2['artist'].split()[-1]} — two ways to hold a frame."
        body = (
            f"Left: {a1['artist']}, {a1['title']} ({a1.get('date') or 'Archive'}).\n"
            f"Right: {a2['artist']}, {a2['title']} ({a2.get('date') or 'Archive'}).\n\n"
            f"Left or Right — which composition pulls you in first?\n{save_line}"
        )
    elif is_quiz:
        hook = "Before you swipe: can you guess the year?"
        body = (
            f"{a1['title']} by {a1['artist']}.\n"
            f"Drop A, B, C, or D in the comments, then swipe to Slide 2 for the answer.\n{save_line}"
        )
    elif post_format == "reel":
        hook = f"Watch how {a1['artist'].split()[-1]} builds light from shadow."
        question = SINGLE_WORK_QUESTIONS[slot_index % len(SINGLE_WORK_QUESTIONS)]
        body = (
            f"{a1['title']} ({a1.get('date') or 'Archive'}) · {a1.get('institution') or 'Public Domain Collection'}.\n"
            f"{question}"
        )
    else:
        hook = f"Pause on the brushwork in {a1['artist'].split()[-1]}."
        question = SINGLE_WORK_QUESTIONS[slot_index % len(SINGLE_WORK_QUESTIONS)]
        body = (
            f"{a1['title']} ({a1.get('date') or 'Archive'}) — swipe for the close-up detail and real pigment card.\n"
            f"{question} {save_line}"
        )

    return f"{hook}\n\n{body}\n\n{tags}"


def compose_first_comment(artworks: Sequence[dict]) -> str:
    """Build a clean technical museum provenance card for the First Comment."""
    lines = ["Archive & Museum Provenance:"]
    for idx, art in enumerate(artworks, 1):
        prefix = f"{idx}. " if len(artworks) > 1 else "• "
        dims = f" · {art['dimensions']}" if art.get("dimensions") else ""
        lines.append(
            f"{prefix}{art['artist']} — {art['title']} ({art.get('date') or 'n.d.'}){dims}"
        )
        inst = art.get("institution") or "Public Domain Collection"
        url = art.get("museum_url") or ""
        lines.append(f"   Collection: {inst}" + (f" · {url}" if url else ""))
    lines.append("Public domain / open-access museum record.")
    return "\n".join(lines)


def _describe_palette_words(palette_hex: Sequence[str] = ()) -> str:
    """Turn dominant hex colors into objective, human-readable art-historical color words for Alt-Text."""
    if not palette_hex:
        return "rich archival tones"
    words: list[str] = []
    for hx in palette_hex[:3]:
        s = hx.lstrip("#")
        if len(s) != 6:
            continue
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if luma < 48:
            word = "deep shadow black"
        elif luma > 210:
            word = "warm ivory"
        elif r > g + 35 and r > b + 35:
            word = "ochre crimson" if g < 110 else "warm ochre gold"
        elif b > r + 25 and b > g + 10:
            word = "slate blue"
        elif g > r + 15 and g > b + 15:
            word = "muted olive green"
        else:
            word = "muted earth umber"
        if word not in words:
            words.append(word)
    if not words:
        return "rich archival tones"
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + f", and {words[-1]}"


def compose_seo_alt_text(
    artworks: Sequence[dict],
    *,
    post_format: str = "carousel",
    tone_band: str = "mid",
    palette_hex: Sequence[str] = (),
) -> str:
    """Generate an objective, 1-sentence English Alt-Text for accessibility & Instagram Discover SEO."""
    if not artworks:
        return "Curated museum artwork from an open-access fine art collection."
    palette_desc = _describe_palette_words(palette_hex)
    lead = artworks[0]
    title = (lead.get("title") or "Untitled work").strip()
    artist = (lead.get("artist") or "Unknown artist").strip()
    date_txt = (lead.get("date") or "").strip()
    museum = (lead.get("institution") or "a public museum collection").strip()
    date_prefix = f"{date_txt} " if date_txt else ""
    tone_adj = {"dark": "dramatic chiaroscuro", "light": "luminous", "mid": "balanced"}.get(tone_band, "balanced")

    if len(artworks) >= 2:
        second = artworks[1]
        t2 = (second.get("title") or "companion work").strip()
        a2 = (second.get("artist") or artist).strip()
        text = (
            f"Side-by-side fine art comparison of {date_prefix}'{title}' by {artist} "
            f"and '{t2}' by {a2} from {museum}, featuring {tone_adj} {palette_desc}."
        )
    elif post_format == "reel":
        text = (
            f"Vertical detail-to-full motion study of the {date_prefix}artwork '{title}' by {artist} "
            f"in {museum}, highlighting {tone_adj} {palette_desc}."
        )
    else:
        text = (
            f"Curated gallery slide of the {date_prefix}artwork '{title}' by {artist} "
            f"from {museum}, rendered in {tone_adj} {palette_desc}."
        )
    return text[:260]

