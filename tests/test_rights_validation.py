from pathlib import Path

import pytest

from src import art_fetcher
from src.quality_filter import ImageValidationResult
from src.models import NormalizedArtwork
from src.museums import aic, cleveland, met, rijksmuseum


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_aic_requires_explicit_public_domain_flag(monkeypatch):
    payload = {
        "data": [
            {
                "id": 1,
                "title": "Safe",
                "artist_title": "Artist",
                "classification_title": "Painting",
                "image_id": "image-1",
                "is_public_domain": True,
                "copyright_notice": "Public domain",
                "credit_line": "AIC",
            },
            {"id": 2, "classification_title": "Painting", "image_id": "image-2", "is_public_domain": False},
            {"id": 3, "classification_title": "Painting", "image_id": "image-3"},
            {"id": 4, "classification_title": "Painting", "is_public_domain": True},
        ]
    }
    monkeypatch.setattr(aic.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    candidates = aic.AICAdapter().fetch_candidates()

    assert [candidate.canonical_id for candidate in candidates] == ["aic_1"]
    assert candidates[0].rights_status == "CONFIRMED_PUBLIC_DOMAIN"
    assert candidates[0].image_url == "https://www.artic.edu/iiif/2/image-1/full/1686,/0/default.jpg"


@pytest.mark.parametrize("is_public_domain", [True, False, None])
def test_met_requires_explicit_public_domain_flag(monkeypatch, is_public_domain):
    responses = iter(
        [
            FakeResponse({"objectIDs": [1]}),
            FakeResponse(
                {
                    "isPublicDomain": is_public_domain,
                    "title": "Safe",
                    "objectName": "Painting",
                    "primaryImage": "https://images.example/safe.jpg",
                }
            ),
        ]
    )
    monkeypatch.setattr(met.requests, "get", lambda *args, **kwargs: next(responses))

    candidates = met.MetAdapter().fetch_candidates(limit=1)

    assert len(candidates) == (1 if is_public_domain is True else 0)
    if candidates:
        assert candidates[0].rights_status == "CONFIRMED_PUBLIC_DOMAIN"


def test_cleveland_accepts_only_cc0_images(monkeypatch):
    payload = {
        "data": [
            {
                "id": 1,
                "title": "Safe",
                "type": "Painting",
                "share_license_status": "CC0",
                "images": {"web": {"url": "https://images.example/safe.jpg", "width": "956", "height": "893"}},
                "copyright": "CC0",
            },
            {
                "id": 2,
                "title": "Restricted",
                "type": "Painting",
                "share_license_status": "Copyrighted",
                "images": {"web": {"url": "https://images.example/restricted.jpg"}},
            },
            {
                "id": 3,
                "title": "Unknown",
                "type": "Painting",
                "images": {"web": {"url": "https://images.example/unknown.jpg"}},
            },
        ]
    }
    monkeypatch.setattr(cleveland.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    candidates = cleveland.ClevelandAdapter().fetch_candidates()

    assert [candidate.canonical_id for candidate in candidates] == ["cleveland_1"]
    assert candidates[0].rights_status == "CONFIRMED_OPEN_ACCESS"
    assert (candidates[0].image_width, candidates[0].image_height) == (956, 893)


@pytest.mark.parametrize("width,height", [(None, "893"), ("unknown", "893"), ("0", "893"), ("956", "-1")])
def test_cleveland_rejects_invalid_image_dimensions(monkeypatch, width, height):
    payload = {
        "data": [
            {
                "id": 1,
                "title": "Safe",
                "type": "Painting",
                "share_license_status": "CC0",
                "images": {"web": {"url": "https://images.example/safe.jpg", "width": width, "height": height}},
            }
        ]
    }
    monkeypatch.setattr(cleveland.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    candidate = cleveland.ClevelandAdapter().fetch_candidates()[0]

    assert (candidate.image_width, candidate.image_height) == (None, None)


def test_rijksmuseum_requires_explicit_public_domain_or_cc0(monkeypatch):
    payload = {
        "artObjects": [
            {
                "objectNumber": "SK-A-1",
                "title": "Safe",
                "webImage": {"url": "https://images.example/safe.jpg"},
                "copyrightHolder": "Public Domain",
            },
            {
                "objectNumber": "SK-A-2",
                "title": "Ambiguous",
                "webImage": {"url": "https://images.example/ambiguous.jpg"},
                "copyrightHolder": "Rijksmuseum",
            },
            {
                "objectNumber": "SK-A-3",
                "title": "Missing",
                "webImage": {"url": "https://images.example/missing.jpg"},
            },
        ]
    }
    monkeypatch.setattr(rijksmuseum.requests, "get", lambda *args, **kwargs: FakeResponse(payload))
    monkeypatch.setenv("RIJKSMUSEUM_API_KEY", "test-key")

    candidates = rijksmuseum.RijksmuseumAdapter().fetch_candidates()

    assert [candidate.canonical_id for candidate in candidates] == ["rijksmuseum_SK-A-1"]
    assert candidates[0].rights_status == "CONFIRMED_PUBLIC_DOMAIN"


def test_selection_filters_unconfirmed_rights_before_scoring_or_download(monkeypatch, tmp_path):
    restricted = NormalizedArtwork(
        source="aic",
        source_id="restricted",
        museum_name="AIC",
        image_url="https://images.example/restricted.jpg",
        is_public_domain=True,
    )
    safe = NormalizedArtwork(
        source="aic",
        source_id="safe",
        museum_name="AIC",
        image_url="https://images.example/safe.jpg",
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )
    safe_second = NormalizedArtwork(
        source="met",
        source_id="safe-second",
        museum_name="Met",
        image_url="https://images.example/safe-second.jpg",
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )

    class StaticAdapter:
        source_id = "test"

        def __init__(self, candidates):
            self.candidates = candidates

        def fetch_candidates(self, **kwargs):
            return self.candidates

    adapters = iter(
        [
            StaticAdapter([restricted, safe, safe_second]),
            StaticAdapter([]),
            StaticAdapter([]),
            StaticAdapter([]),
        ]
    )
    monkeypatch.setattr(art_fetcher, "AICAdapter", lambda: next(adapters))
    monkeypatch.setattr(art_fetcher, "ClevelandAdapter", lambda: next(adapters))
    monkeypatch.setattr(art_fetcher, "MetAdapter", lambda: next(adapters))
    monkeypatch.setattr(art_fetcher, "RijksmuseumAdapter", lambda: next(adapters))

    scored_ids = []
    downloaded_urls = []
    monkeypatch.setattr(
        art_fetcher,
        "calculate_quality_score",
        lambda candidate, weights: scored_ids.append(candidate.canonical_id) or 100,
    )

    def download(url, path):
        downloaded_urls.append(url)
        Path(path).write_bytes(b"validated image")
        return ImageValidationResult(True, width=2000, height=1600, image_format="JPEG", reason="ok")

    monkeypatch.setattr(
        art_fetcher,
        "validate_and_download_image_with_metadata",
        download,
    )
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))

    artworks = art_fetcher.fetch_themed_artworks(set(), "portrait", count=2, color_tone="warm")

    assert [artwork["id"] for artwork in artworks] == ["aic_safe", "met_safe-second"]
    assert scored_ids == ["aic_safe", "met_safe-second", "aic_safe", "met_safe-second"]
    assert downloaded_urls == ["https://images.example/safe.jpg", "https://images.example/safe-second.jpg"]
