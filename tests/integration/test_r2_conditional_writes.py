"""Live Cloudflare R2 CAS verification; never enabled by credentials alone."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time

from botocore.exceptions import ClientError
import pytest

from src import history_tracker, publication_state
from tests.test_publication_state import safety_candidate
from tests.integration.r2_test_support import (
    R2IntegrationContext,
    assert_integration_test_key,
    assert_isolated_test_bucket,
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
    try:
        assert_isolated_test_bucket()
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)

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


def test_concurrent_writers_cannot_both_win(r2_context):
    key = r2_context.key("raw/concurrent-cas.json")
    _put(r2_context, key, b'{"version":"initial"}', IfNoneMatch="*")
    etag = r2_context.client.head_object(Bucket=r2_context.bucket, Key=key)["ETag"]
    # Avoid the documented same-key write limit obscuring the first CAS attempt.
    time.sleep(1.2)
    start = Barrier(2)

    def write(body: bytes) -> tuple[bytes, int]:
        start.wait()
        try:
            r2_context.client.put_object(
                Bucket=r2_context.bucket, Key=key, Body=body,
                ContentType="application/json", IfMatch=etag,
            )
        except ClientError as error:
            return body, error.response["ResponseMetadata"]["HTTPStatusCode"]
        return body, 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (b'{"writer":1}', b'{"writer":2}')))
    winners = [body for body, status in results if status == 200]
    losers = [status for _, status in results if status != 200]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0] in {412, 429}
    assert _get_bytes(r2_context, key) == winners[0]
    if losers[0] == 429:
        time.sleep(1.2)
        with pytest.raises(ClientError) as captured:
            r2_context.client.put_object(
                Bucket=r2_context.bucket, Key=key, Body=b'{"writer":"retry"}',
                ContentType="application/json", IfMatch=etag,
            )
        _assert_precondition_failed(captured.value)
    print("Concurrent CAS ......... PASS")


def test_missing_object_classification(r2_context):
    key = r2_context.key("raw/intentionally-missing.json")

    with pytest.raises(ClientError) as captured:
        r2_context.client.get_object(Bucket=r2_context.bucket, Key=key)

    assert history_tracker._is_missing_object(captured.value)
    assert captured.value.response.get("ResponseMetadata", {}).get(
        "HTTPStatusCode"
    ) == 404
    print("Missing object .......... PASS")


class NamespacedStateClient:
    """Map fixed application keys into one guarded integration namespace."""

    def __init__(self, context: R2IntegrationContext, subpath: str):
        self.context = context
        self.subpath = subpath

    def key(self, key: str) -> str:
        return self.context.key(f"{self.subpath}/{key}")

    def get_object(self, *, Bucket, Key):
        return self.context.client.get_object(Bucket=Bucket, Key=self.key(Key))

    def put_object(self, *, Bucket, Key, **kwargs):
        test_key = self.key(Key)
        self.context.wait_for_write_slot(test_key)
        return self.context.client.put_object(Bucket=Bucket, Key=test_key, **kwargs)


def _state_store(context: R2IntegrationContext, subpath: str):
    config = publication_state.StateConfiguration("integration", context.bucket, "test", "test")
    client = NamespacedStateClient(context, subpath)
    return publication_state.PublicationStateStore(config, client)


def test_application_v2_safety_and_receipt_cas(r2_context):
    store = _state_store(r2_context, "application")
    safety = safety_candidate()
    receipts = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "integration-test",
        "source_sha256": "a" * 64, "record_count": 0, "records": [],
    })
    store.create_initial(publication_state.SAFETY_KEY, safety)
    store.create_initial(publication_state.RECEIPTS_KEY, receipts)
    loaded, etag = store.load_safety()
    assert len(loaded.published_artwork_protection.entries) == 634

    candidate = loaded.model_dump(mode="json")
    candidate["generation"] += 1
    candidate = publication_state.seal(candidate)
    store.update_safety(candidate, etag)
    with pytest.raises(publication_state.StateConflictError):
        store.update_safety(candidate, etag)
    after, _ = store.load_safety()
    ledger, _ = store.load_receipts()
    assert after.generation == 2
    assert len(after.published_artwork_protection.entries) == 634
    assert ledger.record_count == 0
    print("Application v2 CAS ....... PASS")
    print("Protected IDs preserved .. PASS")


def test_partial_bootstrap_fails_closed_then_clean_recovery_succeeds(r2_context):
    store = _state_store(r2_context, "partial-bootstrap")
    store.require_uninitialized()
    safety = safety_candidate()
    receipts = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "integration-test",
        "source_sha256": "a" * 64, "record_count": 0, "records": [],
    })

    # Simulate the first bootstrap write succeeding and the second failing.
    store.create_initial(publication_state.RECEIPTS_KEY, receipts)
    with pytest.raises(publication_state.StateConflictError):
        store.require_uninitialized()
    with pytest.raises(publication_state.StateValidationError):
        store.load_safety()
    assert store.load_receipts()[0].record_count == 0

    # An operator can remove only the disposable partial object, then retry.
    client = store.client
    key = client.key(publication_state.RECEIPTS_KEY)
    assert_integration_test_key(key, r2_context.prefix)
    r2_context.client.delete_object(Bucket=r2_context.bucket, Key=key)
    store.require_uninitialized()
    store.create_initial(publication_state.RECEIPTS_KEY, receipts)
    store.create_initial(publication_state.SAFETY_KEY, safety)
    assert store.load_receipts()[0].record_count == 0
    assert len(store.load_safety()[0].published_artwork_protection.entries) == 634
    with pytest.raises(publication_state.StateConflictError):
        store.require_uninitialized()
    print("Partial bootstrap gate ... PASS")
    print("Recovery bootstrap ...... PASS")


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
