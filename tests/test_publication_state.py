"""Safety-state invariants independent of documentary receipt completeness."""

import copy
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError

from src import history_tracker, publication_state
from src.models import PublicationReceipt, normalize_artwork_id
from src.models import REEL_RELEASE_FILE_HASH_KEYS, ReelReleaseIdentity


FIXTURE = json.loads((Path(__file__).parent / "fixtures/recovered_ids.json").read_text())


def safety_candidate():
    proven = FIXTURE["A_PROVEN_PUBLISHED_ARTWORK_IDS"]
    supported = FIXTURE["B_STRONGLY_SUPPORTED_PUBLISHED_ARTWORK_IDS"]
    unverified = FIXTURE["C_LEGACY_UNVERIFIED_CANDIDATES"]
    entries = {
        artwork_id: {
            "canonical_artwork_id": artwork_id,
            "classification": "PROVEN" if artwork_id in proven else "STRONGLY_SUPPORTED",
            "provenance_refs": ["test-fixture:verified-recovery-manifest"],
            "first_known_publication_reference": None,
            "historical_reuse_publication_ids": [], "origin": "RECOVERED",
        }
        for artwork_id in proven + supported
    }
    protection = publication_state.seal({
        "schema_version": 1, "import_batch_id": "test-recovery",
        "source_artifact": "tests/fixtures/recovered_ids.json",
        "source_sha256": "a" * 64,
        "entry_count": len(entries), "entries": entries,
    })
    return publication_state.seal({
        "schema_version": 2, "state_epoch": "test-recovery", "generation": 1,
        "published_artwork_protection": protection,
        "recovery_quarantine": {
            "schema_version": 1,
            "candidate_artwork_ids": [
                {"canonical_artwork_id": item, "classification": "UNVERIFIED",
                 "evidence_ref": "test-fixture:unverified"}
                for item in unverified
            ],
            "inferred_catalog_candidates": [],
            "unresolved_historic_position_count": 0,
            "unresolved_positions": [],
            "blocked_sources": [],
        },
        "active_publication_state": {
            "schema_version": 1, "posted_artworks": [],
            "reel_reservations": [], "reel_publications": [],
            "reel_publication_count": 0, "staging_media_cleanup_queue": [],
            "reel_staging_cleanup_queue": [], "receipt_sync_pending": [],
        },
        "operational_projection": {
            "publications": [], "grid_publication_count": 0,
            "grid_counter_epoch": "test-recovery", "active_color_tone": "warm",
        },
    })


class FakeS3:
    def __init__(self, safety, receipts=None):
        self.objects = {publication_state.SAFETY_KEY: safety}
        if receipts is not None:
            self.objects[publication_state.RECEIPTS_KEY] = receipts
        self.etags = {key: 1 for key in self.objects}
        self.puts = 0
        self.uncertain = False

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        return {"Body": io.BytesIO(publication_state.canonical_bytes(self.objects[Key])),
                "ETag": f'"{self.etags[Key]}"'}

    def put_object(self, *, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **kwargs):
        self.puts += 1
        if self.uncertain:
            raise EndpointConnectionError(endpoint_url="https://fake.example")
        if IfNoneMatch == "*" and Key in self.objects or (
            IfMatch is not None and IfMatch != f'"{self.etags.get(Key, 0)}"'
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.objects[Key] = json.loads(Body)
        self.etags[Key] = self.etags.get(Key, 0) + 1


_DEFAULT_RECEIPTS = object()


def store_for(safety, receipts=_DEFAULT_RECEIPTS):
    if receipts is _DEFAULT_RECEIPTS:
        receipts = publication_state.seal({
            "schema_version": 2, "generation": 1, "source_artifact": "test",
            "source_sha256": "a" * 64, "record_count": 0, "records": [],
        })
    client = FakeS3(safety, receipts)
    config = publication_state.StateConfiguration("account", "state", "state-key", "secret")
    return publication_state.PublicationStateStore(config, client), client


def test_all_recovered_protected_ids_and_quarantine_block_selection():
    safety = publication_state.validate_safety_state(safety_candidate())
    history = publication_state.history_view(safety)
    blocked = history_tracker.globally_protected_artwork_ids(
        history, now=history_tracker.datetime.now(history_tracker.timezone.utc)
    )
    assert len(safety.published_artwork_protection.entries) == 634
    assert all(item in blocked for item in FIXTURE["A_PROVEN_PUBLISHED_ARTWORK_IDS"])
    assert all(item in blocked for item in FIXTURE["B_STRONGLY_SUPPORTED_PUBLISHED_ARTWORK_IDS"])
    assert all(item in blocked for item in FIXTURE["C_LEGACY_UNVERIFIED_CANDIDATES"])
    assert len(safety.recovery_quarantine.candidate_artwork_ids) == 18
    assert normalize_artwork_id("artic_84774") == "aic_84774"
    assert normalize_artwork_id("cma_153927") == "cleveland_153927"
    assert "artic_84774" in history_tracker.ProtectedArtworkIds(blocked, ())
    assert "cma_153927" in history_tracker.ProtectedArtworkIds(blocked, ())


def test_unresolved_positions_keep_source_embargo_at_selection_and_reservation(monkeypatch):
    candidate = safety_candidate()
    candidate["recovery_quarantine"]["unresolved_historic_position_count"] = 1
    candidate["recovery_quarantine"]["unresolved_positions"] = [{
        "publication_id": "historic-1", "position": 1,
        "instagram_child_media_id": "child-1", "caption_label": "Unknown work",
        "evidence_ref": "forensic:1",
    }]
    candidate["recovery_quarantine"]["blocked_sources"] = ["met", "smithsonian"]
    store, client = store_for(publication_state.seal(candidate))
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    selection = history_tracker.get_posted_ids()
    assert "met_999999" in selection
    assert "smithsonian_ld1-unknown" in selection
    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_artworks(
            [{"id": "met_999999", "title": "X", "artist": "Y", "museum": "Met"}],
            "single", "new-feed",
        )
    assert client.puts == 0


def test_missing_malformed_and_forward_schema_fail_closed():
    state = safety_candidate()
    store, client = store_for(state, receipts=None)
    with pytest.raises(publication_state.StateValidationError):
        store.load_receipts()
    bad = copy.deepcopy(state)
    bad["schema_version"] = 3
    bad = publication_state.seal(bad)
    with pytest.raises(publication_state.StateValidationError):
        publication_state.validate_safety_state(bad)
    bad = copy.deepcopy(state)
    bad["unexpected"] = True
    bad = publication_state.seal(bad)
    with pytest.raises(publication_state.StateValidationError):
        publication_state.validate_safety_state(bad)
    bad = copy.deepcopy(state)
    bad["generation"] = 2
    with pytest.raises(publication_state.StateValidationError):
        publication_state.validate_safety_state(bad)
    del client.objects[publication_state.SAFETY_KEY]
    with pytest.raises(publication_state.StateValidationError):
        store.load_safety()


def test_structurally_valid_test_state_is_not_accepted_as_production_recovery():
    state = publication_state.validate_safety_state(safety_candidate())
    with pytest.raises(publication_state.StateValidationError, match="unapproved recovery epoch"):
        publication_state.require_recovery_safety_baseline(state)


def test_state_configuration_never_falls_back_to_media_credentials():
    environment = {
        "CLOUDFLARE_R2_ACCOUNT_ID": "account",
        "CLOUDFLARE_R2_BUCKET_NAME": "media",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "media-key",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "media-secret",
        "CLOUDFLARE_STATE_R2_BUCKET_NAME": "state",
        "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID": "state-key",
        "CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY": "state-secret",
    }
    assert publication_state.StateConfiguration.from_environment(environment).access_key == "state-key"
    for missing in ("CLOUDFLARE_STATE_R2_BUCKET_NAME",
                    "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID",
                    "CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY"):
        with pytest.raises(publication_state.StateValidationError, match="incomplete"):
            publication_state.StateConfiguration.from_environment({**environment, missing: ""})
    with pytest.raises(publication_state.StateValidationError, match="differ from media bucket"):
        publication_state.StateConfiguration.from_environment({
            **environment, "CLOUDFLARE_STATE_R2_BUCKET_NAME": "media",
        })
    with pytest.raises(publication_state.StateValidationError, match="credentials must be distinct"):
        publication_state.StateConfiguration.from_environment({
            **environment, "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID": "media-key",
        })


def test_malformed_lifecycle_response_fails_closed():
    class Client:
        def get_bucket_lifecycle_configuration(self, **_kwargs):
            return {"Unexpected": "missing rules"}

    store = publication_state.PublicationStateStore(
        publication_state.StateConfiguration("account", "state", "key", "secret"),
        Client(),
    )
    with pytest.raises(publication_state.StateValidationError, match="malformed"):
        publication_state.validate_state_bucket_lifecycle(store)


def test_recovered_receipt_cannot_enter_live_pending_state():
    state = safety_candidate()
    state["active_publication_state"]["posted_artworks"] = [{
        "id": "aic_999997", "publication_id": "new-feed-3",
        "status": "PENDING", "reserved_at": "2026-09-23T12:00:00Z",
        "media_id": "historical-media-id",
    }]
    with pytest.raises(publication_state.StateValidationError, match="Pending live artwork"):
        publication_state.validate_safety_state(publication_state.seal(state))


def test_cas_conflict_and_uncertain_write_preserve_old_protection():
    state = safety_candidate()
    store, client = store_for(state)
    candidate = copy.deepcopy(state)
    candidate["generation"] = 2
    candidate = publication_state.seal(candidate)
    with pytest.raises(publication_state.StateConflictError):
        store.update_safety(candidate, '"stale"')
    assert client.puts == 0
    client.uncertain = True
    with pytest.raises(publication_state.StateWriteUncertainError):
        store.update_safety(candidate, '"1"')
    assert store.load_safety()[0].generation == 1
    assert len(store.load_safety()[0].published_artwork_protection.entries) == 634


def test_v2_cas_refuses_silent_live_lock_removal(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    history_tracker.reserve_artworks(
        [{"id": "aic_999991", "title": "Test", "artist": "Artist", "museum": "Museum"}],
        "single", "live-lock-1",
    )
    state, etag = store.load_safety()
    candidate = state.model_dump(mode="json")
    candidate["active_publication_state"]["posted_artworks"] = []
    candidate["generation"] += 1
    with pytest.raises(publication_state.StateValidationError, match="cannot be removed"):
        store.update_safety(publication_state.seal(candidate), etag)
    assert client.puts == 1
    assert store.load_safety()[0].active_publication_state.posted_artworks[0]["status"] == "PENDING"


def test_stale_v2_reservation_needs_explicit_expiry_and_stale_v2_worker_is_fenced(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork = {"id": "aic_999990", "title": "Test", "artist": "Artist", "museum": "Museum"}
    history_tracker.reserve_artworks([artwork], "single", "old-worker")
    state = client.objects[publication_state.SAFETY_KEY]
    state["active_publication_state"]["posted_artworks"][0]["reserved_at"] = "2020-01-01T00:00:00Z"
    client.objects[publication_state.SAFETY_KEY] = publication_state.seal(state)
    assert artwork["id"] in history_tracker.get_posted_ids()
    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_artworks([artwork], "single", "new-worker")
    assert history_tracker.recover_stale_reservations() == 1
    history_tracker.reserve_artworks([artwork], "single", "new-worker")
    with pytest.raises(RuntimeError, match="replaced"):
        history_tracker.start_publication_attempt(
            [artwork["id"]], "old-container", expected_publication_id="old-worker"
        )
    with pytest.raises(RuntimeError, match="replaced"):
        history_tracker.mark_publication_not_published(
            [artwork["id"]], "old-failure", authoritative=True,
            expected_publication_id="old-worker",
        )
    assert store.load_safety()[0].active_publication_state.posted_artworks[0]["publication_id"] == "new-worker"


def test_v2_publish_response_cannot_replace_durable_media_id(monkeypatch):
    store, _ = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork = {"id": "aic_999989", "title": "Test", "artist": "Artist", "museum": "Museum"}
    history_tracker.reserve_artworks([artwork], "single", "feed-identity")
    history_tracker.start_publication_attempt(
        [artwork["id"]], "container", expected_publication_id="feed-identity"
    )
    history_tracker.record_publish_response(
        [artwork["id"]], "media-1", expected_publication_id="feed-identity"
    )
    with pytest.raises(RuntimeError, match="conflicting media receipt"):
        history_tracker.record_publish_response(
            [artwork["id"]], "media-2", expected_publication_id="feed-identity"
        )
    with pytest.raises(history_tracker.CorruptedHistoryError, match="conflicts"):
        history_tracker.confirm_artworks_and_record_publication(
            [artwork["id"]], "media-2", "single", publication_id="feed-identity"
        )
    with pytest.raises(publication_state.StateValidationError, match="cannot expire"):
        history_tracker.mark_publication_not_published(
            [artwork["id"]], "mistaken_rejection", authoritative=True,
            expected_publication_id="feed-identity",
        )
    assert store.load_safety()[0].active_publication_state.posted_artworks[0]["publish_response_media_id"] == "media-1"


def test_v2_ambiguous_feed_lock_cannot_be_released_by_expiry(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork = {"id": "aic_999988", "title": "Test", "artist": "Artist", "museum": "Museum"}
    history_tracker.reserve_artworks([artwork], "single", "ambiguous-feed")
    history_tracker.start_publication_attempt(
        [artwork["id"]], "container", expected_publication_id="ambiguous-feed"
    )
    history_tracker.mark_artworks_ambiguous(
        [artwork["id"]], expected_publication_id="ambiguous-feed"
    )
    puts_before = client.puts
    with pytest.raises(publication_state.StateValidationError, match="cannot move backward"):
        history_tracker.mark_publication_not_published(
            [artwork["id"]], "late_container_error", authoritative=True,
            expected_publication_id="ambiguous-feed",
        )
    assert client.puts == puts_before
    assert store.load_safety()[0].active_publication_state.posted_artworks[0]["status"] == "AMBIGUOUS"


def test_successful_put_with_failed_readback_is_reported_uncertain():
    class ReadbackFailure(FakeS3):
        reads = 0

        def get_object(self, *, Bucket, Key):
            self.reads += 1
            if self.reads == 2:
                raise ClientError({"Error": {"Code": "ServiceUnavailable"},
                                   "ResponseMetadata": {"HTTPStatusCode": 503}}, "GetObject")
            return super().get_object(Bucket=Bucket, Key=Key)

    client = ReadbackFailure(safety_candidate())
    config = publication_state.StateConfiguration("account", "state", "state-key", "secret")
    store = publication_state.PublicationStateStore(config, client)
    candidate = safety_candidate()
    candidate["generation"] = 2
    candidate = publication_state.seal(candidate)
    with pytest.raises(publication_state.StateWriteUncertainError, match="read-after-write"):
        store.update_safety(candidate, '"1"')
    assert client.objects[publication_state.SAFETY_KEY]["generation"] == 2


def test_lost_successful_cas_response_is_uncertain_and_not_retried():
    class LostResponse(FakeS3):
        def put_object(self, **kwargs):
            super().put_object(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://fake.example")

    client = LostResponse(safety_candidate())
    store = publication_state.PublicationStateStore(
        publication_state.StateConfiguration("account", "state", "state-key", "secret"),
        client,
    )
    candidate = safety_candidate()
    candidate["generation"] = 2
    candidate = publication_state.seal(candidate)
    with pytest.raises(publication_state.StateWriteUncertainError):
        store.update_safety(candidate, '"1"')
    assert client.puts == 1
    assert store.load_safety()[0].generation == 2


def test_protection_and_quarantine_cannot_shrink_under_cas():
    state = safety_candidate()
    store, client = store_for(state)
    changed = copy.deepcopy(state)
    key = next(iter(changed["published_artwork_protection"]["entries"]))
    del changed["published_artwork_protection"]["entries"][key]
    changed["published_artwork_protection"]["entry_count"] -= 1
    changed["published_artwork_protection"] = publication_state.seal(
        changed["published_artwork_protection"]
    )
    changed["generation"] += 1
    changed = publication_state.seal(changed)
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(changed, '"1"')
    assert client.puts == 0


def test_incomplete_historical_receipt_is_valid_but_new_receipt_is_strict():
    receipt = {
        "publication_id": "historical-1", "instagram_media_id": "media-1",
        "publication_type": "carousel", "historical_state": "PUBLISHED_CONFIRMED",
        "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
        "identity_completeness": "INCOMPLETE", "occurred_at": None,
        "permalink": None, "workflow_run_id": None,
        "artwork_positions": [
            {"position": i, "canonical_artwork_id": None,
             "instagram_child_media_id": str(i), "caption_label": f"label-{i}"}
            for i in range(1, 9)
        ], "evidence_ref": "historical-artifact:1",
    }
    assert PublicationReceipt.model_validate(receipt).identity_completeness == "INCOMPLETE"
    receipt["record_origin"] = "NEW"
    with pytest.raises(ValidationError):
        PublicationReceipt.model_validate(receipt)


def test_media_cleanup_rejects_durable_state_key():
    from src import r2_media
    with pytest.raises(ValueError):
        r2_media.validate_owned_object_key(publication_state.SAFETY_KEY, "publication-1")
    with pytest.raises(ValueError):
        r2_media.validate_owned_reel_object_key(publication_state.RECEIPTS_KEY, "publication-1")


def test_new_feed_transaction_adds_protection_and_strict_receipt(monkeypatch):
    ledger = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "test",
        "source_sha256": "a" * 64, "record_count": 0, "records": [],
    })
    store, client = store_for(safety_candidate(), ledger)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork_id = "aic_999999"
    publication_id = "new-feed-1"
    history_tracker.reserve_artworks(
        [{"id": artwork_id, "title": "Test", "artist": "Artist", "museum": "Museum"}],
        "single", publication_id,
    )
    history_tracker.start_publication_attempt(
        [artwork_id], "container-1", expected_publication_id=publication_id
    )
    history_tracker.record_publish_response(
        [artwork_id], "media-1", expected_publication_id=publication_id
    )
    result = history_tracker.confirm_artworks_and_record_publication(
        [artwork_id], "media-1", "single", publication_id=publication_id,
    )
    state, _ = store.load_safety()
    receipts, _ = store.load_receipts()
    assert result["id"] == publication_id
    assert artwork_id in state.published_artwork_protection.entries
    assert state.published_artwork_protection.entries[artwork_id].origin == "NEW"
    assert state.active_publication_state.receipt_sync_pending == []
    assert len(receipts.records) == 1
    assert receipts.records[0].record_origin == "NEW"
    assert receipts.records[0].artwork_positions[0].canonical_artwork_id == artwork_id
    publication_state.validate_live_receipt_coverage(state, receipts)
    empty_ledger = publication_state.validate_receipts(publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "test",
        "source_sha256": "a" * 64, "record_count": 0, "records": [],
    }))
    with pytest.raises(publication_state.StateValidationError, match="exact receipt"):
        publication_state.validate_live_receipt_coverage(state, empty_ledger)
    missing_protection = state.model_dump(mode="json")
    del missing_protection["published_artwork_protection"]["entries"][artwork_id]
    missing_protection["published_artwork_protection"]["entry_count"] -= 1
    missing_protection["published_artwork_protection"] = publication_state.seal(
        missing_protection["published_artwork_protection"]
    )
    with pytest.raises(publication_state.StateValidationError, match="permanent protection"):
        publication_state.validate_safety_state(publication_state.seal(missing_protection))
    assert client.puts >= 5


def test_reel_reservation_cannot_bypass_permanent_protection(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork_id = FIXTURE["A_PROVEN_PUBLISHED_ARTWORK_IDS"][0]
    release = ReelReleaseIdentity(
        version="artfolio-release-v1", reel_id=artwork_id,
        created_at="2026-09-23T12:00:00Z", manifest_sha256="a" * 64,
        files_sha256={key: "b" * 64 for key in REEL_RELEASE_FILE_HASH_KEYS},
    )
    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_reel(artwork_id, release)
    assert client.puts == 0


def test_feed_and_reel_pending_locks_block_each_other(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)

    def release(artwork_id):
        return ReelReleaseIdentity(
            version="artfolio-release-v1", reel_id=artwork_id,
            created_at="2026-09-23T12:00:00Z", manifest_sha256="a" * 64,
            files_sha256={key: "b" * 64 for key in REEL_RELEASE_FILE_HASH_KEYS},
        )

    feed_artwork = {"id": "aic_999987", "title": "Feed", "artist": "Artist", "museum": "Museum"}
    history_tracker.reserve_artworks([feed_artwork], "single", "feed-first")
    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_reel(feed_artwork["id"], release(feed_artwork["id"]))

    reel_artwork = {"id": "aic_999986", "title": "Reel", "artist": "Artist", "museum": "Museum"}
    history_tracker.reserve_reel(reel_artwork["id"], release(reel_artwork["id"]))
    with pytest.raises(RuntimeError, match="already protected"):
        history_tracker.reserve_artworks([reel_artwork], "single", "feed-second")
    assert client.puts == 2


def test_receipt_failure_leaves_published_protection_and_replay_marker(monkeypatch):
    ledger = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "test",
        "source_sha256": "a" * 64, "record_count": 0, "records": [],
    })
    store, _ = store_for(safety_candidate(), ledger)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork_id = "aic_999998"
    history_tracker.reserve_artworks(
        [{"id": artwork_id, "title": "Test", "artist": "Artist", "museum": "Museum"}],
        "single", "new-feed-2",
    )
    history_tracker.start_publication_attempt(
        [artwork_id], "container-2", expected_publication_id="new-feed-2"
    )
    history_tracker.record_publish_response(
        [artwork_id], "media-2", expected_publication_id="new-feed-2"
    )
    original_append = store.append_receipt
    monkeypatch.setattr(
        store, "append_receipt",
        lambda _receipt: (_ for _ in ()).throw(
            publication_state.StateWriteUncertainError("receipt unavailable")
        ),
    )
    with pytest.raises(publication_state.StateWriteUncertainError):
        history_tracker.confirm_artworks_and_record_publication(
            [artwork_id], "media-2", "single", publication_id="new-feed-2",
        )
    state, _ = store.load_safety()
    assert artwork_id in state.published_artwork_protection.entries
    assert state.active_publication_state.receipt_sync_pending == ["new-feed-2"]
    assert state.active_publication_state.posted_artworks[0]["status"] == "PUBLISHED"
    monkeypatch.setattr(store, "append_receipt", original_append)
    assert publication_state.replay_pending_receipts(store) == 1
    assert store.load_safety()[0].active_publication_state.receipt_sync_pending == []
    assert store.load_receipts()[0].record_count == 1


def test_lost_receipt_append_response_replays_without_duplicate(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork = {"id": "aic_999984", "title": "Test", "artist": "Artist", "museum": "Museum"}
    publication_id = "lost-receipt-response"
    history_tracker.reserve_artworks([artwork], "single", publication_id)
    history_tracker.start_publication_attempt(
        [artwork["id"]], "container", expected_publication_id=publication_id
    )
    history_tracker.record_publish_response(
        [artwork["id"]], "media-id", expected_publication_id=publication_id
    )
    original_put = client.put_object
    lost = {"raised": False}

    def put_with_lost_response(**kwargs):
        result = original_put(**kwargs)
        if kwargs["Key"] == publication_state.RECEIPTS_KEY and not lost["raised"]:
            lost["raised"] = True
            raise EndpointConnectionError(endpoint_url="https://fake.example")
        return result

    monkeypatch.setattr(client, "put_object", put_with_lost_response)
    with pytest.raises(publication_state.StateWriteUncertainError):
        history_tracker.confirm_artworks_and_record_publication(
            [artwork["id"]], "media-id", "single", publication_id=publication_id
        )
    assert store.load_safety()[0].active_publication_state.receipt_sync_pending == [publication_id]
    assert store.load_receipts()[0].record_count == 1
    assert publication_state.replay_pending_receipts(store) == 1
    assert store.load_receipts()[0].record_count == 1
    assert store.load_safety()[0].active_publication_state.receipt_sync_pending == []


def test_receipt_append_is_idempotent_and_rejects_conflicting_identity():
    ledger = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "test",
        "source_sha256": "a" * 64, "record_count": 0, "records": [],
    })
    store, client = store_for(safety_candidate(), ledger)
    receipt = {
        "publication_id": "historical-1", "instagram_media_id": "media-1",
        "publication_type": "single", "historical_state": "PUBLISHED_CONFIRMED",
        "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
        "identity_completeness": "COMPLETE", "occurred_at": None,
        "permalink": None, "workflow_run_id": None,
        "artwork_positions": [{
            "position": 1, "canonical_artwork_id": "aic_1",
            "instagram_child_media_id": None, "caption_label": None,
        }], "evidence_ref": "forensic:1",
    }
    store.append_receipt(receipt)
    store.append_receipt(receipt)
    assert client.puts == 1
    assert store.load_receipts()[0].record_count == 1
    conflicting = {**receipt, "instagram_media_id": "media-2"}
    with pytest.raises(publication_state.StateValidationError, match="conflicts"):
        store.append_receipt(conflicting)


def test_recovered_receipt_collision_blocks_new_reservation(monkeypatch):
    recovered = {
        "publication_id": "collision-id", "instagram_media_id": "historical-media",
        "publication_type": "single", "historical_state": "PUBLISHED_CONFIRMED",
        "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
        "identity_completeness": "COMPLETE", "occurred_at": None,
        "permalink": None, "workflow_run_id": None,
        "artwork_positions": [{
            "position": 1, "canonical_artwork_id": "aic_1",
            "instagram_child_media_id": None, "caption_label": None,
        }], "evidence_ref": "forensic:collision",
    }
    ledger = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "test",
        "source_sha256": "a" * 64, "record_count": 1, "records": [recovered],
    })
    store, _ = store_for(safety_candidate(), ledger)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    artwork = {"id": "aic_999985", "title": "Test", "artist": "Artist", "museum": "Museum"}
    with pytest.raises(publication_state.StateValidationError, match="existing receipt"):
        history_tracker.reserve_artworks([artwork], "single", "collision-id")
    state, _ = store.load_safety()
    assert artwork["id"] not in state.published_artwork_protection.entries
    assert state.active_publication_state.posted_artworks == []
    assert store.load_receipts()[0].records[0].instagram_media_id == "historical-media"


def test_legacy_conversion_requires_complete_strict_snapshot():
    with pytest.raises(publication_state.StateValidationError, match="incomplete"):
        publication_state.convert_legacy_snapshot({"posted_artworks": []})
    with pytest.raises(publication_state.StateValidationError, match="unsupported"):
        publication_state.convert_legacy_snapshot({
            "schema_version": 2, "posted_artworks": [], "publications": [],
        })
    snapshot = {
        "schema_version": 1, "posted_artworks": [], "publications": [],
        "reel_reservations": [], "reel_publications": [],
        "reel_publication_count": 0, "grid_publication_count": 0,
    }
    safety, ledger = publication_state.convert_legacy_snapshot(snapshot)
    assert publication_state.validate_safety_state(safety).generation == 1
    assert publication_state.validate_receipts(ledger).record_count == 0


def test_missing_legacy_object_cannot_be_interpreted_as_empty(monkeypatch):
    for key, value in {
        "CLOUDFLARE_R2_ACCOUNT_ID": "account",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "legacy-key",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "legacy-secret",
        "CLOUDFLARE_R2_BUCKET_NAME": "legacy-media",
    }.items():
        monkeypatch.setenv(key, value)

    class MissingObjectClient:
        def get_object(self, **_kwargs):
            raise ClientError({"Error": {"Code": "NoSuchKey"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")

    monkeypatch.setattr(history_tracker, "_get_s3_client", MissingObjectClient)
    with pytest.raises(RuntimeError, match="migration cannot assume empty history"):
        history_tracker.load_legacy_history_for_migration()
