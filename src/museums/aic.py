import logging
import random
import requests
from typing import List
from .base import MuseumAdapter
from src.models import NormalizedArtwork

logger = logging.getLogger(__name__)

class AICAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "aic"

    def fetch_candidates(self, limit: int = 20, query: str = None) -> List[NormalizedArtwork]:
        candidates = []
        try:
            page = random.randint(1, 20)
            search_query = f"painting {query}" if query else "painting"
            url = (
                f"https://api.artic.edu/api/v1/artworks/search"
                f"?q={search_query}&query[term][is_public_domain]=true"
                f"&fields=id,title,artist_title,date_display,medium_display,image_id,classification_title"
                f"&limit={limit}&page={page}"
            )
            
            headers = {"User-Agent": "InstagramArtBot/1.0"}
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                logger.warning(f"[AIC] API returned {res.status_code}")
                return candidates
                
            artworks = res.json().get("data", [])
            
            for item in artworks:
                image_id = item.get("image_id")
                if not image_id:
                    continue
                    
                cls = item.get("classification_title", "")
                if not cls or "painting" not in cls.lower():
                    continue
                    
                image_url = f"https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg"
                
                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(item.get("id")),
                    title=item.get("title") or "Untitled",
                    artist_name=item.get("artist_title") or "Unknown Artist",
                    creation_date=item.get("date_display") or "Unknown Date",
                    medium=item.get("medium_display") or "",
                    classification=item.get("classification_title"),
                    museum_name="Art Institute of Chicago",
                    image_url=image_url,
                    is_public_domain=True
                )
                candidates.append(artwork)
                
        except Exception as e:
            logger.error(f"[AIC] Error fetching candidates: {e}")
            
        return candidates
