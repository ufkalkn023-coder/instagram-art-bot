import logging
import random
import requests
import os
from typing import List
from .base import MuseumAdapter
from src.models import NormalizedArtwork
import config

logger = logging.getLogger(__name__)

class RijksmuseumAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "rijksmuseum"

    def fetch_candidates(self, limit: int = 50, query: str = None) -> List[NormalizedArtwork]:
        candidates = []
        api_key = os.environ.get("RIJKSMUSEUM_API_KEY", getattr(config, "RIJKSMUSEUM_API_KEY", ""))
        if not api_key:
            logger.info("[Rijksmuseum] No API key configured, skipping.")
            return candidates

        try:
            page = random.randint(0, 100)
            url = (
                f"https://www.rijksmuseum.nl/api/en/collection"
                f"?key={api_key}&hasImage=true&type=painting&ps={limit}&p={page}&imgonly=true"
            )
            if query:
                url += f"&q={query}"
            
            headers = {"User-Agent": "InstagramArtBot/1.0"}
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code != 200:
                logger.warning(f"[Rijksmuseum] API returned {res.status_code}")
                return candidates

            artworks = res.json().get("artObjects", [])
            
            for item in artworks:
                obj_number = item.get("objectNumber")
                if not obj_number:
                    continue

                web_image = item.get("webImage", {})
                image_url = web_image.get("url")
                if not image_url:
                    continue

                title = item.get("title") or "Untitled"
                artist = item.get("principalOrFirstMaker") or "Unknown Artist"
                date = item.get("longTitle", "").split(",")[-1].strip() if item.get("longTitle") else "Unknown Date"

                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(obj_number),
                    title=title,
                    artist_name=artist,
                    creation_date=date,
                    medium="painting",
                    museum_name="Rijksmuseum, Amsterdam",
                    image_url=image_url,
                    is_public_domain=True
                )
                candidates.append(artwork)

        except Exception as e:
            logger.error(f"[Rijksmuseum] Error fetching candidates: {e}")

        return candidates
