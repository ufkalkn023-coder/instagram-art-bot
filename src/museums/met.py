import logging
import random
import requests
import re
from typing import List
from .base import AdapterHTTPError, MuseumAdapter
from src.models import NormalizedArtwork
from src.region import infer_region, metadata_text
import config
from src.source_health import classify_exception, classify_http_failure

logger = logging.getLogger(__name__)

MET_SEARCH_PAGE_LIMIT = 500
MET_SEARCH_RESULT_LIMIT = 10_000

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
            search_term = query if query else random_source.choice(MET_SEARCH_TERMS)
            headers = {"User-Agent": "InstagramArtBot/1.0"}
            search_params = {
                "hasImages": "true",
                "medium": "Paintings",
                "q": search_term,
                "offset": 0,
                "limit": MET_SEARCH_PAGE_LIMIT,
            }
            first_page = self._fetch_search_page(headers, search_params)
            if first_page is None:
                return candidates

            total, object_ids = first_page
            if not object_ids:
                return candidates

            accessible_total = min(total, MET_SEARCH_RESULT_LIMIT)
            page_count = (
                accessible_total + MET_SEARCH_PAGE_LIMIT - 1
            ) // MET_SEARCH_PAGE_LIMIT
            if page_count > 1:
                page_index = random_source.randrange(page_count)
                offset = page_index * MET_SEARCH_PAGE_LIMIT
                if offset:
                    page_limit = min(
                        MET_SEARCH_PAGE_LIMIT,
                        accessible_total - offset,
                        MET_SEARCH_RESULT_LIMIT - offset,
                    )
                    page = self._fetch_search_page(
                        headers,
                        {**search_params, "offset": offset, "limit": page_limit},
                    )
                    if page is None:
                        return candidates
                    _, object_ids = page
                    if not object_ids:
                        return candidates

            sample_ids = random_source.sample(object_ids, min(limit, len(object_ids)))
            logger.debug(
                "[Met] Candidate pool sample_size=%s seeded=%s",
                len(sample_ids),
                rng is not None,
            )

            for obj_id in sample_ids:
                detail_url = f"{config.MET_API_BASE}/objects/{obj_id}"
                d_res = requests.get(detail_url, headers=headers, timeout=15)
                if d_res.status_code != 200:
                    if d_res.status_code in {403, 429}:
                        category = classify_http_failure(
                            d_res.status_code, d_res.headers
                        )
                        self._record_source_failure(category)
                        raise AdapterHTTPError(
                            self.source_id,
                            d_res.status_code,
                            operation="object",
                            category=category,
                        )
                    continue

                try:
                    detail = d_res.json()
                except ValueError:
                    self._record_source_failure("INVALID_RESPONSE")
                    return []
                if not isinstance(detail, dict):
                    self._record_source_failure("INVALID_RESPONSE")
                    return []
                title = detail.get("title") or "Untitled"
                object_name = detail.get("objectName") or ""
                classification = detail.get("classification") or ""
                medium = detail.get("medium") or ""
                culture = metadata_text(detail.get("culture"))
                geographic_origin = metadata_text(
                    [detail.get("city"), detail.get("country"), detail.get("region"), detail.get("subregion"), detail.get("locale")]
                )
                artist_nationality = metadata_text(detail.get("artistNationality"))
                department = metadata_text(detail.get("department"))
                style_or_period = metadata_text([detail.get("period"), detail.get("dynasty"), detail.get("reign")])

                if not is_painting(title, object_name, classification, medium):
                    continue

                image_url = detail.get("primaryImage") or detail.get("primaryImageSmall")
                if not image_url:
                    continue

                public_domain_flag = detail.get("isPublicDomain")
                is_public_domain = public_domain_flag is True
                rights_text = detail.get("rightsAndReproduction")
                artwork = NormalizedArtwork(
                    source=self.source_id,
                    source_id=str(obj_id),
                    title=title,
                    artist_name=detail.get("artistDisplayName") or "Unknown Artist",
                    creation_date=detail.get("objectDate") or "Unknown Date",
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
                    classification=classification,
                    style_or_period=style_or_period,
                    museum_name="The Metropolitan Museum of Art",
                    image_url=image_url,
                    artwork_url=detail.get("objectURL"),
                    credit_line=detail.get("creditLine"),
                    license="The Met Open Access" if is_public_domain else None,
                    is_public_domain=is_public_domain,
                    rights_status=(
                        "CONFIRMED_PUBLIC_DOMAIN"
                        if is_public_domain
                        else "KNOWN_RESTRICTED"
                        if public_domain_flag is False
                        else None
                    ),
                    rights_text=rights_text,
                    copyright_notice=rights_text,
                )
                candidates.append(artwork)

        except AdapterHTTPError:
            raise
        except Exception as e:
            logger.error("[Met] Error fetching candidates (%s).", type(e).__name__)
            self._record_source_failure(classify_exception(e))

        return candidates

    def _fetch_search_page(
        self,
        headers: dict[str, str],
        params: dict[str, str | int],
    ) -> tuple[int, list[int]] | None:
        response = requests.get(
            f"{config.MET_SEARCH_API_BASE}/search",
            params=params,
            headers=headers,
            timeout=20,
        )
        if response.status_code != 200:
            category = classify_http_failure(response.status_code, response.headers)
            self._record_source_failure(category)
            if response.status_code in {403, 429}:
                raise AdapterHTTPError(
                    self.source_id,
                    response.status_code,
                    operation="search",
                    category=category,
                )
            logger.warning("[Met] API returned %s", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            self._record_source_failure("INVALID_RESPONSE")
            return None

        if not isinstance(payload, dict):
            self._record_source_failure("INVALID_RESPONSE")
            return None
        total = payload.get("total")
        object_ids = payload.get("objectIDs")
        if total == 0 and object_ids is None:
            object_ids = []
        offset = params["offset"]
        requested_limit = params["limit"]
        malformed = (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or not isinstance(object_ids, list)
            or len(object_ids) > requested_limit
            or total < offset + len(object_ids)
            or (total > offset and not object_ids)
            or any(
                not isinstance(object_id, int)
                or isinstance(object_id, bool)
                or object_id <= 0
                for object_id in object_ids
            )
        )
        if malformed:
            self._record_source_failure("INVALID_RESPONSE")
            return None
        return total, object_ids
