"""Single-artwork reservations share the feed's single-use production fence."""

from datetime import timedelta

import pytest

from src import feed_schedule, history_tracker, publication_state
from src.production_authorization import ProductionAuthorizationError
from src.production_config import ProductionConfigurationError
from tests.test_feed_schedule import NOW, auth, setup_runtime


def artwork(identifier="cleveland_990001"):
    return {"id": identifier, "title": "X", "artist": "Y", "museum": "Museum"}


def test_scheduled_single_reservation_consumes_one_permit_and_finalizes(monkeypatch):
    store, client = setup_runtime(monkeypatch)
    before = client.puts

    publication_id = history_tracker.reserve_single_publication(
        artwork(), authorization=auth()
    )

    state = store.load_safety()[0]
    records = state.active_publication_state.posted_artworks
    assert len(records) == 1
    assert records[0]["publication_id"] == publication_id
    assert records[0]["publication_type"] == "single"
    assert records[0]["content_type"] == "SINGLE_ARTWORK"
    assert state.feed_schedule_control.permits[-1].status == "RESERVED"
    assert state.feed_schedule_control.permits[-1].publication_id == publication_id
    assert state.active_publication_state.consumed_authorizations[0]["key"] == "schedule:123"
    assert client.puts == before + 1

    history_tracker.start_publication_attempt(
        [records[0]["id"]], "parent", expected_publication_id=publication_id,
        authorization=auth(),
    )
    history_tracker.confirm_artworks_and_record_publication(
        [records[0]["id"]], "media-1", "single", publication_id=publication_id
    )

    receipt_ledger, _ = store.load_receipts()
    receipt = receipt_ledger.records[-1]
    assert receipt.publication_type == "single"
    assert len(receipt.artwork_positions) == 1
    state = store.load_safety()[0]
    receipts, _ = store.load_receipts()
    with pytest.raises(feed_schedule.FeedScheduleError, match="COOLDOWN"):
        feed_schedule.require_cooldown(state, receipts, NOW + timedelta(hours=1))


def test_single_reservation_rejects_missing_or_consumed_authorization_without_write(monkeypatch):
    store, client = setup_runtime(monkeypatch)
    before = client.puts

    with pytest.raises(
        ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_INVALID"
    ):
        history_tracker.reserve_single_publication(artwork())
    history_tracker.reserve_single_publication(artwork(), authorization=auth())
    before = client.puts
    with pytest.raises(
        ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_ALREADY_CONSUMED"
    ):
        history_tracker.reserve_single_publication(artwork("aic_990002"), authorization=auth())
    assert client.puts == before


def test_single_reservation_rejects_protected_artwork_without_write(monkeypatch):
    store, client = setup_runtime(monkeypatch)
    before = client.puts
    protected_id = next(iter(store.load_safety()[0].published_artwork_protection.entries))

    with pytest.raises(RuntimeError, match="protected artwork"):
        history_tracker.reserve_single_publication(artwork(protected_id), authorization=auth())
    assert client.puts == before


def test_single_reservation_rejects_dirty_production_state_without_write(monkeypatch):
    store, client = setup_runtime(monkeypatch)
    state, etag = store.load_safety()
    raw = state.model_dump(mode="json")
    raw["active_publication_state"]["staging_media_cleanup_queue"] = [{
        "publication_id": "66a4cac3-3db7-419d-bb05-74bd16a68802",
        "eligible_at": NOW.isoformat().replace("+00:00", "Z"),
        "reason": "test",
    }]
    raw["generation"] += 1
    store.update_safety(publication_state.seal(raw), etag)
    before = client.puts

    with pytest.raises(ProductionConfigurationError, match="cleanup_queue_unresolved"):
        history_tracker.reserve_single_publication(artwork(), authorization=auth())
    assert client.puts == before
