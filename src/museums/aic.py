import logging
import random
import requests
from typing import List
from .base import AdapterHTTPError, MuseumAdapter
from src.aic_image_policy import aic_request_headers
from src.models import NormalizedArtwork
from src.region import infer_region, metadata_text
from src.source_health import classify_exception, classify_http_failure

logger = logging.getLogger(__name__)

# AIC documents this larger IIIF derivative for public-domain images. The
# adapter constructs it only after the API's explicit public-domain check.
AIC_PUBLIC_DOMAIN_IIIF_WIDTH = 1686


class AICAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "aic"

    def fetch_candidates(
        self,
        limit: int = 20,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        candidates = []
        self._clear_source_failure()
        try:
            random_source = rng or random
            page = random_source.randint(1, 20)
            logger.debug("[AIC] Candidate pool page=%s seeded=%s", page, rng is not None)
            search_query = f"painting {query}" if query else "painting"
            url = (
                f"https://api.artic.edu/api/v1/artworks/search"
                f"?q={search_query}&query[term][is_public_domain]=true"
                f"&fields=id,title,artist_title,artist_display,date_display,medium_display,image_id,classification_title,place_of_origin,department_title,style_titles,is_public_domain,copyright_notice,credit_line"
                f"&limit={limit}&page={page}"
            )
            
            headers = aic_request_headers(
                url, {"User-Agent": "InstagramArtBot/1.0"}
            )
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                self._record_source_failure(classify_http_failure(res.status_code, res.headers))
                if res.status_code in {403, 429}:
                    raise AdapterHTTPError(self.source_id, res.status_code)
                logger.warning(f"[AIC] API returned {res.status_code}")
                return candidates

            try:
                payload = res.json()
            except ValueError:
                logger.warning("[AIC] API returned malformed JSON.")
                self._record_source_failure("INVALID_RESPONSE")
                return candidates
            artworks = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(artworks, list):
                logger.warning("[AIC] API returned an unexpected JSON payload.")
                self._record_source_failure("INVALID_RESPONSE")
                return candidates
            
            for item in artworks:
                if item.get("is_public_domain") is not True:
                    logger.debug(f"[AIC] Rejected {item.get('id')}: rights not confirmed.")
                    continue

                image_id = item.get("image_id")
                if not image_id:
                    continue
                    
                cls = item.get("classification_title", "")
                if not cls or "painting" not in cls.lower():
                    continue
                    
                image_url = (
                    f"https://www.artic.edu/iiif/2/{image_id}"
                    f"/full/{AIC_PUBLIC_DOMAIN_IIIF_WIDTH},/0/default.jpg"
                )
                
                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(item.get("id")),
                    title=item.get("title") or "Untitled",
                    artist_name=item.get("artist_title") or "Unknown Artist",
                    creation_date=item.get("date_display") or "Unknown Date",
                    medium=item.get("medium_display") or "",
                    geographic_origin=metadata_text(item.get("place_of_origin")),
                    artist_nationality=metadata_text(item.get("artist_display")),
                    region=infer_region(
                        geography=item.get("place_of_origin"),
                        artist_nationality=item.get("artist_display"),
                        department=item.get("department_title"),
                        style_or_period=item.get("style_titles"),
                    ),
                    department=metadata_text(item.get("department_title")),
                    classification=item.get("classification_title"),
                    style_or_period=metadata_text(item.get("style_titles")),
                    museum_name="Art Institute of Chicago",
                    image_url=image_url,
                    credit_line=item.get("credit_line"),
                    is_public_domain=True,
                    rights_status="CONFIRMED_PUBLIC_DOMAIN",
                    rights_text=item.get("copyright_notice"),
                )
                candidates.append(artwork)
                
        except AdapterHTTPError:
            raise
        except Exception as e:
            logger.error("[AIC] Error fetching candidates (%s).", type(e).__name__)
            self._record_source_failure(classify_exception(e))
            
        return candidates
