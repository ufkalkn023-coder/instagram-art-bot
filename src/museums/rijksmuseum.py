import logging
import random
import requests
import os
from typing import List
from .base import MuseumAdapter
from src.models import NormalizedArtwork
from src.source_health import classify_exception, classify_http_failure
import config

logger = logging.getLogger(__name__)

RIJKSMUSEUM_ALLOWED_RIGHTS = {
    "public domain": "CONFIRMED_PUBLIC_DOMAIN",
    "cc0": "CONFIRMED_OPEN_ACCESS",
    "cc0 1.0": "CONFIRMED_OPEN_ACCESS",
    "creative commons zero": "CONFIRMED_OPEN_ACCESS",
}


def get_rights_status(copyright_holder: object) -> str | None:
    """Accept only explicit public-domain or CC0 statements from the API."""
    if not isinstance(copyright_holder, str):
        return None
    return RIJKSMUSEUM_ALLOWED_RIGHTS.get(copyright_holder.strip().casefold())

class RijksmuseumAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "rijksmuseum"

    def fetch_candidates(
        self,
        limit: int = 50,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        candidates = []
        self._clear_source_failure()
        api_key = os.environ.get("RIJKSMUSEUM_API_KEY", getattr(config, "RIJKSMUSEUM_API_KEY", ""))
        if not api_key:
            logger.info("[Rijksmuseum] No API key configured, skipping.")
            self._record_source_failure("MISSING_CREDENTIAL")
            return candidates

        try:
            random_source = rng or random
            page = random_source.randint(0, 100)
            logger.debug("[Rijksmuseum] Candidate pool page=%s seeded=%s", page, rng is not None)
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
                self._record_source_failure(classify_http_failure(res.status_code, res.headers))
                return candidates

            try:
                payload = res.json()
            except ValueError:
                logger.warning("[Rijksmuseum] API returned malformed JSON.")
                self._record_source_failure("INVALID_RESPONSE")
                return candidates
            artworks = payload.get("artObjects") if isinstance(payload, dict) else None
            if not isinstance(artworks, list):
                logger.warning("[Rijksmuseum] API returned an unexpected JSON payload.")
                self._record_source_failure("INVALID_RESPONSE")
                return candidates
            
            for item in artworks:
                rights_text = item.get("copyrightHolder")
                rights_status = get_rights_status(rights_text)
                if rights_status is None:
                    logger.info(f"[Rijksmuseum] Rejected {item.get('objectNumber')}: rights not confirmed.")
                    continue

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
                    license=rights_text,
                    is_public_domain=True,
                    rights_status=rights_status,
                    rights_text=rights_text,
                )
                candidates.append(artwork)

        except Exception as e:
            logger.error("[Rijksmuseum] Error fetching candidates (%s).", type(e).__name__)
            self._record_source_failure(classify_exception(e))

        return candidates
