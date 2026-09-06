import random
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from src import production_config, reel_candidate_acquisition
from src.museums import rijksmuseum
from src.museums.base import AdapterHTTPError


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("bad json")
        return self.payload


def _localized(value, language="en"):
    return {"@language": language, "@value": value}


def _linked_art(
    pid,
    object_number="SK-A-1",
    *,
    title="English Title",
    visual_id=None,
    include_optional=True,
):
    visual_id = visual_id or str(20_000 + int(pid))
    record = {
        "id": f"https://id.rijksmuseum.nl/{pid}",
        "type": "HumanMadeObject",
        "identified_by": [
            {
                "type": "Identifier",
                "content": object_number,
                "classified_as": [{"id": "http://vocab.getty.edu/aat/300312355"}],
            },
            {
                "type": "Name",
                "content": "Nederlandse titel",
                "classified_as": [{"id": "http://vocab.getty.edu/aat/300417200"}],
                "language": [{"id": "http://vocab.getty.edu/aat/300388256"}],
            },
            {
                "type": "Name",
                "content": title,
                "classified_as": [{"id": "http://vocab.getty.edu/aat/300417200"}],
                "language": [{"id": "http://vocab.getty.edu/aat/300388277"}],
            },
        ],
        "shows": [{"id": f"https://id.rijksmuseum.nl/{visual_id}", "type": "VisualItem"}],
    }
    if include_optional:
        record.update(
            {
                "produced_by": {
                    "type": "Production",
                    "timespan": {
                        "identified_by": [
                            {"type": "Name", "content": "ca. 1642", "language": [{"id": "http://vocab.getty.edu/aat/300388277"}]}
                        ],
                        "begin_of_the_begin": "1642-01-01T00:00:00Z",
                        "end_of_the_end": "1642-12-31T23:59:59Z",
                    },
                    "part": [
                        {
                            "carried_out_by": [
                                {
                                    "type": "Person",
                                    "notation": [_localized("Kunstenaar", "nl"), _localized("Artist")],
                                }
                            ],
                            "took_place_at": [
                                {"type": "Place", "notation": [_localized("Amsterdam")]}
                            ],
                        }
                    ],
                },
                "classified_as": [
                    {
                        "type": "Type",
                        "notation": [_localized("painting")],
                        "classified_as": [{"id": "http://vocab.getty.edu/aat/300435443"}],
                    }
                ],
                "made_of": [
                    {"type": "Material", "notation": [_localized("oil paint")]},
                    {"type": "Material", "notation": [_localized("canvas")]},
                ],
                "dimension": [
                    {
                        "type": "Dimension",
                        "value": "80",
                        "classified_as": [{"notation": [_localized("height")]}],
                        "unit": {"id": "http://vocab.getty.edu/aat/300379098"},
                    }
                ],
                "subject_of": [
                    {
                        "type": "LinguisticObject",
                        "content": "English description",
                        "classified_as": [{"id": "http://vocab.getty.edu/aat/300048722"}],
                        "language": [{"id": "http://vocab.getty.edu/aat/300388277"}],
                    },
                    {
                        "type": "LinguisticObject",
                        "digitally_carried_by": [
                            {
                                "type": "DigitalObject",
                                "access_point": [
                                    {
                                        "id": f"https://www.rijksmuseum.nl/en/collection/{object_number}",
                                        "type": "DigitalObject",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )
    return record


def _resource_set(pid, object_number="SK-A-1", *, width=4000, height=3000, rights=None, **kwargs):
    visual_id = str(20_000 + int(pid))
    digital_id = str(30_000 + int(pid))
    image_id = f"image{pid}"
    return {
        (pid, "la-framed"): _linked_art(
            pid, object_number, visual_id=visual_id, **kwargs
        ),
        (pid, "edm-framed"): {"edmRights": rights} if rights is not None else {},
        (visual_id, "la-framed"): {
            "id": f"https://id.rijksmuseum.nl/{visual_id}",
            "type": "VisualItem",
            "digitally_shown_by": [
                {"id": f"https://id.rijksmuseum.nl/{digital_id}", "type": "DigitalObject"}
            ],
        },
        (digital_id, "la-framed"): {
            "id": f"https://id.rijksmuseum.nl/{digital_id}",
            "type": "DigitalObject",
            "access_point": [
                {"id": f"https://iiif.micr.io/{image_id}/full/max/0/default.jpg"}
            ],
        },
        (image_id, "info"): {
            "id": f"https://iiif.micr.io/{image_id}",
            "type": "ImageService3",
            "protocol": "http://iiif.io/api/image",
            "formats": ["jpg", "png"],
            "width": width,
            "height": height,
        },
    }


def _install_router(monkeypatch, pages, resources=None, calls=None):
    resources = resources or {}
    calls = calls if calls is not None else []
    page_iter = iter(pages)

    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.startswith(rijksmuseum.SEARCH_URL):
            return FakeResponse(next(page_iter))
        parsed = urlparse(url)
        if parsed.hostname == rijksmuseum.DATA_HOST:
            key = (parsed.path.lstrip("/"), parse_qs(parsed.query).get("_profile", [None])[0])
        elif parsed.hostname == rijksmuseum.IIIF_HOST and parsed.path.endswith("/info.json"):
            key = (parsed.path.split("/")[1], "info")
        else:
            raise AssertionError(f"unexpected URL: {url}")
        payload = resources.get(key)
        if isinstance(payload, BaseException):
            raise payload
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)

    monkeypatch.setattr(rijksmuseum.requests, "get", request)
    return calls


def _search_page(*pids, next_url=None):
    payload = {
        "type": "OrderedCollectionPage",
        "orderedItems": [
            {"id": f"https://id.rijksmuseum.nl/{pid}", "type": "HumanMadeObject"}
            for pid in pids
        ],
    }
    if next_url is not None:
        payload["next"] = {"id": next_url, "type": "OrderedCollectionPage"}
    return payload


def test_keyless_search_uses_current_parameters_and_normalizes_linked_data(monkeypatch):
    resources = _resource_set(
        "1001",
        rights="http://creativecommons.org/publicdomain/mark/1.0/",
    )
    calls = _install_router(monkeypatch, [_search_page("1001")], resources)

    candidate = rijksmuseum.RijksmuseumAdapter().fetch_candidates(
        limit=1, query="night scene", rng=random.Random(1)
    )[0]

    search_url, search_kwargs = calls[0]
    assert search_url == rijksmuseum.SEARCH_URL
    assert search_kwargs["params"] == {
        "type": "painting",
        "imageAvailable": "true",
        "description": "night scene",
    }
    assert "key" not in search_kwargs["params"]
    assert candidate.source_id == "SK-A-1"
    assert candidate.canonical_id == "rijksmuseum_SK-A-1"
    assert candidate.title == "English Title"
    assert candidate.artist_name == "Artist"
    assert candidate.creation_date == "ca. 1642"
    assert candidate.classification == "painting"
    assert candidate.medium == "oil paint; canvas"
    assert candidate.dimensions == "height 80 cm"
    assert candidate.description == "English description"
    assert candidate.artwork_url == "https://www.rijksmuseum.nl/en/collection/SK-A-1"


def test_pagination_follows_opaque_next_id_without_rebuilding_token(monkeypatch):
    next_url = f"{rijksmuseum.SEARCH_URL}?type=painting&pageToken=opaque-value"
    resources = _resource_set("1002", object_number="SK-A-2")
    calls = _install_router(
        monkeypatch,
        [_search_page(next_url=next_url), _search_page("1002")],
        resources,
    )

    result = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1, rng=random.Random(2))

    assert result[0].source_id == "SK-A-2"
    assert calls[1][0] == next_url
    assert calls[1][1]["params"] is None


def test_malformed_pagination_url_fails_closed(monkeypatch):
    adapter = rijksmuseum.RijksmuseumAdapter()
    _install_router(monkeypatch, [_search_page(next_url="https://[invalid")])

    assert adapter.fetch_candidates() == []
    assert adapter.source_failure_category == "INVALID_RESPONSE"


def test_empty_search_response_is_not_a_source_failure(monkeypatch):
    adapter = rijksmuseum.RijksmuseumAdapter()
    calls = _install_router(monkeypatch, [_search_page()])

    assert adapter.fetch_candidates() == []
    assert adapter.source_failure_category is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(json_error=True),
        FakeResponse([]),
        FakeResponse({"artObjects": []}),
        FakeResponse({"type": "UnexpectedCollection", "orderedItems": []}),
    ],
)
def test_malformed_search_response_fails_closed(monkeypatch, response):
    monkeypatch.setattr(rijksmuseum.requests, "get", lambda *args, **kwargs: response)
    adapter = rijksmuseum.RijksmuseumAdapter()

    assert adapter.fetch_candidates() == []
    assert adapter.source_failure_category == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("https://id.rijksmuseum.nl/200107928", "200107928"),
        ("http://id.rijksmuseum.nl/200107928", None),
        ("https://evil.example/200107928", None),
        ("https://id.rijksmuseum.nl/not-numeric", None),
        ("https://id.rijksmuseum.nl/200107928?x=1", None),
        ("https://[invalid", None),
    ],
)
def test_persistent_identifier_validation(identifier, expected):
    assert rijksmuseum._persistent_integer(identifier) == expected


def test_structured_date_is_used_when_display_date_is_missing(monkeypatch):
    resources = _resource_set("1003")
    resources[("1003", "la-framed")]["produced_by"]["timespan"]["identified_by"] = []
    resources[("1003", "la-framed")]["produced_by"]["timespan"].update(
        {"begin_of_the_begin": "1600-01-01T00:00:00Z", "end_of_the_end": "1605-12-31T23:59:59Z"}
    )
    _install_router(monkeypatch, [_search_page("1003")], resources)

    candidate = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1)[0]

    assert candidate.creation_date == "1600\u20131605"


def test_missing_optional_metadata_uses_stable_defaults(monkeypatch):
    resources = _resource_set("1004", include_optional=False)
    _install_router(monkeypatch, [_search_page("1004")], resources)

    candidate = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1)[0]

    assert candidate.title == "English Title"
    assert candidate.artist_name == "Unknown Artist"
    assert candidate.creation_date == "Unknown Date"
    assert candidate.medium is None
    assert candidate.dimensions is None
    assert candidate.description is None


def test_missing_object_number_skips_candidate(monkeypatch):
    resources = _resource_set("1012")
    resources[("1012", "la-framed")]["identified_by"] = []
    _install_router(monkeypatch, [_search_page("1012")], resources)

    assert rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1) == []


@pytest.mark.parametrize(
    "missing_key", ["shows", "visual", "digital", "access", "malformed_access", "info"]
)
def test_missing_media_chain_element_skips_individual_candidate(monkeypatch, missing_key):
    resources = _resource_set("1005")
    if missing_key == "shows":
        resources[("1005", "la-framed")]["shows"] = []
    elif missing_key == "visual":
        resources[("21005", "la-framed")] = {"type": "VisualItem"}
    elif missing_key == "digital":
        resources[("31005", "la-framed")] = {"type": "DigitalObject"}
    elif missing_key == "access":
        resources[("31005", "la-framed")]["access_point"] = []
    elif missing_key == "malformed_access":
        resources[("31005", "la-framed")]["access_point"] = [
            {"id": "https://[invalid"}
        ]
    else:
        resources[("image1005", "info")]["width"] = "invalid"
    _install_router(monkeypatch, [_search_page("1005")], resources)

    assert rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1) == []


@pytest.mark.parametrize(
    "width,height,expected_suffix",
    [
        (6000, 4000, "/full/2560,/0/default.jpg"),
        (3000, 5000, "/full/,2560/0/default.jpg"),
        (1200, 800, "/full/1200,/0/default.jpg"),
    ],
)
def test_iiif_dimensions_orientation_and_bounded_url(monkeypatch, width, height, expected_suffix):
    resources = _resource_set("1006", width=width, height=height)
    _install_router(monkeypatch, [_search_page("1006")], resources)

    candidate = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1)[0]

    assert (candidate.image_width, candidate.image_height) == (width, height)
    assert candidate.image_url.endswith(expected_suffix)
    assert "/full/max/" not in candidate.image_url


@pytest.mark.parametrize(
    "rights,status",
    [
        ("http://creativecommons.org/publicdomain/mark/1.0/", "CONFIRMED_PUBLIC_DOMAIN"),
        ("https://creativecommons.org/publicdomain/mark/1.0", "CONFIRMED_PUBLIC_DOMAIN"),
        ("http://creativecommons.org/publicdomain/zero/1.0/", "CONFIRMED_OPEN_ACCESS"),
        ("https://creativecommons.org/publicdomain/zero/1.0", "CONFIRMED_OPEN_ACCESS"),
        ("https://creativecommons.org/licenses/by/4.0/", None),
        ("https://creativecommons.org:444/publicdomain/mark/1.0/", None),
        ("https://creativecommons.org/publicdomain/mark/1.0/?source=invalid", None),
        ("https://creativecommons.org/publicdomain/mark/1.0/#invalid", None),
        ("https://[invalid", None),
        ("Public Domain", None),
        (["https://creativecommons.org/publicdomain/mark/1.0/"], None),
        (None, None),
    ],
)
def test_rights_uri_mapping_preserves_existing_semantics(rights, status):
    assert rijksmuseum.get_rights_status(rights) == status


@pytest.mark.parametrize(
    "rights,status,is_public_domain",
    [
        ("https://creativecommons.org/publicdomain/mark/1.0/", "CONFIRMED_PUBLIC_DOMAIN", True),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CONFIRMED_OPEN_ACCESS", True),
        ("https://creativecommons.org/licenses/by/4.0/", "KNOWN_RESTRICTED", False),
        ("https://[invalid", "KNOWN_RESTRICTED", False),
        (None, None, False),
    ],
)
def test_edm_rights_are_preserved_on_candidate(monkeypatch, rights, status, is_public_domain):
    resources = _resource_set("1007", rights=rights)
    _install_router(monkeypatch, [_search_page("1007")], resources)

    candidate = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1)[0]

    assert candidate.rights_status == status
    assert candidate.is_public_domain is is_public_domain
    assert candidate.license == rights
    assert candidate.rights_text == rights
    assert candidate.copyright_notice == rights


def test_duplicate_object_numbers_are_returned_once(monkeypatch):
    resources = {}
    resources.update(_resource_set("1008", object_number="SK-DUP"))
    resources.update(_resource_set("1009", object_number="SK-DUP"))
    _install_router(monkeypatch, [_search_page("1008", "1009")], resources)

    candidates = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=2, rng=random.Random(4))

    assert [candidate.canonical_id for candidate in candidates] == ["rijksmuseum_SK-DUP"]


@pytest.mark.parametrize(
    "status,category,raises",
    [(429, "RATE_LIMITED", True), (500, "API_ERROR", False), (503, "API_ERROR", False)],
)
def test_search_http_failures_are_classified(monkeypatch, status, category, raises):
    monkeypatch.setattr(
        rijksmuseum.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code=status),
    )
    adapter = rijksmuseum.RijksmuseumAdapter()

    if raises:
        with pytest.raises(AdapterHTTPError) as error:
            adapter.fetch_candidates()
        assert error.value.category == category
    else:
        assert adapter.fetch_candidates() == []
    assert adapter.source_failure_category == category


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_search_network_failures_are_classified(monkeypatch, error):
    monkeypatch.setattr(rijksmuseum.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    adapter = rijksmuseum.RijksmuseumAdapter()

    assert adapter.fetch_candidates() == []
    assert adapter.source_failure_category == "NETWORK_ERROR"


@pytest.mark.parametrize(
    "failure,category,raises",
    [
        (FakeResponse(status_code=429), "RATE_LIMITED", True),
        (FakeResponse(status_code=503), "API_ERROR", False),
        (requests.Timeout(), "NETWORK_ERROR", False),
        (requests.ConnectionError(), "NETWORK_ERROR", False),
    ],
)
def test_detail_failures_are_classified(monkeypatch, failure, category, raises):
    resources = _resource_set("1013")
    resources[("1013", "la-framed")] = failure
    _install_router(monkeypatch, [_search_page("1013")], resources)
    adapter = rijksmuseum.RijksmuseumAdapter()

    if raises:
        with pytest.raises(AdapterHTTPError) as error:
            adapter.fetch_candidates(limit=1)
        assert error.value.category == category
    else:
        assert adapter.fetch_candidates(limit=1) == []
        assert adapter.source_failure_category == category


def test_bad_individual_artwork_does_not_hide_later_candidate(monkeypatch):
    resources = _resource_set("1011", object_number="SK-GOOD")
    resources[("1010", "la-framed")] = FakeResponse(status_code=404)
    _install_router(monkeypatch, [_search_page("1010", "1011")], resources)

    candidates = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=1, rng=random.Random(0))

    assert [candidate.source_id for candidate in candidates] == ["SK-GOOD"]
    assert candidates[0].canonical_id == "rijksmuseum_SK-GOOD"


def test_resolution_and_iiif_requests_are_bounded_by_requested_limit(monkeypatch):
    pids = [str(1100 + index) for index in range(100)]
    resources = {}
    for pid in pids:
        resources.update(_resource_set(pid, object_number=f"SK-{pid}"))
    calls = _install_router(monkeypatch, [_search_page(*pids)], resources)

    candidates = rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=3, rng=random.Random(12))

    assert len(candidates) == 3
    assert len(calls) == 1 + 5 * 3
    assert len(calls) < 20


def test_resolution_budget_caps_worst_case_detail_requests(monkeypatch):
    pids = [str(1300 + index) for index in range(100)]
    resources = {}
    for pid in pids:
        resources.update(_resource_set(pid))
        resources[(f"image{pid}", "info")]["protocol"] = "invalid"
    calls = _install_router(monkeypatch, [_search_page(*pids)], resources)

    assert rijksmuseum.RijksmuseumAdapter().fetch_candidates(limit=50, rng=random.Random(8)) == []
    assert len(calls) == 1 + rijksmuseum.MAX_DETAIL_REQUESTS
    assert len(calls) == 41


def test_seeded_identifier_sampling_is_reproducible(monkeypatch):
    pids = [str(1200 + index) for index in range(8)]
    resources = {}
    for pid in pids:
        resources.update(_resource_set(pid, object_number=f"SK-{pid}"))

    def run():
        _install_router(monkeypatch, [_search_page(*pids)], resources)
        return [
            candidate.source_id
            for candidate in rijksmuseum.RijksmuseumAdapter().fetch_candidates(
                limit=3, rng=random.Random(22)
            )
        ]

    assert run() == run()


def test_rijksmuseum_no_longer_depends_on_a_credential():
    assert rijksmuseum.RijksmuseumAdapter().unavailable_reason() is None
    assert "rijksmuseum" not in production_config.OPTIONAL_INTEGRATION_VARIABLES
    assert "rijksmuseum" not in reel_candidate_acquisition._REQUIRED_CREDENTIAL_ENV
