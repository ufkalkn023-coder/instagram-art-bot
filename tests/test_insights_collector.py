from datetime import datetime, timedelta, timezone

from src.insights_collector import InsightsCollector, select_due_slot
from src.insights_storage import InsightsConcurrencyError
from src.instagram_insights import InsightsResponse, InstagramInsightsRequestError, TARGET_METRICS


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


def _response(metrics=None):
    metrics = {"views": 4} if metrics is None else metrics
    returned = tuple(metric for metric in TARGET_METRICS if metric in metrics)
    return InsightsResponse(metrics, TARGET_METRICS, returned, tuple(metric for metric in TARGET_METRICS if metric not in metrics))


def test_due_policy_prefers_highest_defensible_slot_and_expires_old_gaps():
    assert select_due_slot(23, set()) == (None, 0)
    assert select_due_slot(30, set()) == (24, 0)
    assert select_due_slot(80, set()) == (72, 0)
    assert select_due_slot(190, set()) == (168, 1)
    assert select_due_slot(800, set()) == (None, 3)


def test_collector_writes_one_snapshot_for_carousel_parent_and_never_changes_history():
    publication = _publication("carousel", 80, "parent-media", "carousel")
    storage = FakeStorage([publication])
    client = FakeClient([_response({"views": 0, "saved": 2})])

    summary = InsightsCollector(storage, client, NOW).run()

    assert summary.snapshots_written == 1
    assert summary.api_requests == 1
    assert client.media_ids == ["parent-media"]
    assert storage.appended[0]["target_age_hours"] == 72
    assert storage.appended[0]["metrics"] == {"views": 0, "saved": 2}
    assert storage.history["posted_artworks"] == []
    assert storage.history["publications"] == [publication]


def test_existing_slots_are_skipped_and_empty_data_is_retried():
    publication = _publication("pub", 30)
    existing = {
        "publication_id": "pub", "media_id": "media-pub", "target_age_hours": 24,
        "captured_at": "2026-08-24T11:00:00Z", "actual_age_hours": 24, "metrics": {"views": 1},
        "missing_metrics": [], "api_version": "v22.0",
    }
    complete_storage = FakeStorage([publication], [existing])
    assert InsightsCollector(complete_storage, FakeClient([]), NOW).run().api_requests == 0

    pending_storage = FakeStorage([_publication("pending", 30)])
    pending_summary = InsightsCollector(pending_storage, FakeClient([_response({})]), NOW).run()
    assert pending_summary.availability_pending == 1
    assert pending_storage.appended == []


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
