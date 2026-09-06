"""Keyless Rijksmuseum Data Services adapter.

Discovery returns only persistent identifiers. Candidate normalization therefore
uses the documented Linked Art, EDM, and IIIF resolver chain, with strict bounds
on both search pagination and per-candidate resolution work.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Iterable, Mapping
from typing import Any, List
from urllib.parse import urlparse

import requests

from .base import AdapterHTTPError, MuseumAdapter
from src.models import NormalizedArtwork, normalize_image_dimensions
from src.region import infer_region
from src.source_health import classify_exception, classify_http_failure

logger = logging.getLogger(__name__)

SEARCH_URL = "https://data.rijksmuseum.nl/search/collection"
DATA_HOST = "data.rijksmuseum.nl"
PID_HOST = "id.rijksmuseum.nl"
IIIF_HOST = "iiif.micr.io"
REQUEST_TIMEOUT_SECONDS = 20
MAX_SEARCH_PAGES = 2
MAX_RESOLUTION_ATTEMPTS = 8
MAX_MEDIA_REFERENCES = 2
MAX_DETAIL_REQUESTS = 40
IIIF_LONG_EDGE = 2560

ENGLISH_LANGUAGE_IDS = frozenset(
    {"http://vocab.getty.edu/aat/300388277", "https://vocab.getty.edu/aat/300388277"}
)
DUTCH_LANGUAGE_IDS = frozenset(
    {"http://vocab.getty.edu/aat/300388256", "https://vocab.getty.edu/aat/300388256"}
)
OBJECT_NUMBER_TYPES = frozenset(
    {
        "http://vocab.getty.edu/aat/300312355",
        "https://vocab.getty.edu/aat/300312355",
        "https://id.rijksmuseum.nl/22015218",
    }
)
PRIMARY_TITLE_TYPE = "http://vocab.getty.edu/aat/300417200"
DISPLAY_TITLE_TYPE = "http://vocab.getty.edu/aat/300417207"
DESCRIPTION_TYPE = "http://vocab.getty.edu/aat/300048722"
WORK_TYPE_CLASSIFICATION = "http://vocab.getty.edu/aat/300435443"

RIGHTS_BY_PATH = {
    ("creativecommons.org", "/publicdomain/mark/1.0"): "CONFIRMED_PUBLIC_DOMAIN",
    ("creativecommons.org", "/publicdomain/zero/1.0"): "CONFIRMED_OPEN_ACCESS",
}
UNIT_LABELS = {
    "http://vocab.getty.edu/aat/300379098": "cm",
    "http://vocab.getty.edu/aat/300379226": "kg",
}


class _CandidateRequestError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class _ResolutionBudgetExhausted(RuntimeError):
    pass


class _RequestBudget:
    def __init__(self, limit: int):
        self.remaining = limit

    def consume(self) -> None:
        if self.remaining <= 0:
            raise _ResolutionBudgetExhausted
        self.remaining -= 1


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _dicts(value: object) -> list[dict[str, Any]]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _classification_ids(node: Mapping[str, Any]) -> set[str]:
    return {
        item["id"]
        for item in _dicts(node.get("classified_as"))
        if isinstance(item.get("id"), str)
    }


def _language_rank(node: Mapping[str, Any]) -> int:
    languages = _dicts(node.get("language"))
    language_ids = {
        language.get("id") for language in languages if isinstance(language.get("id"), str)
    }
    inline_language = node.get("@language")
    if inline_language == "en" or language_ids.intersection(ENGLISH_LANGUAGE_IDS):
        return 0
    if inline_language == "nl" or language_ids.intersection(DUTCH_LANGUAGE_IDS):
        return 1
    if not inline_language and not language_ids:
        return 2
    return 3


def _preferred_text(value: object, *, fields: tuple[str, ...] = ("content", "@value")) -> str:
    choices: list[tuple[int, int, str]] = []
    for index, item in enumerate(_as_list(value)):
        if isinstance(item, str) and item.strip():
            choices.append((2, index, item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        for field in fields:
            text = item.get(field)
            if isinstance(text, str) and text.strip():
                choices.append((_language_rank(item), index, text.strip()))
                break
    return min(choices, default=(4, 0, ""))[2]


def _label(node: Mapping[str, Any]) -> str:
    return _preferred_text(node.get("notation")) or _preferred_text(node.get("identified_by"))


def _unique_join(values: Iterable[str]) -> str | None:
    unique = list(dict.fromkeys(value for value in values if value))
    return "; ".join(unique) if unique else None


def _iter_nodes(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nodes(child)


def _persistent_integer(identifier: object) -> str | None:
    if not isinstance(identifier, str):
        return None
    try:
        parsed = urlparse(identifier)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != PID_HOST or parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(r"/(\d+)", parsed.path)
    return match.group(1) if match else None


def _resolver_url(identifier: object, profile: str = "la-framed") -> str | None:
    integer = _persistent_integer(identifier)
    return f"https://{DATA_HOST}/{integer}?_profile={profile}" if integer else None


def _valid_search_page_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != DATA_HOST:
        return None
    return value if parsed.path == "/search/collection" else None


def get_rights_status(rights: object) -> str | None:
    """Map only explicit, supported EDM rights URIs to confirmed statuses."""
    if not isinstance(rights, str):
        return None
    try:
        parsed = urlparse(rights.strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.casefold() != "creativecommons.org"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return RIGHTS_BY_PATH.get(("creativecommons.org", parsed.path.rstrip("/")))


def _object_number(record: Mapping[str, Any]) -> str | None:
    for identifier in _dicts(record.get("identified_by")):
        content = identifier.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        labels = {
            _label(classification).casefold()
            for classification in _dicts(identifier.get("classified_as"))
        }
        if _classification_ids(identifier).intersection(OBJECT_NUMBER_TYPES) or labels.intersection(
            {"object number", "objectnummer"}
        ):
            return content.strip()
    return None


def _title(record: Mapping[str, Any]) -> str:
    names = [item for item in _dicts(record.get("identified_by")) if item.get("type") == "Name"]
    for title_type in (PRIMARY_TITLE_TYPE, DISPLAY_TITLE_TYPE):
        title = _preferred_text(
            [name for name in names if title_type in _classification_ids(name)]
        )
        if title:
            return title
    return _preferred_text(names) or "Untitled"


def _creator(record: Mapping[str, Any]) -> str:
    production = record.get("produced_by")
    if not isinstance(production, dict):
        return "Unknown Artist"
    for part in _dicts(production.get("part")):
        for actor in _dicts(part.get("carried_out_by")):
            creator = _preferred_text(actor.get("notation")) or _preferred_text(actor.get("identified_by"))
            if creator:
                return creator
    return _preferred_text(production.get("referred_to_by")) or "Unknown Artist"


def _creation_date(record: Mapping[str, Any]) -> str:
    production = record.get("produced_by")
    timespan = production.get("timespan") if isinstance(production, dict) else None
    if not isinstance(timespan, dict):
        return "Unknown Date"
    display = _preferred_text(timespan.get("identified_by"))
    if display:
        return display
    begin = timespan.get("begin_of_the_begin")
    end = timespan.get("end_of_the_end")
    begin_year = begin[:4] if isinstance(begin, str) and re.match(r"^\d{4}", begin) else None
    end_year = end[:4] if isinstance(end, str) and re.match(r"^\d{4}", end) else None
    if begin_year and end_year:
        return begin_year if begin_year == end_year else f"{begin_year}\u2013{end_year}"
    return begin_year or end_year or "Unknown Date"


def _work_types(record: Mapping[str, Any]) -> str | None:
    values = []
    for classification in _dicts(record.get("classified_as")):
        nested_ids = _classification_ids(classification)
        if not nested_ids or WORK_TYPE_CLASSIFICATION in nested_ids:
            values.append(_label(classification))
    return _unique_join(values)


def _materials(record: Mapping[str, Any]) -> str | None:
    return _unique_join(_label(material) for material in _dicts(record.get("made_of")))


def _dimensions(record: Mapping[str, Any]) -> str | None:
    values = []
    for dimension in _dicts(record.get("dimension")):
        amount = dimension.get("value")
        if not isinstance(amount, (str, int, float)) or isinstance(amount, bool):
            continue
        kind = _preferred_text(
            [
                notation
                for item in _dicts(dimension.get("classified_as"))
                for notation in _as_list(item.get("notation"))
            ]
        )
        unit = dimension.get("unit")
        unit_id = unit.get("id") if isinstance(unit, dict) else None
        values.append(" ".join(part for part in (kind, str(amount), UNIT_LABELS.get(unit_id, "")) if part))
    return _unique_join(values)


def _description(record: Mapping[str, Any]) -> str | None:
    descriptions = [
        node
        for node in _iter_nodes(record.get("subject_of"))
        if node.get("type") == "LinguisticObject"
        and DESCRIPTION_TYPE in _classification_ids(node)
        and isinstance(node.get("content"), str)
    ]
    return _preferred_text(descriptions) or None


def _artwork_url(record: Mapping[str, Any]) -> str | None:
    for node in _iter_nodes(record.get("subject_of")):
        identifier = node.get("id")
        if isinstance(identifier, str) and identifier.startswith("https://www.rijksmuseum.nl/"):
            return identifier
    return None


def _geography(record: Mapping[str, Any]) -> str | None:
    production = record.get("produced_by")
    if not isinstance(production, dict):
        return None
    return _unique_join(
        _label(place)
        for part in _dicts(production.get("part"))
        for place in _dicts(part.get("took_place_at"))
    )


def _reference_ids(value: object, expected_type: str) -> list[str]:
    identifiers = []
    for item in _dicts(value):
        identifier = item.get("id")
        if item.get("type") == expected_type and _persistent_integer(identifier):
            identifiers.append(identifier)
    return list(dict.fromkeys(identifiers))[:MAX_MEDIA_REFERENCES]


def _iiif_service_root(access_point: object) -> str | None:
    if not isinstance(access_point, str):
        return None
    try:
        parsed = urlparse(access_point)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != IIIF_HOST or parsed.query or parsed.fragment:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
        return None
    return f"https://{IIIF_HOST}/{parts[0]}"


def _bounded_image_url(service_root: str, width: int, height: int) -> str:
    if width >= height:
        size = f"{min(width, IIIF_LONG_EDGE)},"
    else:
        size = f",{min(height, IIIF_LONG_EDGE)}"
    return f"{service_root}/full/{size}/0/default.jpg"


class RijksmuseumAdapter(MuseumAdapter):
    @property
    def source_id(self) -> str:
        return "rijksmuseum"

    def unavailable_reason(self) -> str | None:
        return None

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Accept": "application/ld+json", "User-Agent": "InstagramArtBot/1.0"}

    def _search_json(self, url: str, *, params: dict[str, str] | None, operation: str) -> dict[str, Any] | None:
        try:
            response = requests.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as error:
            self._record_source_failure(classify_exception(error))
            logger.warning("[Rijksmuseum] %s request failed (%s).", operation, type(error).__name__)
            return None
        if response.status_code != 200:
            category = classify_http_failure(response.status_code, getattr(response, "headers", {}))
            self._record_source_failure(category)
            if response.status_code in {403, 429}:
                raise AdapterHTTPError(
                    self.source_id,
                    response.status_code,
                    operation=operation,
                    category=category,
                )
            logger.warning("[Rijksmuseum] %s returned %s.", operation, response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            self._record_source_failure("INVALID_RESPONSE")
            logger.warning("[Rijksmuseum] %s returned malformed JSON.", operation)
            return None
        if not isinstance(payload, dict):
            self._record_source_failure("INVALID_RESPONSE")
            return None
        return payload

    def _candidate_json(
        self,
        url: str,
        operation: str,
        request_budget: _RequestBudget,
    ) -> dict[str, Any]:
        request_budget.consume()
        try:
            response = requests.get(
                url,
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as error:
            raise _CandidateRequestError(classify_exception(error)) from error
        if response.status_code != 200:
            category = classify_http_failure(response.status_code, getattr(response, "headers", {}))
            if response.status_code in {403, 429}:
                raise AdapterHTTPError(
                    self.source_id,
                    response.status_code,
                    operation=operation,
                    category=category,
                )
            raise _CandidateRequestError(category)
        try:
            payload = response.json()
        except ValueError as error:
            raise _CandidateRequestError("INVALID_RESPONSE") from error
        if not isinstance(payload, dict):
            raise _CandidateRequestError("INVALID_RESPONSE")
        return payload

    def _resolve_media(
        self,
        record: Mapping[str, Any],
        detail_failures: list[str],
        request_budget: _RequestBudget,
    ) -> tuple[str, int, int] | None:
        visual_ids = _reference_ids(record.get("shows"), "VisualItem")
        for visual_id in visual_ids:
            visual_url = _resolver_url(visual_id)
            if visual_url is None:
                continue
            try:
                visual = self._candidate_json(visual_url, "visual_item", request_budget)
            except _CandidateRequestError as error:
                detail_failures.append(error.category)
                continue
            if visual.get("type") != "VisualItem":
                continue
            digital_ids = _reference_ids(visual.get("digitally_shown_by"), "DigitalObject")
            for digital_id in digital_ids:
                digital_url = _resolver_url(digital_id)
                if digital_url is None:
                    continue
                try:
                    digital = self._candidate_json(digital_url, "digital_object", request_budget)
                except _CandidateRequestError as error:
                    detail_failures.append(error.category)
                    continue
                if digital.get("type") != "DigitalObject":
                    continue
                for access_point in _dicts(digital.get("access_point"))[:MAX_MEDIA_REFERENCES]:
                    service_root = _iiif_service_root(access_point.get("id"))
                    if service_root is None:
                        continue
                    try:
                        info = self._candidate_json(
                            f"{service_root}/info.json", "iiif_info", request_budget
                        )
                    except _CandidateRequestError as error:
                        detail_failures.append(error.category)
                        continue
                    width, height = normalize_image_dimensions(info.get("width"), info.get("height"))
                    protocol = info.get("protocol")
                    formats = info.get("formats")
                    if (
                        width is None
                        or height is None
                        or info.get("type") not in {"ImageService2", "ImageService3"}
                        or protocol != "http://iiif.io/api/image"
                        or (formats is not None and not isinstance(formats, list))
                        or (
                            isinstance(formats, list)
                            and not {str(item).casefold() for item in formats}.intersection({"jpg", "jpeg"})
                        )
                    ):
                        continue
                    return _bounded_image_url(service_root, width, height), width, height
        return None

    def _resolve_candidate(
        self,
        persistent_id: str,
        detail_failures: list[str],
        request_budget: _RequestBudget,
    ) -> NormalizedArtwork | None:
        linked_art_url = _resolver_url(persistent_id)
        edm_url = _resolver_url(persistent_id, "edm-framed")
        if linked_art_url is None or edm_url is None:
            return None
        try:
            record = self._candidate_json(linked_art_url, "object", request_budget)
        except _CandidateRequestError as error:
            detail_failures.append(error.category)
            return None
        if record.get("type") != "HumanMadeObject":
            return None
        object_number = _object_number(record)
        if not object_number:
            return None

        rights_uri = None
        try:
            rights_record = self._candidate_json(edm_url, "rights", request_budget)
        except _CandidateRequestError as error:
            detail_failures.append(error.category)
        else:
            if isinstance(rights_record.get("edmRights"), str):
                rights_uri = rights_record["edmRights"].strip() or None
        rights_status = get_rights_status(rights_uri)

        media = self._resolve_media(record, detail_failures, request_budget)
        if media is None:
            return None
        image_url, image_width, image_height = media
        geographic_origin = _geography(record)
        classification = _work_types(record)
        medium = _materials(record)
        creation_date = _creation_date(record)
        return NormalizedArtwork(
            source=self.source_id,
            source_id=object_number,
            title=_title(record),
            artist_name=_creator(record),
            creation_date=creation_date,
            creation_date_display=creation_date,
            medium=medium,
            dimensions=_dimensions(record),
            geographic_origin=geographic_origin,
            region=infer_region(
                geography=geographic_origin,
                style_or_period=classification,
            ),
            classification=classification,
            description=_description(record),
            museum_name="Rijksmuseum, Amsterdam",
            museum_url="https://www.rijksmuseum.nl/en",
            artwork_url=_artwork_url(record),
            image_url=image_url,
            image_width=image_width,
            image_height=image_height,
            license=rights_uri,
            is_public_domain=rights_status in {
                "CONFIRMED_PUBLIC_DOMAIN",
                "CONFIRMED_OPEN_ACCESS",
            },
            rights_status=rights_status or ("KNOWN_RESTRICTED" if rights_uri else None),
            rights_text=rights_uri,
            copyright_notice=rights_uri,
        )

    def fetch_candidates(
        self,
        limit: int = 50,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        self._clear_source_failure()
        if limit <= 0:
            return []

        resolution_budget = min(MAX_RESOLUTION_ATTEMPTS, max(1, limit * 2))
        identifier_pool_target = resolution_budget * 4
        params = {"type": "painting", "imageAvailable": "true"}
        if isinstance(query, str) and query.strip():
            # The repository query is untyped. Description is the closest safe
            # current equivalent to the removed legacy generic ``q`` search.
            params["description"] = query.strip()

        persistent_ids: list[str] = []
        next_url: str | None = SEARCH_URL
        for page_index in range(MAX_SEARCH_PAGES):
            if next_url is None or len(persistent_ids) >= identifier_pool_target:
                break
            payload = self._search_json(
                next_url,
                params=params if page_index == 0 else None,
                operation="search",
            )
            if payload is None:
                return []
            ordered_items = payload.get("orderedItems")
            if payload.get("type") != "OrderedCollectionPage" or not isinstance(
                ordered_items, list
            ):
                self._record_source_failure("INVALID_RESPONSE")
                logger.warning("[Rijksmuseum] Search returned an unexpected payload.")
                return []
            for item in ordered_items:
                if not isinstance(item, dict) or item.get("type") != "HumanMadeObject":
                    continue
                if _persistent_integer(item.get("id")):
                    persistent_ids.append(item["id"])
            persistent_ids = list(dict.fromkeys(persistent_ids))
            next_record = payload.get("next")
            if next_record is None:
                next_url = None
            elif isinstance(next_record, dict):
                next_url = _valid_search_page_url(next_record.get("id"))
                if next_url is None:
                    self._record_source_failure("INVALID_RESPONSE")
                    return []
            else:
                self._record_source_failure("INVALID_RESPONSE")
                return []

        random_source = rng or random
        selected_ids = random_source.sample(
            persistent_ids,
            min(resolution_budget, len(persistent_ids)),
        )
        candidates: list[NormalizedArtwork] = []
        seen_object_numbers: set[str] = set()
        detail_failures: list[str] = []
        request_budget = _RequestBudget(MAX_DETAIL_REQUESTS)
        for persistent_id in selected_ids:
            try:
                candidate = self._resolve_candidate(
                    persistent_id, detail_failures, request_budget
                )
            except _ResolutionBudgetExhausted:
                break
            if candidate is None or candidate.source_id in seen_object_numbers:
                continue
            seen_object_numbers.add(candidate.source_id)
            candidates.append(candidate)
            if len(candidates) >= limit:
                break

        if not candidates and detail_failures:
            self._record_source_failure(detail_failures[0])
        return candidates
