from pathlib import Path

import pytest

from src import art_fetcher
from src.quality_filter import ImageValidationResult
from src.models import NormalizedArtwork
from src.museums import aic, cleveland, met
from src.rights_policy import (
    RightsPolicyMode,
    is_rights_eligible,
    resolve_rights_policy,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_aic_preserves_rights_and_uses_public_domain_derivative_only_when_allowed(monkeypatch):
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

    assert [candidate.canonical_id for candidate in candidates] == [
        "aic_1",
        "aic_2",
        "aic_3",
    ]
    assert candidates[0].rights_status == "CONFIRMED_PUBLIC_DOMAIN"
    assert candidates[0].image_url == "https://www.artic.edu/iiif/2/image-1/full/1686,/0/default.jpg"
    assert candidates[0].copyright_notice == "Public domain"
    assert candidates[0].credit_line == "AIC"
    assert candidates[1].rights_status == "KNOWN_RESTRICTED"
    assert candidates[1].image_url == "https://www.artic.edu/iiif/2/image-2/full/843,/0/default.jpg"
    assert candidates[2].image_url == "https://www.artic.edu/iiif/2/image-3/full/843,/0/default.jpg"
    assert candidates[2].rights_status is None


@pytest.mark.parametrize("is_public_domain", [True, False, None])
def test_met_acquisition_is_not_gated_by_public_domain_flag(monkeypatch, is_public_domain):
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

    assert len(candidates) == 1
    expected_status = (
        "CONFIRMED_PUBLIC_DOMAIN"
        if is_public_domain is True
        else "KNOWN_RESTRICTED"
        if is_public_domain is False
        else None
    )
    assert candidates[0].rights_status == expected_status


def test_cleveland_preserves_open_restricted_and_unknown_rights(monkeypatch):
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

    assert [candidate.canonical_id for candidate in candidates] == [
        "cleveland_1",
        "cleveland_2",
        "cleveland_3",
    ]
    assert candidates[0].rights_status == "CONFIRMED_OPEN_ACCESS"
    assert candidates[1].rights_status == "KNOWN_RESTRICTED"
    assert candidates[2].rights_status is None
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


def test_non_production_rights_policy_remains_permissive_by_default_and_reversible():
    confirmed = NormalizedArtwork(
        source="aic",
        source_id="confirmed",
        museum_name="AIC",
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )
    restricted = confirmed.model_copy(
        update={"source_id": "restricted", "is_public_domain": False, "rights_status": "KNOWN_RESTRICTED"}
    )
    unknown = confirmed.model_copy(
        update={"source_id": "unknown", "rights_status": "UNKNOWN"}
    )
    ambiguous = confirmed.model_copy(
        update={"source_id": "ambiguous", "rights_status": "AMBIGUOUS"}
    )
    missing = confirmed.model_copy(
        update={"source_id": "missing", "rights_status": None}
    )
    open_access = confirmed.model_copy(
        update={"source_id": "open", "rights_status": "CONFIRMED_OPEN_ACCESS"}
    )

    assert resolve_rights_policy({}) is RightsPolicyMode.PERMISSIVE
    assert all(
        is_rights_eligible(artwork, RightsPolicyMode.PERMISSIVE)
        for artwork in (confirmed, open_access, restricted, unknown, ambiguous, missing)
    )
    assert is_rights_eligible(confirmed, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)
    assert is_rights_eligible(open_access, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)
    assert not is_rights_eligible(restricted, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)
    assert not is_rights_eligible(unknown, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)
    assert not is_rights_eligible(ambiguous, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)
    assert not is_rights_eligible(missing, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)


def test_strict_policy_requires_confirmed_status_even_when_public_domain_flag_is_true():
    artwork = NormalizedArtwork(
        source="museum",
        source_id="candidate",
        museum_name="Museum",
        is_public_domain=True,
        rights_status="KNOWN_RESTRICTED",
    )

    assert not is_rights_eligible(artwork, RightsPolicyMode.STRICT_PUBLIC_DOMAIN)
    assert not is_rights_eligible(
        artwork.model_copy(update={"rights_status": None}),
        RightsPolicyMode.STRICT_PUBLIC_DOMAIN,
    )


def test_strict_selection_filters_unconfirmed_rights_before_scoring_or_download(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTFOLIO_RIGHTS_POLICY", "strict_public_domain")
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

    monkeypatch.setattr(
        art_fetcher,
        "_museum_adapters",
        lambda: [StaticAdapter([restricted, safe, safe_second])],
    )

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
