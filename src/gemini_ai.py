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
    suggested_track_index: int


def analyze_artwork(image_path: str, title: str, artist: str, date: str, museum: str) -> Optional[Dict[str, Any]]:
    """
    Analyzes the artwork using Gemini 3.5 Flash and returns a complete analysis.
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
        track_list_str = "\n".join(
            f"Index {i}: {track['title']} by {track['artist']}" 
            for i, track in enumerate(config.PUBLIC_AUDIO_TRACKS)
        )

        prompt = f"""You are an expert art historian and Instagram social media manager.
Analyze the provided artwork image and its metadata.

Metadata:
- Title: {title}
- Artist: {artist}
- Date: {date}
- Museum: {museum}

Please provide a JSON response with the following fields:
1. caption: An educational, academic, yet engaging Instagram caption (4-6 sentences) explaining the artwork's history, technique, emotional atmosphere, and meaning. The language MUST be English.
2. alt_text: A detailed and descriptive alt text for visually impaired users and SEO (1-2 sentences), strictly describing the visual contents of the painting.
3. hashtags: 5-8 highly relevant, SEO-optimized hashtags including the art movement, artist, and subject matter (e.g., #Impressionism #VanGogh).
4. art_movement: The specific art movement or period this painting belongs to (e.g., Baroque, Impressionism, Renaissance).
5. suggested_track_index: Choose the single most atmospherically fitting classical music track for a Reels video of this painting from the following list. Return ONLY the integer index (e.g., 0, 1, 2).

Audio Tracks to choose from:
{track_list_str}
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
