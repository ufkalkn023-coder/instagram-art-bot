import random

from src.museums import europeana, smithsonian


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_smithsonian_requires_key_and_preserves_image_rights(monkeypatch):
    monkeypatch.delenv("SMITHSONIAN_API_KEY", raising=False)
    monkeypatch.setattr(smithsonian.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("request")))
    assert smithsonian.SmithsonianAdapter().fetch_candidates() == []

    payload = {
        "response": {
            "rows": [
                {
                    "id": "edanmdm-safe-1",
                    "title": "Safe",
                    "unitCode": "SAAM",
                    "content": {
                        "descriptiveNonRepeating": {
                            "record_link": "https://example.test/object/1",
                            "online_media": {
                                "media": [
                                    {
                                        "type": "Images",
                                        "usage": {"access": "CC0"},
                                        "resources": [{"url": "https://images.example/safe.jpg", "width": 2000, "height": 1500}],
                                    }
                                ]
                            },
                        },
                        "indexedStructured": {"name": ["Artist"], "date": ["1900"], "object_type": ["Painting"]},
                    },
                },
                {
                    "id": "edanmdm-restricted",
                    "content": {"descriptiveNonRepeating": {"online_media": {"media": [{"type": "Images", "usage": {"access": "Usage Conditions Apply"}, "resources": [{"url": "https://images.example/no.jpg"}]}]}}, "indexedStructured": {}},
                },
            ]
        }
    }
    monkeypatch.setenv("SMITHSONIAN_API_KEY", "test-key")
    monkeypatch.setattr(smithsonian.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    candidates = smithsonian.SmithsonianAdapter().fetch_candidates(limit=2, rng=random.Random(1))

    assert [candidate.canonical_id for candidate in candidates] == [
        "smithsonian_edanmdm-safe-1",
        "smithsonian_edanmdm-restricted",
    ]
    assert candidates[0].rights_status == "CONFIRMED_OPEN_ACCESS"
    assert candidates[1].rights_status == "KNOWN_RESTRICTED"
    assert (candidates[0].image_width, candidates[0].image_height) == (2000, 1500)


def test_europeana_preserves_matching_webresource_rights_without_gating(monkeypatch):
    monkeypatch.setenv("EUROPEANA_API_KEY", "test-key")
    search = {"items": [{"id": "/123/safe"}, {"id": "/123/restricted"}]}
    records = {
        "safe": {
            "object": {
                "title": ["Safe"],
                "dcCreator": ["Artist"],
                "year": ["1900"],
                "dataProvider": ["Provider Museum"],
                "aggregations": [{"edmIsShownBy": "https://images.example/safe.jpg", "webResources": [{"about": "https://images.example/safe.jpg", "edmRights": "http://creativecommons.org/publicdomain/zero/1.0/", "ebucoreWidth": "2200", "ebucoreHeight": "1600"}]}],
            }
        },
        "restricted": {
            "object": {
                "aggregations": [{"edmIsShownBy": "https://images.example/restricted.jpg", "edmRights": "http://creativecommons.org/publicdomain/zero/1.0/", "webResources": [{"about": "https://images.example/restricted.jpg", "edmRights": "http://rightsstatements.org/vocab/InC/1.0/"}]}]
            }
        },
    }

    def request(url, **kwargs):
        if url.endswith("search.json"):
            return FakeResponse(search)
        return FakeResponse(records[url.rsplit("/", 1)[-1].removesuffix(".json")])

    monkeypatch.setattr(europeana.requests, "get", request)
    candidates = europeana.EuropeanaAdapter().fetch_candidates(limit=2, rng=random.Random(1))

    assert [candidate.canonical_id for candidate in candidates] == [
        "europeana_123%2Fsafe",
        "europeana_123%2Frestricted",
    ]
    assert candidates[0].museum_name == "Provider Museum"
    assert candidates[0].rights_status == "CONFIRMED_OPEN_ACCESS"
    assert candidates[1].rights_status == "KNOWN_RESTRICTED"
    assert candidates[1].rights_text == "http://rightsstatements.org/vocab/InC/1.0/"

def test_europeana_skips_without_an_api_key(monkeypatch):
    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)
    monkeypatch.setattr(europeana.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("request")))

    assert europeana.EuropeanaAdapter().fetch_candidates() == []


def test_europeana_prefers_an_open_resource_over_an_earlier_restricted_fallback():
    record = {
        "aggregations": [
            {
                "edmIsShownBy": "https://images.example/restricted.jpg",
                "webResources": [
                    {
                        "about": "https://images.example/restricted.jpg",
                        "edmRights": "http://rightsstatements.org/vocab/InC/1.0/",
                    }
                ],
            },
            {
                "edmIsShownBy": "https://images.example/open.jpg",
                "webResources": [
                    {
                        "about": "https://images.example/open.jpg",
                        "edmRights": "http://creativecommons.org/publicdomain/zero/1.0/",
                    }
                ],
            },
        ]
    }

    image_url, _, rights_status, _ = europeana._image_resource(record)

    assert image_url == "https://images.example/open.jpg"
    assert rights_status == "CONFIRMED_OPEN_ACCESS"
