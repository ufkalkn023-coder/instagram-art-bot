import logging
import os
import random
from typing import Any, List

import requests

from .base import MuseumAdapter
from src.models import NormalizedArtwork, normalize_image_dimensions

logger = logging.getLogger(__name__)

SMITHSONIAN_API_BASE = "https://api.si.edu/openaccess/api/v1.0"
SMITHSONIAN_CC0 = "CC0"


def _first_text(value: object, default: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return default


def _freetext_value(content: dict[str, Any], field: str) -> str:
    entries = content.get("freetext", {}).get(field, [])
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
            return entry["content"].strip()
    return ""


def _cc0_image_media(content: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    descriptive = content.get("descriptiveNonRepeating", {})
    if not isinstance(descriptive, dict):
        return None
    online_media = descriptive.get("online_media", {})
    media_items = online_media.get("media", []) if isinstance(online_media, dict) else []
    if not isinstance(media_items, list):
        return None

    for media in media_items:
        if not isinstance(media, dict) or media.get("type") != "Images":
            continue
        usage = media.get("usage", {})
        if (
            not isinstance(usage, dict)
            or not isinstance(usage.get("access"), str)
            or usage["access"].strip().upper() != SMITHSONIAN_CC0
        ):
            continue
        resources = media.get("resources", [])
        if not isinstance(resources, list):
            resources = []
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            url = resource.get("url")
            if isinstance(url, str) and url.startswith("https://") and not url.lower().endswith(".tif"):
                return media, resource
    return None


class SmithsonianAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "smithsonian"

    def fetch_candidates(
        self,
        limit: int = 20,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        api_key = os.environ.get("SMITHSONIAN_API_KEY", "").strip()
        if not api_key:
            logger.info("[Smithsonian] No API key configured, skipping.")
            return []

        requested = max(1, min(limit, 20))
        search_query = query or "painting"
        params = {
            "api_key": api_key,
            "q": f'({search_query}) AND online_media_type:"Images" AND media_usage:"CC0"',
            "rows": min(max(requested * 2, 10), 40),
            "sort": "id",
        }
        try:
            response = requests.get(
                f"{SMITHSONIAN_API_BASE}/search",
                params=params,
                headers={"User-Agent": "InstagramArtBot/1.0"},
                timeout=20,
            )
        except requests.RequestException as error:
            logger.warning("[Smithsonian] Search request failed (%s).", type(error).__name__)
            return []
        if response.status_code != 200:
            logger.warning("[Smithsonian] API returned %s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            logger.warning("[Smithsonian] API returned malformed JSON.")
            return []
        if not isinstance(payload, dict):
            logger.warning("[Smithsonian] API returned an unexpected JSON payload.")
            return []

        response_data = payload.get("response", {})
        rows = response_data.get("rows", []) if isinstance(response_data, dict) else []
        if not isinstance(rows, list):
            return []
        random_source = rng or random
        sampled_rows = random_source.sample(rows, min(requested, len(rows)))
        candidates = []
        for item in sampled_rows:
            if not isinstance(item, dict):
                continue
            content = item.get("content", {})
            if not isinstance(content, dict):
                continue
            media_and_resource = _cc0_image_media(content)
            if media_and_resource is None:
                continue
            media, resource = media_and_resource
            source_record_id = item.get("id")
            if not isinstance(source_record_id, str) or not source_record_id:
                continue
            descriptive = content.get("descriptiveNonRepeating", {})
            indexed = content.get("indexedStructured", {})
            if not isinstance(descriptive, dict) or not isinstance(indexed, dict):
                continue
            image_width, image_height = normalize_image_dimensions(resource.get("width"), resource.get("height"))
            candidates.append(
                NormalizedArtwork(
                    source=self.source_id,
                    source_id=source_record_id,
                    title=_first_text(item.get("title"), "Untitled"),
                    artist_name=_first_text(indexed.get("name"), "Unknown Artist"),
                    creation_date=_first_text(indexed.get("date"), "Unknown Date"),
                    medium=_freetext_value(content, "physicalDescription"),
                    classification=_first_text(indexed.get("object_type")),
                    museum_name=f"Smithsonian Institution ({item.get('unitCode') or 'Open Access'})",
                    artwork_url=descriptive.get("record_link") if isinstance(descriptive.get("record_link"), str) else None,
                    image_url=resource["url"],
                    image_width=image_width,
                    image_height=image_height,
                    license=SMITHSONIAN_CC0,
                    is_public_domain=True,
                    rights_status="CONFIRMED_OPEN_ACCESS",
                    rights_text=SMITHSONIAN_CC0,
                )
            )
        return candidates
