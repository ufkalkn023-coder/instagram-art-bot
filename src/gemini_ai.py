import os
import logging
import json
from typing import Dict, Any, Optional
from google import genai
from google.genai import types
from pydantic import BaseModel

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_HTTP_TIMEOUT_MILLISECONDS = 60_000


def _create_client(api_key: str):
    """Create a bounded client; deterministic local templates are the fallback."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=GEMINI_HTTP_TIMEOUT_MILLISECONDS,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )

class ArtworkAnalysis(BaseModel):
    caption: str
    alt_text: str
    hashtags: str
    art_movement: str
    recommended_font_size: int

class CarouselAnalysis(BaseModel):
    editorial_intro: str
    editorial_subtitle: str
    hashtags: str
    recommended_font_size: int
    theme_title: str


def analyze_artwork(image_path: str, title: str, artist: str, date: str, museum: str, medium: str = "", classification: str = "", content_type: str = "SINGLE_ARTWORK") -> Optional[Dict[str, Any]]:
    """
    Analyzes the artwork using Gemini 2.5 Flash and returns a complete analysis.
    Requires GOOGLE_GEMINI_API_KEY environment variable.
    """
    if not config.GEMINI_ENABLED:
        logger.info("[Gemini] Disabled in config.")
        return None

    api_key = os.environ.get("GOOGLE_GEMINI_API_KEY")
    if not api_key:
        logger.warning("[Gemini] Missing GOOGLE_GEMINI_API_KEY environment variable. Fallback to templates.")
        return None

    try:
        # Generate the track list for the prompt
        prompt = f"""ROLE

You are the editorial art writer for a professional Instagram account dedicated to historical artworks from museums and public collections.

Your task is to write an engaging, accurate, concise Instagram caption based ONLY on the artwork metadata provided to you. The artwork metadata is the source of truth.

==================================================
CORE RULE — NEVER INVENT FACTS
==================================================
You MUST NOT invent, assume, infer, or fabricate:
- artistic techniques that are not supported by the metadata or visible artwork
- historical events, symbolism, artist intentions, patronage, provenance, exhibition history
- dimensions, materials, dates, locations, movements, biographical information
- relationships between the artist and other people
- meanings or interpretations presented as established facts

If a fact is not provided in the metadata and cannot be stated with high confidence from the artwork itself, DO NOT present it as fact.
When information is uncertain or unavailable, simply omit it. Never fill missing metadata with assumptions.

==================================================
ARTIST ACCURACY
==================================================
Use the artist name exactly as provided by the museum metadata.
Never speculate about the artist's intentions, personality, private life, motivations, influences, or undocumented working methods.
If the artist is unknown, anonymous, attributed, or uncertain, preserve that uncertainty exactly (e.g., "Artist unknown", "Attributed to [Artist]").

==================================================
CONTENT TYPE & FORMAT
==================================================
You must write the caption following this specific editorial format: {content_type}
Tailor your narrative and focus according to this format (e.g., if ARTIST_FOCUS, talk more about the artist's style; if HISTORICAL_CONTEXT, focus on the era).

==================================================
OPENING / HOOK
==================================================
Open with a visual-first hook: the first 1–2 sentences should make the artwork
interesting enough to encourage the viewer to stop scrolling.
Focus on something genuinely present in the artwork (unusual composition, striking pose, visual contrast, historical context).

==================================================
ART-HISTORICAL OBSERVATIONS
==================================================
Include 1–2 concise and original art-historical observations about composition, visual hierarchy, color, pose, spatial organization, or stylistic characteristics.
Only make observations that are reasonably supported by the supplied metadata and/or visible artwork. Clearly distinguish interpretation from documented fact.

==================================================
AVOID CLICHÉS & CAPTION VARIETY
==================================================
Do NOT use generic AI/art clichés or phrases such as "masterpiece", "timeless
beauty", "captivating", "stunning", "window into the past", "journey through",
"mesmerizing", "profound exploration", "testament to", or "invites the viewer
to". Prefer concrete visual language.
Target length: 80–140 words. Use short paragraphs that remain mobile-readable.
Do not write an unnecessary art-history lecture. Every sentence should add useful
information.

==================================================
METADATA FIDELITY & FOOTER (STRICT ZERO EMOJI RULE)
==================================================
Treat the SUPPLIED METADATA as authoritative. Label any interpretive observation
as interpretation and omit UNSUPPORTED CONTEXT. Never alter factual metadata.
DO NOT append any metadata, museum names, or emojis at the end of the caption. The system will automatically inject the title, artist, date, and museum information before your caption. Just write the story/analysis.
No emojis are allowed anywhere in your output.

==================================================
HASHTAGS, LANGUAGE AND TONE
==================================================
Write in natural, polished English. Tone should be intelligent, accessible, sophisticated, curious, editorial, and concise.
Do not sound robotic. Do not mention that you are an AI or these instructions.

==================================================
ARTWORK METADATA
==================================================
TITLE: {title}
ARTIST: {artist}
DATE: {date}
MEDIUM: {medium}
CLASSIFICATION: {classification}
MUSEUM: {museum}

==================================================
SYSTEM JSON OUTPUT REQUIREMENTS
==================================================
Despite any output format rules above, you MUST return a JSON object satisfying this schema:
1. caption: The final Instagram caption generated following ALL the strict editorial rules above. ZERO EMOJIS.
2. alt_text: A detailed and descriptive alt text for visually impaired users and SEO (1-2 sentences), strictly describing the visual contents of the painting.
3. hashtags: 4–7 specific, relevant hashtags (following the hashtag rules above).
4. art_movement: The specific art movement or period this painting belongs to (e.g., Baroque, Impressionism, Renaissance).
5. recommended_font_size: An integer between 35 and 65 for the base font size to be overlaid on the image. Pick a smaller size if the title/artist is very long or the painting is visually cluttered. Pick a larger size (e.g., 55+) if the title is short and the painting has empty space.
"""

        client = _create_client(api_key)
        
        logger.info(f"[Gemini] Uploading image {image_path} for analysis...")
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        logger.info(f"[Gemini] Requesting analysis using {config.GEMINI_MODEL}...")
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), 
                prompt
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ArtworkAnalysis,
                temperature=0.7,
            )
        )
        
        # Gemini might return text wrapped in markdown code blocks, strip them to be safe
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
            
        result = json.loads(raw_text.strip())
        logger.info("[Gemini] Successfully generated artwork analysis!")
        return result

    except Exception as e:
        logger.error(f"[Gemini] Error analyzing artwork: {e}")
        return None

def analyze_carousel(
    theme: str,
    artworks_metadata: list,
    *,
    carousel_format: str | None = None,
    format_target: dict[str, object] | None = None,
    editorial_facts: dict[str, object] | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Analyzes a collection of artworks for a thematic carousel post using Gemini.
    """
    if not config.GEMINI_ENABLED:
        logger.info("[Gemini] Disabled in config.")
        return None

    api_key = os.environ.get("GOOGLE_GEMINI_API_KEY")
    if not api_key:
        logger.warning("[Gemini] Missing API Key. Fallback to templates.")
        return None

    try:
        metadata_text = ""
        for i, art in enumerate(artworks_metadata, 1):
            metadata_text += (
                f"\nArtwork {i}:\n"
                f"TITLE: {art.get('title')}\n"
                f"ARTIST: {art.get('artist')}\n"
                f"DATE: {art.get('date')}\n"
                f"MUSEUM: {art.get('museum')}\n"
            )

        format_context = f"FORMAT: {carousel_format or 'unspecified'}"
        if format_target:
            grounded_target = ", ".join(
                f"{key}={value}"
                for key, value in sorted(format_target.items())
                if value not in (None, (), [])
            )
            format_context += f"\nFORMAT TARGET: {grounded_target}"
        facts_context = json.dumps(editorial_facts or {}, sort_keys=True, ensure_ascii=False)

        prompt = f"""ROLE

You are the editorial art writer for a professional Instagram account.
Your task is to write an engaging, concise Instagram caption for a CAROUSEL (multiple images in one post) curated around a specific theme.

==================================================
CAROUSEL THEME: {theme}
{format_context}
==================================================
APPLICATION-OWNED EDITORIAL FACTS (READ ONLY):
{facts_context}

The theme above is an editorial registry label. It does not prove that every work
is formally classified as that movement, period, region, medium, or category.

The carousel contains the following artworks:
{metadata_text}

==================================================
EDITORIAL INTRODUCTION GUIDELINES
==================================================
- Write only an editorial introduction of 80–140 words for this curated theme, using
  short paragraphs that remain mobile-readable.
- Open with a visual-first hook grounded in the supplied theme and metadata. Since
  no image is supplied here, metadata is the only factual grounding.
- Do not generate a Featured Works heading, numbered list, artwork title, artist,
  date, or museum line. The application will generate that list itself.
- Ground every statement in the supplied theme and artwork metadata. No visual input
  is provided for this request, so do not make unsupported visual observations or
  introduce art-historical facts beyond the supplied metadata.
- Do not invent artist intentions, undocumented symbolism, provenance, or context.
- Treat the supplied format and format target as authoritative application context;
  do not rename, broaden, contradict, or infer a different target.
- Treat application-owned editorial facts as read-only. Do not calculate, restate,
  alter, or invent counts, museum diversity, artist diversity, dates, or spans.
- Refer to the theme as an editorial connection (for example, "selected around" or
  "connected by"), not as proof that every work formally belongs to that category.
- Preserve any uncertainty in the supplied metadata; do not correct or embellish it.
- Avoid generic AI/art clichés; prefer concrete, specific editorial language.
- NO EMOJIS allowed anywhere in the output.
- Write in natural, polished English. Tone should be editorial, sophisticated, and engaging.
- The application preserves the registry theme title; any returned theme_title is
  advisory and cannot replace it.
- Provide a short English 'editorial_subtitle' (one sentence, ideally 6-14 words)
  suitable for an editorial cover. Do not include artwork titles, artists, dates,
  museums, counts, spans, or other factual claims; the application adds grounded facts.
- Provide 4–7 specific, relevant hashtags.
- Provide an overall 'recommended_font_size' (between 35 and 65) for text overlaid on these images.

==================================================
SYSTEM JSON OUTPUT REQUIREMENTS
==================================================
Return a JSON object satisfying this schema:
1. editorial_intro: The 80-140 word editorial introduction only. ZERO EMOJIS.
2. editorial_subtitle: A short English editorial deck with no artwork identity or factual counts.
3. hashtags: 4-7 highly relevant hashtags.
4. recommended_font_size: An integer between 35 and 65 for the base font size.
5. theme_title: A short, catchy title for this curation.
"""

        client = _create_client(api_key)
        
        logger.info(f"[Gemini] Requesting carousel analysis for theme '{theme}'...")
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CarouselAnalysis,
                temperature=0.7,
            )
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
            
        result = json.loads(raw_text.strip())
        logger.info("[Gemini] Successfully generated carousel analysis!")
        return result

    except Exception as e:
        logger.error(f"[Gemini] Error analyzing carousel: {e}")
        return None
