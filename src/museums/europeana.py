import logging
import os
import random
from typing import Any, List
from urllib.parse import quote

import requests

from .base import MuseumAdapter
from src.models import NormalizedArtwork, normalize_image_dimensions

logger = logging.getLogger(__name__)

EUROPEANA_API_BASE = "https://api.europeana.eu/record/v2"
EUROPEANA_ALLOWED_RIGHTS = {
    "http://creativecommons.org/publicdomain/mark/1.0/": "CONFIRMED_PUBLIC_DOMAIN",
    "https://creativecommons.org/publicdomain/mark/1.0/": "CONFIRMED_PUBLIC_DOMAIN",
    "http://creativecommons.org/publicdomain/zero/1.0/": "CONFIRMED_OPEN_ACCESS",
    "https://creativecommons.org/publicdomain/zero/1.0/": "CONFIRMED_OPEN_ACCESS",
}


def _first_text(value: object, default: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return default


def _rights_status(value: object) -> tuple[str, str] | None:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else None
    if not values or not all(isinstance(item, str) for item in values):
        return None
    normalized = [item.strip() for item in values]
    if len(normalized) != 1:
        return None
    status = EUROPEANA_ALLOWED_RIGHTS.get(normalized[0])
    return (normalized[0], status) if status else None


def _image_resource(record: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]] | None:
    aggregations = record.get("aggregations", [])
    if not isinstance(aggregations, list):
        return None
    for aggregation in aggregations:
        if not isinstance(aggregation, dict):
            continue
        image_url = aggregation.get("edmIsShownBy")
        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            continue
        resources = aggregation.get("webResources", [])
        if not isinstance(resources, list):
            continue
        for resource in resources:
            if not isinstance(resource, dict) or resource.get("about") != image_url:
                continue
            rights = resource.get("edmRights", resource.get("rights"))
            accepted_rights = _rights_status(rights)
            if accepted_rights:
                rights_uri, rights_status = accepted_rights
                return image_url, rights_uri, rights_status, resource
    return None


class EuropeanaAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "europeana"

    def fetch_candidates(
        self,
        limit: int = 20,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        api_key = os.environ.get("EUROPEANA_API_KEY", "").strip()
        if not api_key:
            logger.info("[Europeana] No API key configured, skipping.")
            return []

        requested = max(1, min(limit, 10))
        try:
            response = requests.get(
                f"{EUROPEANA_API_BASE}/search.json",
                params={
                    "wskey": api_key,
                    "query": query or "painting",
                    "rows": requested,
                    "media": "true",
                    "profile": "rich",
                    "reusability": "open",
                },
                headers={"User-Agent": "InstagramArtBot/1.0"},
                timeout=20,
            )
        except requests.RequestException as error:
            logger.warning("[Europeana] Search request failed (%s).", type(error).__name__)
            return []
        if response.status_code != 200:
            logger.warning("[Europeana] API returned %s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            logger.warning("[Europeana] API returned malformed JSON.")
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            logger.warning("[Europeana] API returned an unexpected JSON payload.")
            return []

        items = payload["items"]
        random_source = rng or random
        sampled_items = random_source.sample(items, min(requested, len(items)))
        candidates = []
        for item in sampled_items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            record_id = item["id"].lstrip("/")
            if not record_id:
                continue
            canonical_source_id = quote(record_id, safe="")
            try:
                record_response = requests.get(
                    f"{EUROPEANA_API_BASE}/{record_id}.json",
                    params={"wskey": api_key, "profile": "rich"},
                    headers={"User-Agent": "InstagramArtBot/1.0"},
                    timeout=20,
                )
            except requests.RequestException as error:
                logger.warning("[Europeana] Record request failed (%s).", type(error).__name__)
                continue
            if record_response.status_code != 200:
                logger.warning("[Europeana] Record API returned %s", record_response.status_code)
                continue
            try:
                record_payload = record_response.json()
            except ValueError:
                logger.warning("[Europeana] Record API returned malformed JSON.")
                continue
            record = record_payload.get("object") if isinstance(record_payload, dict) else None
            if not isinstance(record, dict):
                continue
            image = _image_resource(record)
            if image is None:
                continue
            image_url, rights_uri, rights_status, resource = image
            width, height = normalize_image_dimensions(resource.get("ebucoreWidth"), resource.get("ebucoreHeight"))
            provider = _first_text(record.get("dataProvider"), _first_text(record.get("provider"), "Europeana"))
            candidates.append(
                NormalizedArtwork(
                    source=self.source_id,
                    source_id=canonical_source_id,
                    title=_first_text(record.get("title"), "Untitled"),
                    artist_name=_first_text(record.get("dcCreator"), "Unknown Artist"),
                    creation_date=_first_text(record.get("year"), "Unknown Date"),
                    medium=_first_text(record.get("dcFormat")),
                    classification=_first_text(record.get("type")),
                    museum_name=provider,
                    artwork_url=f"https://www.europeana.eu/item/{record_id}",
                    image_url=image_url,
                    image_width=width,
                    image_height=height,
                    license=rights_uri,
                    is_public_domain=True,
                    rights_status=rights_status,
                    rights_text=rights_uri,
                )
            )
        return candidates
