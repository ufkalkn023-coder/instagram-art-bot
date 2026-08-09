import logging
import random
import requests
import re
from typing import List
from .base import MuseumAdapter
from src.models import NormalizedArtwork

logger = logging.getLogger(__name__)

NON_PAINTING_KEYWORDS = {
    "vase", "sculpture", "armor", "armour", "fragment", "stucco", "ceramic", "porcelain",
    "coin", "medal", "glass", "furniture", "weapon", "sword", "dagger", "textile", "rug",
    "costume", "jewelry", "jewellery", "tapestry", "pottery", "bowl", "plate", "cup",
    "statue", "clock", "reliquary", "breastplate", "helm", "helmet", "jar", "jug", "pitcher"
}

def is_painting(title: str, medium: str = "") -> bool:
    combined_text = f"{title} {medium}".lower()
    for keyword in NON_PAINTING_KEYWORDS:
        if re.search(rf"\b{keyword}s?\b", combined_text):
            return False
    return True

class ClevelandAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "cleveland"

    def fetch_candidates(self, limit: int = 50) -> List[NormalizedArtwork]:
        candidates = []
        try:
            skip = random.randint(0, 500)
            url = (
                f"https://openaccess-api.clevelandart.org/api/artworks/"
                f"?has_image=1&limit={limit}&skip={skip}&type=Painting"
            )
            
            headers = {"User-Agent": "InstagramArtBot/1.0"}
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                logger.warning(f"[Cleveland] API returned {res.status_code}")
                return candidates
                
            artworks = res.json().get("data", [])
            
            for item in artworks:
                images = item.get("images")
                if not images:
                    continue
                    
                image_url = None
                if isinstance(images, dict):
                    print_img = images.get("print", {})
                    web_img = images.get("web", {})
                    image_url = print_img.get("url") or web_img.get("url")
                elif isinstance(images, list) and len(images) > 0:
                    image_url = images[0].get("url")
                    
                if not image_url or image_url.endswith(".tif"):
                    web_img = images.get("web", {}) if isinstance(images, dict) else {}
                    image_url = web_img.get("url")
                    
                if not image_url:
                    continue
                    
                title = item.get("title") or "Untitled"
                medium = item.get("technique") or item.get("type") or ""
                
                if not is_painting(title, medium):
                    continue
                    
                creators = item.get("creators", [])
                artist = "Unknown Artist"
                if creators and isinstance(creators, list):
                    desc = creators[0].get("description", "")
                    artist = desc.split("(")[0].strip() if "(" in desc else desc
                
                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(item.get("id")),
                    title=title,
                    artist_name=artist,
                    creation_date=item.get("creation_date") or "Unknown Date",
                    medium=medium,
                    museum_name="Cleveland Museum of Art",
                    image_url=image_url,
                    is_public_domain=True
                )
                candidates.append(artwork)
                
        except Exception as e:
            logger.error(f"[Cleveland] Error fetching candidates: {e}")
            
        return candidates
