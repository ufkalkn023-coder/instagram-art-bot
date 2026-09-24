from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import uuid

from botocore.exceptions import ClientError
import pytest

from src import history_tracker, publication_state
from tests.test_publication_state import safety_candidate, store_for
from tests.integration.r2_test_support import (
    R2IntegrationContext,
    assert_integration_test_key,
    assert_integration_test_prefix,
    assert_isolated_test_bucket,
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


def test_live_test_bucket_must_differ_from_both_production_buckets():
    environment = {
        "CLOUDFLARE_R2_ACCOUNT_ID": "account",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "test-key",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "test-secret",
        "CLOUDFLARE_R2_BUCKET_NAME": "disposable",
        "CLOUDFLARE_PRODUCTION_R2_BUCKET_NAME": "media",
        "CLOUDFLARE_PRODUCTION_STATE_R2_BUCKET_NAME": "state",
    }
    assert_isolated_test_bucket(environment)
    for production_bucket in ("media", "state"):
        with pytest.raises(ValueError, match="overlaps"):
            assert_isolated_test_bucket({**environment, "CLOUDFLARE_R2_BUCKET_NAME": production_bucket})
    with pytest.raises(ValueError, match="Missing"):
        assert_isolated_test_bucket({**environment, "CLOUDFLARE_PRODUCTION_STATE_R2_BUCKET_NAME": ""})


def test_live_test_bucket_rejects_known_media_bucket_when_reference_is_wrong():
    environment = {
        "CLOUDFLARE_R2_ACCOUNT_ID": "account",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "test-key",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "test-secret",
        "CLOUDFLARE_R2_BUCKET_NAME": "instagram-art-bot",
        "CLOUDFLARE_PRODUCTION_R2_BUCKET_NAME": "wrong-media-reference",
        "CLOUDFLARE_PRODUCTION_STATE_R2_BUCKET_NAME": "state",
    }
    with pytest.raises(ValueError, match="production bucket"):
        assert_isolated_test_bucket(environment)


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
    state = safety_candidate()
    store, client = store_for(state)
    with pytest.raises(publication_state.StateValidationError, match="may not create"):
        history_tracker._upload_history({"_safety_state": state}, None)
    assert client.puts == 0
    with pytest.raises(publication_state.StateConflictError):
        store.create_initial(publication_state.SAFETY_KEY, state)
    assert client.puts == 1


def test_history_load_fails_closed_when_existing_object_has_no_etag(monkeypatch):
    class Client:
        def get_object(self, **kwargs):
            return {"Body": BytesIO(b'{}')}

    config = publication_state.StateConfiguration("account", "state", "key", "secret")
    store = publication_state.PublicationStateStore(config, Client())

    with pytest.raises(publication_state.StateValidationError, match="strong ETag"):
        store.load_safety()


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

    config = publication_state.StateConfiguration("account", "state", "key", "secret")
    store = publication_state.PublicationStateStore(config, Client())

    with pytest.raises(publication_state.StateConflictError) as captured:
        store._conditional_put(
            publication_state.SAFETY_KEY, safety_candidate(), '"stale"'
        )
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
    assert "10 skipped" in result.stdout
