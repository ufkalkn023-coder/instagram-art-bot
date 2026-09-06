from datetime import datetime, timedelta, timezone

import pytest

from src.insights_collector import InsightsCollector, manual_association, select_due_slot
from src.insights_storage import InsightsConcurrencyError, InsightsStorageError
from src.instagram_insights import (
    InsightsResponse,
    InstagramInsightsRequestError,
    InstagramMedia,
    MediaDiscoveryResponse,
    TARGET_METRICS,
)
from src.reel_analytics import LocalReel


NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def _publication(identifier, age_hours, media_id=None, publication_type="single"):
    return {
        "id": identifier,
        "type": publication_type,
        "media_id": media_id or f"media-{identifier}",
        "artwork_ids": [f"art-{identifier}"] if publication_type == "single" else [f"art-{identifier}-1", f"art-{identifier}-2"],
        "posted_at": (NOW - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class FakeStorage:
    def __init__(self, publications, snapshots=None, conflict=False):
        self.history = {"posted_artworks": [], "publications": publications}
        self.partition = {"schema_version": 1, "snapshots": snapshots or []}
        self.appended = []
        self.conflict = conflict

    def load_history(self):
        return self.history

    def load_partition(self, posted_at):
        return "insights/2026-08.json", self.partition, "etag"

    def append_snapshot(self, key, data, etag, snapshot):
        if self.conflict:
            raise InsightsConcurrencyError("conflict")
        self.appended.append(snapshot)
        self.partition["snapshots"].append(snapshot)


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.media_ids = []

    def fetch_media_insights(self, media_id):
        self.media_ids.append(media_id)
        outcome = next(self.responses)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(metrics=None, **kwargs):
    metrics = {"views": 4} if metrics is None else metrics
    returned = tuple(metric for metric in TARGET_METRICS if metric in metrics)
    return InsightsResponse(
        metrics,
        TARGET_METRICS,
        returned,
        tuple(metric for metric in TARGET_METRICS if metric not in metrics),
        **kwargs,
    )


def test_due_policy_prefers_highest_defensible_slot_and_expires_old_gaps():
    assert select_due_slot(0.99, set()) == (None, 0)
    assert select_due_slot(1, set()) == (1, 0)
    assert select_due_slot(5.99, set()) == (1, 0)
    assert select_due_slot(6, set()) == (6, 1)
    assert select_due_slot(23, {1}) == (6, 0)
    assert select_due_slot(24, {1, 6}) == (24, 0)
    assert select_due_slot(72, {1, 6, 24}) == (72, 0)
    assert select_due_slot(168, {1, 6, 24, 72}) == (168, 0)
    assert select_due_slot(800, set()) == (None, 5)


def test_collector_writes_one_snapshot_for_carousel_parent_and_never_changes_history():
    publication = _publication("carousel", 80, "parent-media", "carousel")
    storage = FakeStorage([publication])
    client = FakeClient([_response({"views": 0, "saved": 2})])

    summary = InsightsCollector(storage, client, NOW).run()

    assert summary.snapshots_written == 1
    assert summary.api_requests == 1
    assert client.media_ids == ["parent-media"]
    collected = next(item for item in storage.appended if "failure_category" not in item)
    assert collected["target_age_hours"] == 72
    assert collected["metrics"] == {"views": 0, "saved": 2}
    assert storage.history["posted_artworks"] == []
    assert storage.history["publications"] == [publication]


def test_legacy_naive_publication_timestamp_is_normalized_as_utc():
    publication = _publication("legacy-utc", 24)
    publication["posted_at"] = "2026-08-23 12:00:00"
    storage = FakeStorage([publication])

    summary = InsightsCollector(storage, FakeClient([_response()]), NOW).run()

    assert summary.invalid_publications == 0
    assert summary.snapshots_written == 1
    collected = next(item for item in storage.appended if "failure_category" not in item)
    assert collected["target_age_hours"] == 24


def test_aware_legacy_publication_offset_preserves_its_instant():
    publication = _publication("aware-offset", 24)
    publication["posted_at"] = "2026-08-23T15:00:00+03:00"
    storage = FakeStorage([publication])

    summary = InsightsCollector(storage, FakeClient([_response()]), NOW).run()

    assert summary.invalid_publications == 0
    assert summary.snapshots_written == 1
    assert storage.appended[0]["actual_age_hours"] == 24.0


def test_malformed_and_unknown_naive_legacy_timestamps_are_reported_without_blocking_valid_records(caplog):
    malformed = _publication("malformed", 24)
    malformed["posted_at"] = "not-a-date"
    unknown_naive = _publication("unknown-naive", 24)
    unknown_naive["posted_at"] = "2026-08-23T12:00:00"
    valid = _publication("valid", 24)
    storage = FakeStorage([malformed, unknown_naive, valid])

    summary = InsightsCollector(storage, FakeClient([_response()]), NOW).run()

    assert summary.invalid_publications == 2
    assert summary.snapshots_written == 1
    assert "id=malformed" in caplog.text
    assert "id=unknown-naive" in caplog.text


def test_learning_complete_slots_are_skipped_and_empty_data_is_retried():
    publication = _publication("pub", 30)
    existing = {
        "publication_id": "pub", "media_id": "media-pub", "target_age_hours": 24,
        "captured_at": "2026-08-24T11:00:00Z", "actual_age_hours": 24,
        "metrics": {"reach": 1, "likes": 0}, "completion_status": "learning_complete",
        "missing_metrics": [], "api_version": "v22.0",
    }
    complete_storage = FakeStorage([publication], [existing])
    assert InsightsCollector(complete_storage, FakeClient([]), NOW).run().api_requests == 0

    pending_storage = FakeStorage([_publication("pending", 30)])
    pending_summary = InsightsCollector(pending_storage, FakeClient([_response({})]), NOW).run()
    assert pending_summary.availability_pending == 1
    assert all(item["failure_category"] == "missed_collection_window" for item in pending_storage.appended)


def test_partial_snapshot_is_append_only_and_later_completed_in_same_slot():
    publication = _publication("partial", 30)
    storage = FakeStorage([publication])

    first = InsightsCollector(storage, FakeClient([_response({"views": 10})]), NOW).run()
    second = InsightsCollector(
        storage,
        FakeClient([_response({"views": 12, "reach": 8, "likes": 0})]),
        NOW + timedelta(hours=1),
    ).run()

    attempts = [
        item
        for item in storage.partition["snapshots"]
        if item["target_age_hours"] == 24
    ]
    assert first.partial_snapshots == 1
    assert second.learning_complete_snapshots == 1
    assert [item["completion_status"] for item in attempts] == [
        "partial",
        "learning_complete",
    ]
    assert attempts[0]["metrics"] == {"views": 10}
    assert attempts[1]["metrics"]["reach"] == 8


def test_all_metric_permanent_failure_is_terminal_not_availability_pending():
    storage = FakeStorage([_publication("unsupported", 30)])
    response = _response(
        {},
        permanent_failure_category="all_metrics_permanently_unavailable",
        permanently_failed_metrics=TARGET_METRICS,
        diagnostics=("sanitized diagnostic",),
    )

    summary = InsightsCollector(storage, FakeClient([response]), NOW).run()

    terminal = storage.appended[-1]
    assert summary.availability_pending == 0
    assert summary.permanently_unavailable == 1
    assert terminal["completion_status"] == "permanently_unavailable"
    assert terminal["failure_category"] == "all_metrics_permanently_unavailable"
    assert terminal["diagnostics"] == ["sanitized diagnostic"]


def test_repeated_runs_do_not_recount_persisted_missed_slots():
    storage = FakeStorage([_publication("old", 800)])

    first = InsightsCollector(storage, FakeClient([]), NOW).run()
    second = InsightsCollector(storage, FakeClient([]), NOW).run()

    assert first.missed_slots == 5
    assert first.missed_slot_markers_written == 5
    assert second.missed_slots == 0
    assert second.missed_slot_markers_written == 0


def test_api_failure_and_write_conflict_are_isolated_per_publication():
    storage = FakeStorage([_publication("bad", 30), _publication("good", 30)])
    client = FakeClient([InstagramInsightsRequestError("unavailable"), _response()])
    summary = InsightsCollector(storage, client, NOW).run()
    assert summary.api_failures == 1
    assert summary.snapshots_written == 1

    conflict_storage = FakeStorage([_publication("conflict", 30)], conflict=True)
    conflict_summary = InsightsCollector(conflict_storage, FakeClient([_response()]), NOW).run()
    assert conflict_summary.write_conflicts == 1
    assert conflict_summary.snapshots_written == 0


def test_legacy_history_is_noop_and_dry_run_never_fetches_or_writes():
    legacy = FakeStorage([])
    assert InsightsCollector(legacy, FakeClient([]), NOW).run().publications_scanned == 0

    storage = FakeStorage([_publication("dry", 30)])
    client = FakeClient([_response()])
    summary = InsightsCollector(storage, client, NOW).run(dry_run=True)
    assert summary.publications_eligible == 1
    assert client.media_ids == []
    assert storage.appended == []


class ReelStorage:
    def __init__(self, associations=None, publications=None):
        self.associations = {"schema_version": 1, "associations": associations or []}
        self.history = {"posted_artworks": [], "publications": publications or []}
        self.association_writes = []
        self.partition = {"schema_version": 1, "snapshots": []}
        self.appended = []

    def load_associations(self):
        return self.associations, "association-etag"

    def load_history(self):
        return self.history

    def write_associations(self, data, etag):
        self.association_writes.append((data, etag))
        self.associations = data

    def load_partition(self, posted_at):
        return "insights/2026-08.json", self.partition, "partition-etag"

    def append_snapshot(self, key, data, etag, snapshot):
        self.appended.append(snapshot)
        self.partition["snapshots"].append(snapshot)


class ReelClient:
    def __init__(self, media):
        self.media = media
        self.insights_media_ids = []

    def discover_recent_media(self):
        return MediaDiscoveryResponse((self.media,), 1, {"app.call_count": 2})

    def fetch_media(self, media_id):
        assert media_id == self.media.id
        return self.media

    def fetch_media_insights(self, media_id):
        self.insights_media_ids.append(media_id)
        return _response({"reach": 100, "saved": 4})


def test_reel_collection_discovers_matches_persists_and_snapshots():
    published = NOW - timedelta(hours=1.5)
    media = InstagramMedia("ig-reel", "VIDEO", "REELS", "same caption", "https://instagram.com/reel/ig-reel/", published.isoformat())
    local = LocalReel("met_1", "met_1", "Work", "Artist", NOW - timedelta(hours=3), "same caption", "/render.mp4")
    storage = ReelStorage()
    client = ReelClient(media)

    summary = InsightsCollector(storage, client, NOW).run(local_reels=(local,))

    assert summary.media_discovered == 1
    assert summary.matched_new == 1
    assert summary.snapshots_written == 1
    assert summary.api_calls == 2
    assert summary.rate_limit_usage == {"app.call_count": 2}
    assert storage.association_writes[0][0]["associations"][0]["match_method"] == "caption_exact"
    assert storage.appended[0]["target_age_hours"] == 1
    assert storage.appended[0]["derived_metrics"] == {"save_rate": 0.04}

    second = InsightsCollector(storage, ReelClient(media), NOW).run(local_reels=(local,))
    assert second.matched_new == 0
    assert len(storage.associations["associations"]) == 1


def test_persisted_association_collects_without_local_artfolio_files_and_keeps_legacy_targets():
    associated = {
        "canonical_artwork_id": "met_1", "reel_id": "met_1", "instagram_media_id": "ig-reel",
        "published_at": (NOW - timedelta(hours=7)).isoformat(), "matched_at": NOW.isoformat(),
        "match_method": "caption_exact",
    }
    storage = ReelStorage([associated], [_publication("legacy", 7)])
    client = FakeClient([_response(), _response()])

    summary = InsightsCollector(storage, client, NOW).run(local_reels=None, association_mode=True)

    assert summary.media_discovered == 0
    assert summary.mapped_media == 1
    assert summary.snapshots_written == 2
    assert client.media_ids == ["ig-reel", "media-legacy"]


def test_discovery_failure_does_not_block_due_persisted_snapshot():
    associated = {
        "canonical_artwork_id": "met_1", "reel_id": "met_1", "instagram_media_id": "ig-reel",
        "published_at": (NOW - timedelta(hours=7)).isoformat(), "matched_at": NOW.isoformat(),
        "match_method": "caption_exact",
    }
    storage = ReelStorage([associated])

    class DiscoveryFailureClient(FakeClient):
        def discover_recent_media(self):
            raise InstagramInsightsRequestError("offline")

    client = DiscoveryFailureClient([_response()])
    local = LocalReel("met_1", "met_1", "Work", "Artist", NOW, "caption", "/render.mp4")
    summary = InsightsCollector(storage, client, NOW).run(local_reels=(local,))

    assert summary.api_failures == 1
    assert summary.snapshots_written == 1
    assert client.media_ids == ["ig-reel"]


def test_manual_link_replaces_automatic_mapping_but_not_another_manual_link():
    local = LocalReel("met_1", "met_1", "Work", "Artist", NOW, "caption", "/render.mp4")
    automatic = {
        "canonical_artwork_id": "met_1", "reel_id": "met_1", "instagram_media_id": "old",
        "published_at": NOW.isoformat(), "matched_at": NOW.isoformat(), "match_method": "caption_exact",
    }
    new_media = InstagramMedia("new", "VIDEO", "REELS", None, None, NOW.isoformat())
    storage = ReelStorage([automatic])
    association, changed = manual_association(storage, ReelClient(new_media), (local,), "met_1", "new", NOW)
    assert changed is True
    assert association["match_method"] == "manual"
    assert storage.associations["associations"] == [association]

    with pytest.raises(InsightsStorageError, match="different manual association"):
        manual_association(storage, ReelClient(InstagramMedia("third", "VIDEO", "REELS", None, None, NOW.isoformat())), (local,), "met_1", "third", NOW)
