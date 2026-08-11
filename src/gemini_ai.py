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

class ArtworkAnalysis(BaseModel):
    caption: str
    alt_text: str
    hashtags: str
    art_movement: str


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
The first 1–2 sentences should make the artwork interesting enough to encourage the viewer to stop scrolling.
Focus on something genuinely present in the artwork (unusual composition, striking pose, visual contrast, historical context).

==================================================
ART-HISTORICAL OBSERVATIONS
==================================================
Include 1–2 concise and original art-historical observations about composition, visual hierarchy, color, pose, spatial organization, or stylistic characteristics.
Only make observations that are reasonably supported by the supplied metadata and/or visible artwork. Clearly distinguish interpretation from documented fact.

==================================================
AVOID CLICHÉS & CAPTION VARIETY
==================================================
Do NOT use generic art-writing phrases such as "masterpiece", "timeless beauty", "captivating", "stunning", "window into the past", "journey through", "mesmerizing", "profound exploration", "testament to", or "invites the viewer to". Prefer concrete visual language.
Target length: 50–130 words. Do not write an unnecessary art-history lecture. Every sentence should add useful information.

==================================================
METADATA FIDELITY & FOOTER (STRICT ZERO EMOJI RULE)
==================================================
Treat the supplied metadata as authoritative. Never alter factual metadata.
DO NOT append any metadata, museum names, or emojis at the end of the caption. The system will automatically inject the title, artist, date, and museum information before your caption. Just write the story/analysis.
ABSOLUTELY ZERO EMOJIS ARE ALLOWED ANYWHERE IN YOUR OUTPUT.

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
3. hashtags: 3-5 highly relevant, SEO-optimized hashtags (following the hashtag rules above).
4. art_movement: The specific art movement or period this painting belongs to (e.g., Baroque, Impressionism, Renaissance).
"""

        client = genai.Client(api_key=api_key)
        
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
