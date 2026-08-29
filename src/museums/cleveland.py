import logging
import random
import requests
import re
from typing import List
from .base import AdapterHTTPError, MuseumAdapter
from src.models import NormalizedArtwork, normalize_image_dimensions
from src.region import infer_region, metadata_text

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

    def fetch_candidates(
        self,
        limit: int = 50,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        candidates = []
        try:
            random_source = rng or random
            skip = random_source.randint(0, 500)
            logger.debug("[Cleveland] Candidate pool skip=%s seeded=%s", skip, rng is not None)
            url = (
                f"https://openaccess-api.clevelandart.org/api/artworks/"
                f"?has_image=1&limit={limit}&skip={skip}&type=Painting"
            )
            if query:
                url += f"&q={query}"
            
            headers = {"User-Agent": "InstagramArtBot/1.0"}
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                if res.status_code in {403, 429}:
                    raise AdapterHTTPError(self.source_id, res.status_code)
                logger.warning(f"[Cleveland] API returned {res.status_code}")
                return candidates
                
            artworks = res.json().get("data", [])
            
            for item in artworks:
                images = item.get("images")
                if not images:
                    continue
                    
                image_url = None
                image_asset = None
                if isinstance(images, dict):
                    print_img = images.get("print", {})
                    web_img = images.get("web", {})
                    image_asset = print_img if print_img.get("url") else web_img
                    image_url = image_asset.get("url") if isinstance(image_asset, dict) else None
                elif isinstance(images, list) and len(images) > 0:
                    image_asset = images[0]
                    image_url = image_asset.get("url") if isinstance(image_asset, dict) else None
                    
                if not image_url or image_url.endswith(".tif"):
                    web_img = images.get("web", {}) if isinstance(images, dict) else {}
                    image_asset = web_img
                    image_url = web_img.get("url") if isinstance(web_img, dict) else None
                    
                if not image_url:
                    continue

                image_width, image_height = normalize_image_dimensions(
                    image_asset.get("width") if isinstance(image_asset, dict) else None,
                    image_asset.get("height") if isinstance(image_asset, dict) else None,
                )
                    
                title = item.get("title") or "Untitled"
                medium = item.get("technique") or item.get("type") or ""
                
                if not is_painting(title, medium):
                    continue
                    
                creators = item.get("creators", [])
                artist = "Unknown Artist"
                if creators and isinstance(creators, list):
                    desc = creators[0].get("description", "")
                    artist = desc.split("(")[0].strip() if "(" in desc else desc

                culture = metadata_text(item.get("culture"))
                geographic_origin = metadata_text(
                    [item.get("creation_place"), item.get("country_of_origin"), item.get("place_of_origin")]
                )
                artist_nationality = metadata_text(
                    creators[0].get("nationality") if creators and isinstance(creators, list) and isinstance(creators[0], dict) else None
                )
                department = metadata_text(item.get("department"))
                style_or_period = metadata_text(item.get("style"))
                
                license_status = item.get("share_license_status")
                is_public_domain = license_status == "CC0"
                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(item.get("id")),
                    title=title,
                    artist_name=artist,
                    creation_date=item.get("creation_date") or "Unknown Date",
                    medium=medium,
                    culture=culture,
                    geographic_origin=geographic_origin,
                    artist_nationality=artist_nationality,
                    region=infer_region(
                        culture=culture,
                        geography=geographic_origin,
                        artist_nationality=artist_nationality,
                        department=department,
                        style_or_period=style_or_period,
                    ),
                    department=department,
                    style_or_period=style_or_period,
                    museum_name="Cleveland Museum of Art",
                    image_url=image_url,
                    credit_line=item.get("creditline"),
                    artwork_url=item.get("url"),
                    license=license_status,
                    is_public_domain=is_public_domain,
                    rights_status=(
                        "CONFIRMED_OPEN_ACCESS"
                        if is_public_domain
                        else "KNOWN_RESTRICTED"
                        if license_status
                        else None
                    ),
                    rights_text=item.get("copyright"),
                    copyright_notice=item.get("copyright"),
                    image_width=image_width,
                    image_height=image_height,
                )
                candidates.append(artwork)
                
        except AdapterHTTPError:
            raise
        except Exception as e:
            logger.error(f"[Cleveland] Error fetching candidates: {e}")
            
        return candidates
