import pytest
from datetime import datetime, timedelta, timezone
from src.models import NormalizedArtwork, normalize_artwork_id
from src import history_tracker
from src.museums import (
    AICAdapter,
    ClevelandAdapter,
    EuropeanaAdapter,
    GettyAdapter,
    MetAdapter,
    RijksmuseumAdapter,
    SmithsonianAdapter,
)
from src.quality_filter import calculate_quality_score, validate_and_download_image
import os

def test_normalized_artwork():
    art = NormalizedArtwork(
        source="test",
        source_id="123",
        title="Test Painting",
        museum_name="Test Museum"
    )
    assert art.canonical_id == "test_123"
    assert art.artist_name == "Unknown Artist"


@pytest.mark.parametrize(
    ("stored_id", "candidate_id"),
    [
        ("artic_84774", "aic_84774"),
        ("aic_84774", "aic_84774"),
        ("cma_153393", "cleveland_153393"),
        ("cleveland_153393", "cleveland_153393"),
    ],
)
def test_legacy_and_canonical_ids_are_duplicate_equivalents(monkeypatch, stored_id, candidate_id):
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: ({"posted_artworks": [{"id": stored_id}]}, None),
    )

    assert candidate_id in history_tracker.get_posted_ids()


def test_different_artwork_ids_are_not_duplicates(monkeypatch):
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: ({"posted_artworks": [{"id": "artic_84774"}]}, None),
    )

    assert "aic_84775" not in history_tracker.get_posted_ids()


def test_reservation_writes_canonical_id_without_migrating_history(monkeypatch):
    history = {"posted_artworks": [{"id": "artic_84774", "status": "PUBLISHED"}]}
    uploaded = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda value, etag: uploaded.append((value, etag)))

    history_tracker.reserve_artwork({"id": "aic_84774", "title": "A", "artist": "B"})

    assert len(uploaded) == 1
    saved_history, etag = uploaded[0]
    assert etag == "etag"
    assert [item["id"] for item in saved_history["posted_artworks"]] == ["aic_84774"]


@pytest.mark.parametrize("artwork_id", ["artic_84774", "aic_84774", "met_123", "rijksmuseum_SK-A-1"])
def test_artwork_id_normalization_is_idempotent_and_preserves_other_formats(artwork_id):
    normalized = normalize_artwork_id(artwork_id)

    assert normalize_artwork_id(normalized) == normalized
    if artwork_id.startswith("artic_"):
        assert normalized == "aic_84774"
    else:
        assert normalized == artwork_id


def test_normalized_artwork_uses_canonical_source_prefix():
    artwork = NormalizedArtwork(source="artic", source_id="84774", museum_name="AIC")

    assert artwork.canonical_id == "aic_84774"


def test_active_and_published_reservations_are_protected_but_stale_pending_is_reusable(monkeypatch):
    now = datetime.now(timezone.utc)
    history = {
        "posted_artworks": [
            {"id": "aic_active", "status": "PENDING", "reserved_at": (now - history_tracker.PENDING_RESERVATION_TTL + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"id": "aic_stale", "status": "PENDING", "reserved_at": (now - history_tracker.PENDING_RESERVATION_TTL - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"id": "aic_published", "status": "PUBLISHED", "reserved_at": (now - history_tracker.PENDING_RESERVATION_TTL - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"id": "met_legacy", "title": "Legacy schema"},
            {"id": "aic_malformed", "status": "PENDING", "reserved_at": "not-a-timestamp"},
        ]
    }
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    posted_ids = history_tracker.get_posted_ids()

    assert "aic_active" in posted_ids
    assert "aic_stale" not in posted_ids
    assert "aic_published" in posted_ids
    assert "met_legacy" in posted_ids
    assert "aic_malformed" in posted_ids
    assert history["posted_artworks"][1]["status"] == "PENDING"


def test_recover_stale_reservations_is_auditable_and_idempotent(monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    history = {
        "posted_artworks": [
            {"id": "aic_stale", "status": "PENDING", "reserved_at": "2026-08-22T09:00:00Z"},
            {"id": "aic_active", "status": "PENDING", "reserved_at": "2026-08-22T11:00:00Z"},
            {"id": "aic_published", "status": "PUBLISHED", "reserved_at": "2026-08-22T08:00:00Z"},
            {"id": "aic_unknown", "status": "PENDING"},
            {"id": "aic_malformed", "status": "PENDING", "reserved_at": "not-a-timestamp"},
        ]
    }
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda value, etag: uploads.append((value, etag)))

    assert history_tracker.recover_stale_reservations(now) == 1
    assert history_tracker.recover_stale_reservations(now) == 0

    assert len(uploads) == 1
    assert history["posted_artworks"][0]["status"] == "EXPIRED"
    assert history["posted_artworks"][0]["expired_at"] == "2026-08-22T12:00:00Z"
    assert history["posted_artworks"][1]["status"] == "PENDING"
    assert history["posted_artworks"][2]["status"] == "PUBLISHED"
    assert history["posted_artworks"][3]["status"] == "PENDING"
    assert history["posted_artworks"][4]["status"] == "PENDING"


def test_publishing_reservations_are_permanent_duplicate_locks_and_expire_never(monkeypatch):
    history = {
        "posted_artworks": [
            {"id": "aic_publishing", "status": "PUBLISHING", "reserved_at": "2026-08-22T09:00:00Z"},
        ]
    }
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(history_tracker, "_upload_history", lambda value, etag: uploads.append((value, etag)))

    assert "aic_publishing" in history_tracker.get_posted_ids()
    assert history_tracker.recover_stale_reservations(
        datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    ) == 0
    assert history["posted_artworks"][0]["status"] == "PUBLISHING"
    assert uploads == []


def test_recovery_rejects_naive_clock(monkeypatch):
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: ({"posted_artworks": []}, None),
    )

    with pytest.raises(ValueError):
        history_tracker.recover_stale_reservations(datetime(2026, 8, 22, 12, 0))
    
def test_quality_filter_scoring():
    art = NormalizedArtwork(
        source="test",
        source_id="1",
        title="Valid Title",
        artist_name="Valid Artist",
        creation_date="1900",
        medium="Oil on canvas",
        museum_name="Test Museum",
        image_width=2000,
        image_height=2000
    )
    score = calculate_quality_score(art, museum_weights={"test": 15})
    # Core metadata (20), great image res (40), good source (15), perfect ratio (20)
    assert score == 100

    art_bad = NormalizedArtwork(
        source="test",
        source_id="2",
        title="Untitled",
        artist_name="Unknown",
        creation_date="Unknown",
        medium=None,
        museum_name="Test Museum",
        image_width=100,
        image_height=300
    )
    score_bad = calculate_quality_score(art_bad, museum_weights={"test": 15})
    # Bad core metadata (0), low res (15), good source (15), extreme ratio (10)
    assert score_bad == 40 / 95 * 100
    
def test_image_validator_invalid_url():
    assert validate_and_download_image("not a url", "test.jpg") == False
    assert validate_and_download_image("http://invalid.domain.that.does.not.exist.com/image.jpg", "test.jpg") == False
    if os.path.exists("test.jpg"):
        os.remove("test.jpg")

def test_adapters_instantiation():
    adapters = [
        AICAdapter(),
        MetAdapter(),
        ClevelandAdapter(),
        RijksmuseumAdapter(),
        SmithsonianAdapter(),
        GettyAdapter(),
        EuropeanaAdapter(),
    ]
    assert len(adapters) == 7
    for a in adapters:
        assert isinstance(a.source_id, str)
