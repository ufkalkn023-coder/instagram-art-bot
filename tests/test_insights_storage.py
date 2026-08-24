import io
import json

import pytest
from botocore.exceptions import ClientError

from src.insights_storage import InsightsConcurrencyError, InsightsStorage, InsightsStorageError


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "operation")


def _snapshot(publication_id="pub-1", media_id="media-1", target=24):
    return {
        "publication_id": publication_id,
        "media_id": media_id,
        "target_age_hours": target,
        "captured_at": "2026-08-24T12:00:00Z",
        "actual_age_hours": 24.0,
        "metrics": {"views": 0},
        "missing_metrics": ["reach"],
        "api_version": "v22.0",
    }


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


def test_existing_partition_uses_etag_and_rejects_duplicates_or_conflicts():
    existing = _snapshot()
    fake = FakeS3({"insights/2026-08.json": json.dumps({"schema_version": 1, "snapshots": [existing]})})
    storage = InsightsStorage(fake, "bucket")
    key, data, etag = storage.load_partition("2026-08-01T00:00:00Z")

    storage.append_snapshot(key, data, etag, _snapshot(target=72))
    assert fake.put_calls[0]["IfMatch"] == "etag-1"
    with pytest.raises(InsightsStorageError, match="slot already exists"):
        storage.append_snapshot(key, data, etag, _snapshot())
    with pytest.raises(InsightsStorageError, match="Conflicting"):
        storage.append_snapshot(key, data, etag, _snapshot(media_id="other-media"))


def test_conditional_write_conflict_and_malformed_object_fail_closed():
    conflict_storage = InsightsStorage(FakeS3(conflict=True), "bucket")
    with pytest.raises(InsightsConcurrencyError):
        conflict_storage.append_snapshot("insights/2026-08.json", {"schema_version": 1, "snapshots": []}, None, _snapshot())

    malformed_storage = InsightsStorage(FakeS3({"insights/2026-08.json": "{\"schema_version\": 1, \"snapshots\": [{}}"}), "bucket")
    with pytest.raises(InsightsStorageError, match="malformed"):
        malformed_storage.load_partition("2026-08-01T00:00:00Z")
