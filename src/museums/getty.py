import logging
import random
import re
from typing import Any, List

import requests

from .base import MuseumAdapter
from src.models import NormalizedArtwork, normalize_image_dimensions

logger = logging.getLogger(__name__)

GETTY_ACTIVITY_STREAM = "https://data.getty.edu/museum/collection/activity-stream"
GETTY_CC0_URIS = {
    "http://creativecommons.org/publicdomain/zero/1.0/",
    "https://creativecommons.org/publicdomain/zero/1.0/",
}


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/ld+json", "User-Agent": "InstagramArtBot/1.0"},
            timeout=20,
        )
    except requests.RequestException as error:
        logger.warning("[Getty] Request failed (%s).", type(error).__name__)
        return None
    if response.status_code != 200:
        logger.warning("[Getty] API returned %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("[Getty] API returned malformed JSON.")
        return None
    if not isinstance(payload, dict):
        logger.warning("[Getty] API returned an unexpected JSON payload.")
        return None
    return payload


def _first_label(value: object, default: str = "") -> str:
    if isinstance(value, dict) and isinstance(value.get("_label"), str):
        return value["_label"]
    if isinstance(value, list):
        for item in value:
            label = _first_label(item)
            if label:
                return label
    return default


def _media_is_cc0(media: dict[str, Any]) -> bool:
    rights = media.get("subject_to", [])
    if not isinstance(rights, list):
        return False
    for right in rights:
        if not isinstance(right, dict):
            continue
        classifications = right.get("classified_as", [])
        if not isinstance(classifications, list):
            continue
        if any(isinstance(item, dict) and item.get("id") in GETTY_CC0_URIS for item in classifications):
            return True
    return False


def _media_image(media: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
    delivered = media.get("digitally_shown_by", [])
    if not isinstance(delivered, list):
        return None
    for digital_object in delivered:
        if not isinstance(digital_object, dict):
            continue
        access_points = digital_object.get("access_point", [])
        if not isinstance(access_points, list):
            continue
        base_url = next(
            (
                point.get("id")
                for point in access_points
                if isinstance(point, dict)
                and isinstance(point.get("id"), str)
                and point["id"].startswith("https://media.getty.edu/iiif/image/")
            ),
            None,
        )
        if not base_url:
            continue
        width = height = None
        dimensions = digital_object.get("dimension", [])
        if isinstance(dimensions, list):
            values = {}
            for dimension in dimensions:
                if not isinstance(dimension, dict):
                    continue
                label = _first_label(dimension.get("classified_as")).casefold()
                if label in {"width", "height"}:
                    values[label] = dimension.get("value")
            width, height = normalize_image_dimensions(values.get("width"), values.get("height"))
        return f"{base_url}/full/2000,/0/default.jpg", width, height
    return None


def _object_media_urls(record: dict[str, Any]) -> list[str]:
    shows = record.get("shows", [])
    if not isinstance(shows, list):
        return []
    return [
        item["id"]
        for item in shows
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"].startswith("https://data.getty.edu/media/image/")
    ]


class GettyAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "getty"

    def fetch_candidates(
        self,
        limit: int = 20,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        """Discover a bounded random activity page and admit only CC0 image records.

        Getty's official ActivityStream is discovery-only; it does not provide a
        supported themed search surface, so ``query`` intentionally has no effect.
        """
        collection = _get_json(GETTY_ACTIVITY_STREAM)
        if collection is None:
            return []
        last_id = collection.get("last", {}).get("id") if isinstance(collection.get("last"), dict) else None
        match = re.search(r"/page/(\d+)$", last_id) if isinstance(last_id, str) else None
        if match is None:
            logger.warning("[Getty] ActivityStream did not include a valid final page.")
            return []
        random_source = rng or random
        page = random_source.randint(1, int(match.group(1)))
        activity_page = _get_json(f"{GETTY_ACTIVITY_STREAM}/page/{page}")
        if activity_page is None:
            return []
        entries = activity_page.get("orderedItems", [])
        if not isinstance(entries, list):
            return []
        object_urls = [
            item["object"]["id"]
            for item in entries
            if isinstance(item, dict)
            and isinstance(item.get("object"), dict)
            and item["object"].get("type") == "HumanMadeObject"
            and isinstance(item["object"].get("id"), str)
            and item["object"]["id"].startswith("https://data.getty.edu/museum/collection/object/")
        ]
        requested = max(1, min(limit, 10))
        sampled_urls = random_source.sample(object_urls, min(requested, len(object_urls)))
        candidates = []
        for object_url in sampled_urls:
            record = _get_json(object_url)
            if record is None:
                continue
            image_url = width = height = None
            # Getty's object ``shows`` ordering identifies the selected/preferred
            # image.  Do not substitute a different CC0 image when that image is
            # restricted; its rights belong to a different digital object.
            for media_url in _object_media_urls(record)[:1]:
                media = _get_json(media_url)
                if media is None or not _media_is_cc0(media):
                    continue
                image = _media_image(media)
                if image is not None:
                    image_url, width, height = image
                    break
            if image_url is None:
                continue
            object_id = object_url.rsplit("/", 1)[-1]
            produced_by = record.get("produced_by", {})
            if not isinstance(produced_by, dict):
                produced_by = {}
            timespan = produced_by.get("timespan", {})
            classifications = record.get("classified_as", [])
            if not isinstance(classifications, list):
                classifications = []
            candidates.append(
                NormalizedArtwork(
                    source=self.source_id,
                    source_id=object_id,
                    title=record.get("_label") if isinstance(record.get("_label"), str) else "Untitled",
                    artist_name=_first_label(produced_by.get("carried_out_by"), "Unknown Artist"),
                    creation_date=_first_label(timespan, "Unknown Date"),
                    medium=_first_label(record.get("made_of")),
                    classification=", ".join(
                        item["_label"]
                        for item in classifications
                        if isinstance(item, dict) and isinstance(item.get("_label"), str)
                    )
                    or None,
                    museum_name="J. Paul Getty Museum",
                    artwork_url=object_url,
                    image_url=image_url,
                    image_width=width,
                    image_height=height,
                    license="CC0",
                    is_public_domain=True,
                    rights_status="CONFIRMED_OPEN_ACCESS",
                    rights_text="CC0 image metadata",
                )
            )
        return candidates
