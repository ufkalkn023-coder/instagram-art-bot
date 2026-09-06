import io
import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from src.insights_storage import (
    InsightsConcurrencyError,
    InsightsStorage,
    InsightsStorageError,
    parse_aware_timestamp,
    partition_key,
)


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "operation")


def _snapshot(
    publication_id="pub-1",
    media_id="media-1",
    target=24,
    *,
    captured_at="2026-08-24T12:00:00Z",
    metrics=None,
    completion_status=None,
):
    snapshot = {
        "publication_id": publication_id,
        "media_id": media_id,
        "target_age_hours": target,
        "captured_at": captured_at,
        "actual_age_hours": 24.0,
        "metrics": {"views": 0} if metrics is None else metrics,
        "missing_metrics": ["reach"],
        "api_version": "v22.0",
    }
    if completion_status:
        snapshot["completion_status"] = completion_status
    return snapshot


class FakeS3:
    def __init__(self, objects=None, conflict=False):
        self.objects = objects or {}
        self.put_calls = []
        self.conflict = conflict

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        value = self.objects[Key]
        return {"Body": io.BytesIO(value.encode()), "ETag": '"etag-1"'}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.conflict:
            raise _client_error("PreconditionFailed")

    def list_objects_v2(self, **kwargs):
        return {
            "Contents": [{"Key": key} for key in sorted(self.objects)],
            "IsTruncated": False,
        }


def test_missing_partition_initializes_and_uses_if_none_match():
    fake = FakeS3()
    storage = InsightsStorage(fake, "bucket")
    key, data, etag = storage.load_partition("2026-07-31T23:00:00-01:00")

    assert key == "insights/2026-08.json"
    assert data == {"schema_version": 1, "snapshots": []}
    assert etag is None
    storage.append_snapshot(key, data, etag, _snapshot())
    assert fake.put_calls[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in fake.put_calls[0]


def test_aware_datetime_is_preserved_and_naive_or_malformed_values_are_rejected():
    aware = datetime(2026, 8, 24, 15, 30, tzinfo=timezone.utc)

    assert parse_aware_timestamp(aware) is aware
    assert partition_key(aware) == "insights/2026-08.json"
    assert parse_aware_timestamp(datetime(2026, 8, 24, 15, 30)) is None
    assert parse_aware_timestamp("not-a-date") is None


def test_existing_partition_uses_etag_and_keys_slots_by_instagram_media():
    existing = _snapshot()
    fake = FakeS3({"insights/2026-08.json": json.dumps({"schema_version": 1, "snapshots": [existing]})})
    storage = InsightsStorage(fake, "bucket")
    key, data, etag = storage.load_partition("2026-08-01T00:00:00Z")

    storage.append_snapshot(key, data, etag, _snapshot(target=72))
    assert fake.put_calls[0]["IfMatch"] == "etag-1"
    with pytest.raises(InsightsStorageError, match="slot already exists"):
        storage.append_snapshot(key, data, etag, _snapshot())
    with pytest.raises(InsightsStorageError, match="Conflicting analytics snapshot identity"):
        storage.append_snapshot(key, data, etag, _snapshot(media_id="corrected-media"))


def test_partial_and_later_completion_are_append_only_and_roundtrip():
    partial = _snapshot(completion_status="partial")
    fake = FakeS3({
        "insights/2026-08.json": json.dumps({"schema_version": 1, "snapshots": [partial]})
    })
    storage = InsightsStorage(fake, "bucket")
    key, data, etag = storage.load_partition("2026-08-01T00:00:00Z")
    completed = _snapshot(
        captured_at="2026-08-24T13:00:00Z",
        metrics={"reach": 8, "likes": 0},
        completion_status="learning_complete",
    )

    storage.append_snapshot(key, data, etag, completed)

    payload = json.loads(fake.put_calls[0]["Body"])
    assert payload["snapshots"] == [partial, completed]
    roundtrip = InsightsStorage(
        FakeS3({"insights/2026-08.json": json.dumps(payload)}),
        "bucket",
    ).load_partition("2026-08-01T00:00:00Z")[1]
    assert roundtrip == payload
    with pytest.raises(InsightsStorageError, match="already complete"):
        storage.append_snapshot(
            key,
            payload,
            "new-etag",
            _snapshot(captured_at="2026-08-24T14:00:00Z"),
        )


def test_concurrent_completion_preserves_the_existing_partial_attempt():
    partial = _snapshot(completion_status="partial")
    fake = FakeS3(
        {"insights/2026-08.json": json.dumps({"schema_version": 1, "snapshots": [partial]})},
        conflict=True,
    )
    storage = InsightsStorage(fake, "bucket")
    key, data, etag = storage.load_partition("2026-08-01T00:00:00Z")
    completed = _snapshot(
        captured_at="2026-08-24T13:00:00Z",
        metrics={"reach": 8, "likes": 0},
        completion_status="learning_complete",
    )

    with pytest.raises(InsightsConcurrencyError):
        storage.append_snapshot(key, data, etag, completed)

    assert data["snapshots"] == [partial]


def test_associations_use_conditional_deterministic_writes_and_reject_duplicates():
    fake = FakeS3()
    storage = InsightsStorage(fake, "bucket")
    data, etag = storage.load_associations()
    association = {
        "canonical_artwork_id": "met_1", "reel_id": "met_1", "instagram_media_id": "ig_1",
        "permalink": "https://instagram.com/reel/ig_1/", "published_at": "2026-08-24T12:00:00Z",
        "matched_at": "2026-08-25T12:00:00Z", "match_method": "manual",
    }
    storage.write_associations({**data, "associations": [association]}, etag)
    assert fake.put_calls[0]["IfNoneMatch"] == "*"
    payload = fake.put_calls[0]["Body"].decode()
    assert payload.endswith("\n")
    assert "access_token" not in payload

    with pytest.raises(InsightsStorageError, match="Duplicate"):
        storage.write_associations({"schema_version": 1, "associations": [association, {**association, "instagram_media_id": "ig_2"}]}, None)


def test_conditional_write_conflict_and_malformed_object_fail_closed():
    conflict_storage = InsightsStorage(FakeS3(conflict=True), "bucket")
    with pytest.raises(InsightsConcurrencyError):
        conflict_storage.append_snapshot("insights/2026-08.json", {"schema_version": 1, "snapshots": []}, None, _snapshot())

    malformed_storage = InsightsStorage(FakeS3({"insights/2026-08.json": "{\"schema_version\": 1, \"snapshots\": [{}}"}), "bucket")
    with pytest.raises(InsightsStorageError, match="malformed"):
        malformed_storage.load_partition("2026-08-01T00:00:00Z")


def test_all_snapshot_partitions_are_loaded_in_stable_order_and_nonpartitions_ignored():
    january = _snapshot(publication_id="january", media_id="media-january")
    august = _snapshot(publication_id="august", media_id="media-august", target=72)
    fake = FakeS3(
        {
            "insights/2026-08.json": json.dumps({"schema_version": 1, "snapshots": [august]}),
            "insights/2026-01.json": json.dumps({"schema_version": 1, "snapshots": [january]}),
            "insights/media-associations.json": json.dumps({"schema_version": 1, "associations": []}),
        }
    )

    snapshots = InsightsStorage(fake, "bucket").load_all_snapshots()

    assert [snapshot["publication_id"] for snapshot in snapshots] == ["january", "august"]


def test_load_all_snapshots_keeps_valid_entries_and_reports_malformed_entries(caplog):
    valid = _snapshot()
    malformed = {**_snapshot(publication_id="bad", media_id="bad-media"), "metrics": {"reach": "bad"}}
    fake = FakeS3({
        "insights/2026-08.json": json.dumps({
            "schema_version": 1,
            "snapshots": [valid, {}, malformed],
        })
    })
    storage = InsightsStorage(fake, "bucket")

    snapshots = storage.load_all_snapshots()

    assert snapshots == [valid]
    assert storage.last_snapshot_load_diagnostics.valid_snapshots == 1
    assert storage.last_snapshot_load_diagnostics.invalid_snapshots == 2
    assert "Skipped 2 malformed analytics snapshots" in caplog.text
