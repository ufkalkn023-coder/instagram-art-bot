"""Separate, ETag-protected R2 storage for Instagram Insights snapshots."""

import json
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

HISTORY_OBJECT_KEY = "posted_history.json"
SNAPSHOT_SCHEMA_VERSION = 1


class InsightsStorageError(Exception):
    """Analytics storage could not be safely read or written."""


class InsightsConcurrencyError(InsightsStorageError):
    """An R2 conditional write lost a concurrent update."""


def parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    return timestamp.astimezone(timezone.utc)


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def partition_key(posted_at: Any) -> str:
    """Keep every publication's slots in the UTC month in which it was posted."""
    timestamp = parse_aware_timestamp(posted_at)
    if timestamp is None:
        raise InsightsStorageError("Publication posted_at must be an aware timestamp")
    return f"insights/{timestamp:%Y-%m}.json"


def _get_r2_client():
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
    if not all((account_id, access_key, secret_key)):
        raise InsightsStorageError("Missing CLOUDFLARE_R2 credentials")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def _get_bucket_name() -> str:
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
    if not bucket:
        raise InsightsStorageError("Missing CLOUDFLARE_R2_BUCKET_NAME")
    return bucket


def _is_valid_snapshot(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if not isinstance(snapshot.get("publication_id"), str) or not snapshot["publication_id"]:
        return False
    if not isinstance(snapshot.get("media_id"), str) or not snapshot["media_id"]:
        return False
    if snapshot.get("target_age_hours") not in {24, 72, 168}:
        return False
    if parse_aware_timestamp(snapshot.get("captured_at")) is None:
        return False
    actual_age = snapshot.get("actual_age_hours")
    if isinstance(actual_age, bool) or not isinstance(actual_age, (int, float)) or actual_age < 0:
        return False
    metrics = snapshot.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return False
    if any(not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)) for key, value in metrics.items()):
        return False
    return isinstance(snapshot.get("missing_metrics"), list) and isinstance(snapshot.get("api_version"), str)


def _validated_analytics_object(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise InsightsStorageError("Malformed analytics object")
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, list):
        raise InsightsStorageError("Malformed analytics snapshots")
    seen_slots: dict[tuple[str, int], str] = {}
    for snapshot in snapshots:
        if not _is_valid_snapshot(snapshot):
            raise InsightsStorageError("Malformed analytics snapshot")
        slot = (snapshot["publication_id"], snapshot["target_age_hours"])
        previous_media = seen_slots.get(slot)
        if previous_media is not None:
            raise InsightsStorageError("Duplicate analytics snapshot slot")
        seen_slots[slot] = snapshot["media_id"]
    return data


class InsightsStorage:
    def __init__(self, s3_client=None, bucket_name: str | None = None):
        self._s3 = s3_client if s3_client is not None else _get_r2_client()
        self._bucket = bucket_name if bucket_name is not None else _get_bucket_name()

    def load_history(self) -> dict[str, Any]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=HISTORY_OBJECT_KEY)
            data = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as exc:
            raise InsightsStorageError("Unable to read posted history from R2") from exc
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InsightsStorageError("Posted history is unreadable or malformed") from exc
        if not isinstance(data, dict):
            raise InsightsStorageError("Posted history is malformed")
        return data

    def load_partition(self, posted_at: Any) -> tuple[str, dict[str, Any], str | None]:
        key = partition_key(posted_at)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return key, {"schema_version": SNAPSHOT_SCHEMA_VERSION, "snapshots": []}, None
            raise InsightsStorageError("Unable to read analytics partition from R2") from exc
        try:
            data = _validated_analytics_object(json.loads(response["Body"].read().decode("utf-8")))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InsightsStorageError("Analytics partition is unreadable or malformed") from exc
        return key, data, response.get("ETag", "").strip('"')

    def append_snapshot(self, key: str, data: dict[str, Any], etag: str | None, snapshot: dict[str, Any]) -> None:
        _validated_analytics_object(data)
        if not _is_valid_snapshot(snapshot):
            raise InsightsStorageError("Attempted to store malformed analytics snapshot")
        slot = (snapshot["publication_id"], snapshot["target_age_hours"])
        for existing in data["snapshots"]:
            if (existing["publication_id"], existing["target_age_hours"]) != slot:
                continue
            if existing["media_id"] != snapshot["media_id"]:
                raise InsightsStorageError("Conflicting analytics snapshot identity")
            raise InsightsStorageError("Analytics snapshot slot already exists")

        payload = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "snapshots": [*data["snapshots"], snapshot]}
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            "ContentType": "application/json",
        }
        if etag:
            kwargs["IfMatch"] = etag
        else:
            kwargs["IfNoneMatch"] = "*"
        try:
            self._s3.put_object(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}:
                raise InsightsConcurrencyError("Analytics conditional write conflict") from exc
            raise InsightsStorageError("Unable to write analytics partition to R2") from exc
