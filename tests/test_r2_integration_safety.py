from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import uuid

from botocore.exceptions import ClientError
import pytest

from src import history_tracker
from tests.integration.r2_test_support import (
    R2IntegrationContext,
    assert_integration_test_key,
    assert_integration_test_prefix,
    integration_enabled,
    make_run_prefix,
)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )


def test_integration_opt_in_is_exact_and_not_implied_by_credentials():
    credentials_only = {
        "CLOUDFLARE_R2_ACCOUNT_ID": "account",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "access",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "secret",
        "CLOUDFLARE_R2_BUCKET_NAME": "bucket",
    }

    assert not integration_enabled(credentials_only)
    assert not integration_enabled({"ARTFOLIO_RUN_R2_INTEGRATION": "true"})
    assert integration_enabled({"ARTFOLIO_RUN_R2_INTEGRATION": "1"})


def test_integration_key_guard_accepts_only_exact_uuid_run_namespace():
    prefix = make_run_prefix(uuid.UUID("12345678-1234-4234-8234-123456789abc"))
    assert_integration_test_prefix(prefix)
    assert_integration_test_key(f"{prefix}raw/object.json", prefix)

    unsafe = (
        "posted_history.json",
        "images/production.jpg",
        "artfolio-integration-tests/not-a-uuid/object.json",
        f"{prefix[:-1]}-collision/object.json",
        prefix,
    )
    for key in unsafe:
        with pytest.raises(ValueError, match="Refusing"):
            assert_integration_test_key(key, prefix)


def test_cleanup_fails_closed_before_listing_unsafe_prefix():
    class ForbiddenClient:
        def list_objects_v2(self, **kwargs):
            pytest.fail("unsafe cleanup reached R2 client")

    with pytest.raises(ValueError, match="exact integration run prefix"):
        R2IntegrationContext(
            client=ForbiddenClient(),
            bucket="bucket",
            prefix="artfolio-integration-tests/",
        )


def test_etag_is_preserved_as_an_opaque_quoted_validator():
    simple = '"0123456789abcdef"'
    multipart = '"opaque-value-7"'

    assert history_tracker._validated_etag(simple) == simple
    assert history_tracker._validated_etag(multipart) == multipart
    for malformed in (None, "", "unquoted", '"unterminated', '"bad\nvalue"'):
        with pytest.raises(RuntimeError, match="ETag"):
            history_tracker._validated_etag(malformed)


def test_history_put_uses_create_or_match_precondition(monkeypatch):
    calls = []

    class Client:
        def put_object(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "bucket")

    history_tracker._upload_history({"posted_artworks": []}, None)
    history_tracker._upload_history({"posted_artworks": []}, '"opaque-etag"')

    assert calls[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in calls[0]
    assert calls[1]["IfMatch"] == '"opaque-etag"'
    assert "IfNoneMatch" not in calls[1]


def test_history_load_fails_closed_when_existing_object_has_no_etag(monkeypatch):
    class Client:
        def get_object(self, **kwargs):
            return {"Body": BytesIO(b'{"posted_artworks":[]}')}

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "bucket")

    with pytest.raises(RuntimeError, match="missing a valid ETag"):
        history_tracker.load_history_with_etag()


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    (
        ("PreconditionFailed", 412, True),
        ("Unknown", 412, True),
        ("AccessDenied", 403, False),
        ("ServiceUnavailable", 503, False),
    ),
)
def test_precondition_failure_classification(code, status, expected):
    assert history_tracker._is_precondition_failed(
        _client_error(code, status)
    ) is expected


def test_history_precondition_failure_is_not_reclassified_as_transient(
    monkeypatch,
):
    class Client:
        def put_object(self, **kwargs):
            raise _client_error("PreconditionFailed", 412)

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "bucket")

    with pytest.raises(history_tracker.ConcurrentWriteError) as captured:
        history_tracker._upload_history({"posted_artworks": []}, '"stale"')
    assert isinstance(captured.value.__cause__, ClientError)


def test_live_module_is_skipped_without_explicit_opt_in():
    environment = os.environ.copy()
    environment.pop("ARTFOLIO_RUN_R2_INTEGRATION", None)
    test_file = (
        Path(__file__).parent
        / "integration"
        / "test_r2_conditional_writes.py"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_file)],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 skipped" in result.stdout
