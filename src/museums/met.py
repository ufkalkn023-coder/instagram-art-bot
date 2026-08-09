import logging
import random
import requests
import re
from typing import List
from .base import MuseumAdapter
from src.models import NormalizedArtwork
import config

logger = logging.getLogger(__name__)

MET_SEARCH_TERMS = [
    "painting", "oil painting", "impressionism painting", "renaissance painting",
    "baroque painting", "portrait painting", "landscape painting", "still life painting"
]

NON_PAINTING_KEYWORDS = {
    "vase", "sculpture", "armor", "armour", "fragment", "stucco", "ceramic", "porcelain",
    "coin", "medal", "glass", "furniture", "weapon", "sword", "dagger", "textile", "rug",
    "statue", "clock", "reliquary", "breastplate", "helm", "helmet", "jar", "jug", "pitcher"
}

def is_painting(title: str, object_name: str = "", classification: str = "", medium: str = "") -> bool:
    combined_text = f"{title} {object_name} {classification} {medium}".lower()
    for keyword in NON_PAINTING_KEYWORDS:
        if re.search(rf"\b{keyword}s?\b", combined_text):
            return False
    return True

class MetAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "met"

    def fetch_candidates(self, limit: int = 20) -> List[NormalizedArtwork]:
        candidates = []
        try:
            search_term = random.choice(MET_SEARCH_TERMS)
            search_url = (
                f"{config.MET_API_BASE}/search"
                f"?hasImages=true&isPublicDomain=true&medium=Paintings&q={search_term}"
            )
            
            headers = {"User-Agent": "InstagramArtBot/1.0"}
            res = requests.get(search_url, headers=headers, timeout=20)
            if res.status_code != 200:
                logger.warning(f"[Met] API returned {res.status_code}")
                return candidates

            object_ids = res.json().get("objectIDs", [])
            if not object_ids:
                return candidates

            sample_ids = random.sample(object_ids, min(limit, len(object_ids)))

            for obj_id in sample_ids:
                detail_url = f"{config.MET_API_BASE}/objects/{obj_id}"
                d_res = requests.get(detail_url, headers=headers, timeout=15)
                if d_res.status_code != 200:
                    continue

                detail = d_res.json()
                if not detail.get("isPublicDomain"):
                    continue

                title = detail.get("title") or "Untitled"
                object_name = detail.get("objectName") or ""
                classification = detail.get("classification") or ""
                medium = detail.get("medium") or ""

                if not is_painting(title, object_name, classification, medium):
                    continue

                image_url = detail.get("primaryImage") or detail.get("primaryImageSmall")
                if not image_url:
                    continue

                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(obj_id),
                    title=title,
                    artist_name=detail.get("artistDisplayName") or "Unknown Artist",
                    creation_date=detail.get("objectDate") or "Unknown Date",
                    medium=medium,
                    department=detail.get("department"),
                    classification=classification,
                    museum_name="The Metropolitan Museum of Art",
                    image_url=image_url,
                    is_public_domain=True
                )
                candidates.append(artwork)

        except Exception as e:
            logger.error(f"[Met] Error fetching candidates: {e}")

        return candidates
