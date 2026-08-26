"""Live Cloudflare R2 CAS verification; never enabled by credentials alone."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from botocore.exceptions import ClientError
import pytest

from src import history_tracker
from tests.integration.r2_test_support import (
    R2IntegrationContext,
    assert_integration_test_key,
    integration_enabled,
    is_missing_object_error,
    make_run_prefix,
    missing_r2_configuration,
)


pytestmark = pytest.mark.skipif(
    not integration_enabled(),
    reason="live R2 disabled; set ARTFOLIO_RUN_R2_INTEGRATION=1 explicitly",
)


def _assert_precondition_failed(error: ClientError) -> None:
    assert history_tracker._is_precondition_failed(error)
    assert error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412
    assert error.response.get("Error", {}).get("Code") in {
        "PreconditionFailed",
        "412",
    }


@pytest.fixture(scope="session")
def r2_context() -> R2IntegrationContext:
    missing = missing_r2_configuration()
    if missing:
        pytest.fail(
            "LIVE_R2_NOT_RUN_MISSING_CREDENTIALS: " + ", ".join(missing),
            pytrace=False,
        )

    context = R2IntegrationContext(
        client=history_tracker._get_s3_client(),
        bucket=history_tracker._get_bucket_name(),
        prefix=make_run_prefix(),
    )
    print("R2 INTEGRATION VERIFICATION")
    print("R2 integration bucket: configured")
    print(f"R2 integration prefix: {context.prefix}")
    try:
        yield context
    finally:
        try:
            remaining = context.cleanup()
        except Exception as error:
            safe_keys = sorted(context.created_keys)
            print(f"R2 CLEANUP FAILED for safe prefix: {context.prefix}")
            print("Safe integration keys: " + ", ".join(safe_keys))
            pytest.fail(f"R2 integration cleanup failed: {type(error).__name__}")
        else:
            print("Cleanup ................ PASS")
            print(f"Objects remaining: {remaining}")


@pytest.fixture
def r2_object_semantics_context(r2_context) -> R2IntegrationContext:
    """Use a separate UUID namespace for destructive object-semantics checks."""
    context = R2IntegrationContext(
        client=r2_context.client,
        bucket=r2_context.bucket,
        prefix=make_run_prefix(),
    )
    try:
        yield context
    finally:
        assert context.cleanup() == 0


def _put(
    context: R2IntegrationContext,
    key: str,
    body: bytes,
    **conditions: str,
) -> dict:
    context.wait_for_write_slot(key)
    return context.client.put_object(
        Bucket=context.bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        **conditions,
    )


def _get_bytes(context: R2IntegrationContext, key: str) -> bytes:
    response = context.client.get_object(Bucket=context.bucket, Key=key)
    return response["Body"].read()


def test_basic_put_get_head_and_real_etag_shape(r2_context):
    key = r2_context.key("raw/basic.json")
    body = b'{"property":"basic-put-get-head","version":1}'
    assert key == f"{r2_context.prefix}raw/basic.json"

    _put(r2_context, key, body, IfNoneMatch="*")
    fetched = r2_context.client.get_object(Bucket=r2_context.bucket, Key=key)
    head = r2_context.client.head_object(Bucket=r2_context.bucket, Key=key)

    assert fetched["Body"].read() == body
    assert fetched["ContentLength"] == len(body)
    assert head["ContentLength"] == len(body)
    assert fetched["ETag"] == head["ETag"]
    assert history_tracker._validated_etag(fetched["ETag"]) == fetched["ETag"]
    assert fetched["ETag"].startswith('"') and fetched["ETag"].endswith('"')
    print("Basic PUT/GET ........... PASS")
    print("ETag available .......... PASS")


def test_create_if_absent_rejects_existing_object(r2_context):
    key = r2_context.key("raw/create-if-absent.json")
    original = b'{"writer":"first"}'
    _put(r2_context, key, original, IfNoneMatch="*")

    with pytest.raises(ClientError) as captured:
        _put(r2_context, key, b'{"writer":"second"}', IfNoneMatch="*")

    _assert_precondition_failed(captured.value)
    assert _get_bytes(r2_context, key) == original
    print("Create-if-absent ........ PASS")


def test_matching_stale_and_two_writer_cas(r2_context):
    key = r2_context.key("raw/two-writer-cas.json")
    version_a = b'{"version":"A"}'
    version_b = b'{"version":"B"}'
    version_c = b'{"version":"C"}'
    _put(r2_context, key, version_a, IfNoneMatch="*")
    etag_a = r2_context.client.head_object(
        Bucket=r2_context.bucket, Key=key
    )["ETag"]

    _put(r2_context, key, version_b, IfMatch=etag_a)
    etag_b = r2_context.client.head_object(
        Bucket=r2_context.bucket, Key=key
    )["ETag"]
    assert etag_b != etag_a
    assert _get_bytes(r2_context, key) == version_b

    with pytest.raises(ClientError) as captured:
        _put(r2_context, key, version_c, IfMatch=etag_a)

    _assert_precondition_failed(captured.value)
    assert _get_bytes(r2_context, key) == version_b
    assert json.loads(_get_bytes(r2_context, key)) == {"version": "B"}
    print("Matching If-Match ....... PASS")
    print("Stale If-Match .......... PASS")
    print("Two-writer CAS .......... PASS")


def test_missing_object_classification(r2_context):
    key = r2_context.key("raw/intentionally-missing.json")

    with pytest.raises(ClientError) as captured:
        r2_context.client.get_object(Bucket=r2_context.bucket, Key=key)

    assert history_tracker._is_missing_object(captured.value)
    assert captured.value.response.get("ResponseMetadata", {}).get(
        "HTTPStatusCode"
    ) == 404
    print("Missing object .......... PASS")


def test_application_lifecycle_cas_reloads_and_re_evaluates(
    r2_context, monkeypatch
):
    key = r2_context.key("application/reconciliation-history.json")
    artwork_id = "integration_fake_artwork"
    initial = {
        "posted_artworks": [
            {
                "id": artwork_id,
                "publication_id": "integration-fake-publication",
                "publication_type": "SINGLE",
                "status": "PENDING",
                "reserved_at": "2026-08-26T00:00:00Z",
                "container_id": None,
            }
        ]
    }
    monkeypatch.setattr(history_tracker, "HISTORY_OBJECT_KEY", key)
    r2_context.wait_for_write_slot(key)
    history_tracker._upload_history(initial, None)
    loaded, etag = history_tracker.load_history_with_etag()
    assert loaded == initial
    assert history_tracker._validated_etag(etag) == etag

    original_upload = history_tracker._upload_history
    upload_calls = 0

    def race_once(history, stale_etag):
        nonlocal upload_calls
        upload_calls += 1
        if upload_calls == 1:
            current = json.loads(_get_bytes(r2_context, key))
            current["posted_artworks"][0]["other_writer"] = "preserved"
            _put(
                r2_context,
                key,
                json.dumps(current, separators=(",", ":")).encode(),
            )
        r2_context.wait_for_write_slot(key)
        original_upload(history, stale_etag)

    monkeypatch.setattr(history_tracker, "_upload_history", race_once)

    assert history_tracker.start_publication_attempt(
        [artwork_id],
        "integration-fake-container",
        ["integration-fake-child"],
    ) == 1

    after_publish_boundary = json.loads(_get_bytes(r2_context, key))
    record = after_publish_boundary["posted_artworks"][0]
    assert upload_calls == 2
    assert record["status"] == "PUBLISHING"
    assert record["container_id"] == "integration-fake-container"
    assert record["child_container_ids"] == ["integration-fake-child"]
    assert record["other_writer"] == "preserved"

    assert history_tracker.record_reconciliation_result(
        [artwork_id],
        target_status=history_tracker.PublicationStatus.AMBIGUOUS,
        result="STILL_AMBIGUOUS",
        evidence="integration_fake_container_status:FINISHED",
        expected_status=history_tracker.PublicationStatus.PUBLISHING,
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    ) == 1

    reconciled = json.loads(_get_bytes(r2_context, key))["posted_artworks"][0]
    assert reconciled["status"] == "AMBIGUOUS"
    assert reconciled["reconciliation_result"] == "STILL_AMBIGUOUS"
    assert (
        reconciled["reconciliation_evidence"]
        == "integration_fake_container_status:FINISHED"
    )
    assert reconciled["other_writer"] == "preserved"
    print("App-level CAS ........... PASS")
    print("Reconciliation payload .. PASS")


def test_repeated_conditional_json_writes_remain_complete(r2_context):
    key = r2_context.key("raw/json-integrity.json")
    body = json.dumps({"publication_id": "fake", "revision": 0}).encode()
    _put(r2_context, key, body, IfNoneMatch="*")

    for revision in range(1, 5):
        etag = r2_context.client.head_object(
            Bucket=r2_context.bucket, Key=key
        )["ETag"]
        expected = {
            "publication_id": "fake",
            "revision": revision,
            "state": "AMBIGUOUS" if revision % 2 else "PUBLISHING",
        }
        _put(
            r2_context,
            key,
            json.dumps(expected, separators=(",", ":")).encode(),
            IfMatch=etag,
        )
        assert json.loads(_get_bytes(r2_context, key)) == expected
    print("JSON integrity .......... PASS")


def test_exact_delete_is_idempotent_and_object_becomes_absent(
    r2_object_semantics_context,
):
    context = r2_object_semantics_context
    key = context.key("delete-semantics/exact-object.bin")
    context.client.put_object(
        Bucket=context.bucket,
        Key=key,
        Body=b"isolated-delete-semantics",
        ContentType="application/octet-stream",
    )

    context.client.delete_object(Bucket=context.bucket, Key=key)
    with pytest.raises(ClientError) as first_absent:
        context.client.head_object(Bucket=context.bucket, Key=key)
    assert is_missing_object_error(first_absent.value)

    context.client.delete_object(Bucket=context.bucket, Key=key)
    with pytest.raises(ClientError) as still_absent:
        context.client.head_object(Bucket=context.bucket, Key=key)
    assert is_missing_object_error(still_absent.value)
    print("Exact DELETE ............ PASS")
    print("Absent DELETE idempotent  PASS")


def test_exact_prefix_list_delete_preserves_neighbor_and_finishes_empty(
    r2_object_semantics_context,
):
    context = r2_object_semantics_context
    target_prefix = f"{context.prefix}objects/current/"
    assert_integration_test_key(target_prefix, context.prefix)
    target_keys = {
        context.key(f"objects/current/item-{index}.bin")
        for index in range(3)
    }
    neighbor_key = context.key("objects/current-neighbor/keep.bin")
    for key in sorted((*target_keys, neighbor_key)):
        context.client.put_object(
            Bucket=context.bucket,
            Key=key,
            Body=b"isolated-list-delete-semantics",
            ContentType="application/octet-stream",
        )

    listed: list[str] = []
    continuation_token: str | None = None
    pages = 0
    while True:
        request = {
            "Bucket": context.bucket,
            "Prefix": target_prefix,
            "MaxKeys": 2,
        }
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token
        response = context.client.list_objects_v2(**request)
        pages += 1
        for item in response.get("Contents", []):
            key = item["Key"]
            assert_integration_test_key(key, context.prefix)
            assert key.startswith(target_prefix)
            listed.append(key)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
        assert continuation_token

    assert set(listed) == target_keys
    assert pages >= 2
    for key in listed:
        context.client.delete_object(Bucket=context.bucket, Key=key)

    assert context.client.head_object(
        Bucket=context.bucket, Key=neighbor_key
    )["ContentLength"] > 0
    assert context.client.list_objects_v2(
        Bucket=context.bucket, Prefix=target_prefix
    ).get("Contents", []) == []

    context.client.delete_object(Bucket=context.bucket, Key=neighbor_key)
    assert context.list_run_keys() == []
    print("Exact prefix LIST ....... PASS")
    print("Small-page pagination ... PASS")
    print("Neighbor preserved ...... PASS")
    print("Final namespace empty ... PASS")
