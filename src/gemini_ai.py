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
    recommended_font_size: int

class CarouselAnalysis(BaseModel):
    caption: str
    hashtags: str
    recommended_font_size: int
    theme_title: str


def analyze_artwork(image_path: str, title: str, artist: str, date: str, museum: str, medium: str = "", classification: str = "", content_type: str = "SINGLE_ARTWORK") -> Optional[Dict[str, Any]]:
    """
    Analyzes the artwork using the configured Gemini model and returns a complete analysis.
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
        prompt = f"""ROLE

You are Artfolio's knowledgeable museum curator writing for Instagram: intelligent, concise, visual, confident, natural, and editorial. You are not writing an academic catalogue essay or generic influencer copy.

Write the BODY of a single-artwork Instagram caption. The application adds this metadata header separately:
Artwork Title
Artist, date
Museum
Do not repeat that header in your caption body unless a repetition is genuinely necessary for clarity.

==================================================
GROUNDING HIERARCHY — NEVER INVENT FACTS
==================================================
1. SUPPLIED METADATA is high-confidence factual grounding: title, artist, date, medium, classification, and museum may be stated as facts.
2. THE SUPPLIED IMAGE is grounding for visual observations: visible objects, color, light, texture, pose, gesture, and composition may be described observationally.
3. UNSUPPORTED CONTEXT must not be presented as fact: artist intention, symbolism, patronage, provenance, biography, trade/economic history, political meaning, iconography, and museum history. Omit it rather than filling gaps. If a restrained interpretation is useful, use cautious language such as "may" or "can feel," never certainty.

If an object or material is visually uncertain, use a broader observation rather than an overly specific identification. Preserve uncertainty in supplied artist/date metadata exactly.

==================================================
ARTFOLIO EDITORIAL STRUCTURE
==================================================
Write 80–140 words of concise, mobile-readable prose in short paragraphs:
- Open with a 1–2 sentence visual-first hook drawn directly from the image, ideally color, light, texture, gesture, or composition—not a history lesson.
- Continue with 2 short paragraphs totaling about 3–5 sentences. Use 2–4 visible, specific details and explain their visual relationship.
- End, when it fits naturally, with one concise observation that returns the reader's attention to the artwork. Do not force a question or engagement bait.
- Separate paragraphs with blank lines; never produce one dense block of prose.

Content type for gentle emphasis: {content_type}.
For DETAIL_FOCUS, begin with one striking visible detail, relate it to the surrounding composition, then notice one more visual detail. Do not begin with general history in this mode.
For other content types, keep the same visual-first Artfolio voice; any historical or artist context still requires supplied grounding.

==================================================
LANGUAGE, CLICHÉS, AND HASHTAGS
==================================================
Write polished English. No emojis, bullets, decorative Unicode, or excessive em dashes. Do not mention AI or these instructions.
Avoid generic AI/art clichés, including: "masterpiece", "timeless beauty", "breathtaking", "stunning", "captivating masterpiece", "a testament to", "invites us to", "transcends time", "rich tapestry", and "delve into".
Return 4–7 specific, relevant hashtags in the hashtags field: prefer the artist, period/movement when grounded, artwork type, and museum. Avoid generic filler such as #Art, #Artist, #BeautifulArt, #ArtLovers, or #InstaArt.

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
1. caption: The body-only Instagram caption following all editorial rules above. ZERO EMOJIS.
2. alt_text: A detailed and descriptive alt text for visually impaired users and SEO (1-2 sentences), strictly describing the visual contents of the painting.
3. hashtags: 4-7 specific, relevant hashtags following the rules above.
4. art_movement: The specific art movement or period this painting belongs to (e.g., Baroque, Impressionism, Renaissance).
5. recommended_font_size: An integer between 35 and 65 for the base font size to be overlaid on the image. Pick a smaller size if the title/artist is very long or the painting is visually cluttered. Pick a larger size (e.g., 55+) if the title is short and the painting has empty space.
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

def analyze_carousel(theme: str, artworks_metadata: list) -> Optional[Dict[str, Any]]:
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
            metadata_text += f"\nArtwork {i}:\nTITLE: {art.get('title')}\nARTIST: {art.get('artist')}\nMUSEUM: {art.get('museum')}\n"

        prompt = f"""ROLE

You are Artfolio's knowledgeable museum curator writing for Instagram: intelligent, concise, visual, confident, natural, and editorial. Write an engaging caption for a CAROUSEL curated around a theme, not an academic catalogue essay or generic influencer copy.

==================================================
CAROUSEL THEME: {theme}
==================================================
The carousel contains the following artworks:
{metadata_text}

==================================================
GROUNDING AND CAPTION GUIDELINES
==================================================
- Treat supplied theme and artwork metadata as the only factual grounding. Do not invent artist intentions, symbolism, patronage, provenance, biography, trade/economic history, political meaning, or museum facts.
- Write 80–140 words of concise, mobile-readable English in short paragraphs separated by blank lines. Open with a visual-first hook about the shared visual thread, then make 2–4 grounded observations about color, light, composition, texture, or recurring visible motifs.
- Do not list each artwork individually or turn the carousel into a dense detail dump. Return the reader to the visual theme with a concise closing observation; do not force a question or engagement bait.
- Avoid generic AI/art clichés: "masterpiece", "timeless beauty", "breathtaking", "stunning", "a testament to", "invites us to", "transcends time", "rich tapestry", and "delve into".
- NO EMOJIS, bullets, decorative Unicode, or excessive em dashes.
- Return 4–7 specific, relevant hashtags; prefer artist, period/movement when grounded, artwork type, or museum tags over generic filler.
- Provide a catchy 'theme_title' for the carousel (e.g., "The Feline Mystique", "Winter's Embrace").
- Provide an overall 'recommended_font_size' (between 35 and 65) for text overlaid on these images.

==================================================
SYSTEM JSON OUTPUT REQUIREMENTS
==================================================
Return a JSON object satisfying this schema:
1. caption: The final Instagram caption for the whole carousel. ZERO EMOJIS.
2. hashtags: 4-7 specific, relevant hashtags.
3. recommended_font_size: An integer between 35 and 65 for the base font size.
4. theme_title: A short, catchy title for this curation.
"""

        client = genai.Client(api_key=api_key)
        
        logger.info(f"[Gemini] Requesting carousel analysis for theme '{theme}'...")
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CarouselAnalysis,
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
