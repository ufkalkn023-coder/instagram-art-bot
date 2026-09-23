"""Read-only production checks for the normal carousel entry point."""

from botocore.exceptions import ClientError, EndpointConnectionError
import pytest
from types import SimpleNamespace

import main
from src import history_tracker, instagram_poster, production_config
from src.production_config import ProductionConfigurationError


def _configure(monkeypatch, *, public_url="https://media.example"):
    for name in (
        "INSTAGRAM_ACCOUNT_ID",
        "INSTAGRAM_ACCESS_TOKEN",
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_BUCKET_NAME",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("CLOUDFLARE_R2_PUBLIC_URL", public_url)
    monkeypatch.setenv("ARTFOLIO_RIGHTS_POLICY", "strict_public_domain")
    monkeypatch.setattr(
        instagram_poster,
        "validate_instagram_account_access",
        lambda _account_id, _access_token: None,
        raising=False,
    )


def _client_error(code, status, operation="GetObject"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "secret-must-not-appear"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def test_preflight_reads_authoritative_history_without_mutation(monkeypatch):
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: (calls.append("read") or {"posted_artworks": []}, '"etag"'),
    )
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda *_args: pytest.fail("preflight mutated history"),
    )

    assert production_config.validate_carousel_production_preflight() == {"gemini": "disabled"}
    assert calls == ["read"]


def test_preflight_uses_normalized_instagram_credentials(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", " account-1 ")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", " token ")
    checked = []
    monkeypatch.setattr(
        instagram_poster,
        "validate_instagram_account_access",
        lambda account_id, access_token: checked.append((account_id, access_token)),
    )
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: ({"posted_artworks": []}, '"etag"'),
    )

    validate = production_config.validate_carousel_production_preflight
    assert validate() == {"gemini": "disabled"}
    assert checked == [("account-1", "token")]


def test_preflight_rejects_missing_authoritative_history(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: ({"posted_artworks": []}, None),
    )

    with pytest.raises(ProductionConfigurationError, match="posted_history.json.*missing"):
        production_config.validate_carousel_production_preflight()


def test_preflight_checks_instagram_access_before_history_read(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        instagram_poster,
        "validate_instagram_account_access",
        lambda *_args: (_ for _ in ()).throw(
            instagram_poster.InstagramAuthError("Instagram account access unavailable")
        ),
    )
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: pytest.fail("unavailable Instagram account reached R2"),
    )

    with pytest.raises(instagram_poster.InstagramAuthError):
        production_config.validate_carousel_production_preflight()


@pytest.mark.parametrize("url", ["http://media.example", "https://user:pass@media.example", "https://media.example/?token=secret", "https://media.example/#", "https://bad host.example", "https://localhost"])
def test_preflight_rejects_unsafe_public_media_url_before_r2_read(monkeypatch, url):
    _configure(monkeypatch, public_url=url)
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: pytest.fail("invalid URL reached R2"),
    )

    with pytest.raises(ProductionConfigurationError, match="CLOUDFLARE_R2_PUBLIC_URL"):
        production_config.validate_carousel_production_preflight()


@pytest.mark.parametrize(
    "history",
    [
        {"posted_artworks": "invalid"},
        {"posted_artworks": [{"status": "PUBLISHED"}]},
        {"posted_artworks": [{"id": "aic_1", "status": "UNKNOWN"}]},
        {"posted_artworks": [{"id": "aic_1"}, {"id": "artic_1"}]},
        {"posted_artworks": [{"id": "aic_1", "status": "PENDING"}]},
        {
            "posted_artworks": [
                {
                    "id": "aic_1",
                    "status": "PUBLISHING",
                    "reserved_at": "2026-09-23T12:00:00Z",
                }
            ]
        },
    ],
)
def test_preflight_rejects_history_that_could_weaken_duplicate_protection(
    monkeypatch, history
):
    _configure(monkeypatch)
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: (history, '"etag"'),
    )

    with pytest.raises(history_tracker.CorruptedHistoryError):
        production_config.validate_carousel_production_preflight()


def test_preflight_keeps_legacy_history_readable(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        history_tracker,
        "load_history_with_etag",
        lambda: ({"posted_artworks": [{"id": "artic_84774"}]}, '"etag"'),
    )

    assert production_config.validate_carousel_production_preflight() == {"gemini": "disabled"}


def test_missing_bucket_is_not_treated_as_missing_history_object(monkeypatch):
    class Client:
        def get_object(self, **_kwargs):
            raise _client_error("NoSuchBucket", 404)

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")

    with pytest.raises(RuntimeError) as caught:
        history_tracker.load_history_with_etag()
    message = str(caught.value)
    assert "operation=GetObject" in message
    assert "code=NoSuchBucket" in message
    assert "http_status=404" in message
    assert "bucket=art-bucket" in message
    assert "key=posted_history.json" in message
    assert "secret-must-not-appear" not in message


@pytest.mark.parametrize("code", ["AccessDenied", "SignatureDoesNotMatch"])
def test_history_access_failure_has_safe_actionable_diagnostics(monkeypatch, code):
    class Client:
        def get_object(self, **_kwargs):
            raise _client_error(code, 403)

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")

    with pytest.raises(RuntimeError) as caught:
        history_tracker.load_history_with_etag()
    message = str(caught.value)
    assert f"code={code}" in message
    assert "http_status=403" in message
    assert "secret-must-not-appear" not in message


def test_history_endpoint_failure_reports_type_without_endpoint_url(monkeypatch):
    class Client:
        def get_object(self, **_kwargs):
            raise EndpointConnectionError(
                endpoint_url="https://r2.example/?token=secret-must-not-appear"
            )

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")

    with pytest.raises(RuntimeError) as caught:
        history_tracker.load_history_with_etag()
    message = str(caught.value)
    assert "operation=GetObject" in message
    assert "error_type=EndpointConnectionError" in message
    assert "secret-must-not-appear" not in message


def test_history_client_initialization_failure_keeps_bucket_context(monkeypatch):
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")
    monkeypatch.setattr(
        history_tracker,
        "_get_s3_client",
        lambda: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://secret-must-not-appear")
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        history_tracker.load_history_with_etag()
    assert "bucket=art-bucket" in str(caught.value)
    assert "error_type=EndpointConnectionError" in str(caught.value)
    assert "secret-must-not-appear" not in str(caught.value)


def test_history_write_access_denied_reports_safe_operation_context(monkeypatch):
    class Client:
        def put_object(self, **_kwargs):
            raise _client_error("AccessDenied", 403, "PutObject")

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")

    with pytest.raises(RuntimeError) as caught:
        history_tracker._upload_history({"posted_artworks": []}, '"etag"')
    message = str(caught.value)
    assert "operation=PutObject" in message
    assert "code=AccessDenied" in message
    assert "bucket=art-bucket" in message
    assert "key=posted_history.json" in message
    assert "secret-must-not-appear" not in message


def test_history_write_client_initialization_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")
    monkeypatch.setattr(
        history_tracker,
        "_get_s3_client",
        lambda: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://secret-must-not-appear")
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        history_tracker._upload_history({"posted_artworks": []}, '"etag"')
    message = str(caught.value)
    assert "operation=PutObject" in message
    assert "bucket=art-bucket" in message
    assert "error_type=EndpointConnectionError" in message
    assert "secret-must-not-appear" not in message


def test_invalid_r2_client_configuration_is_not_treated_as_fresh_history(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_R2_ACCOUNT_ID", "configured")
    monkeypatch.setenv("CLOUDFLARE_R2_ACCESS_KEY_ID", "configured")
    monkeypatch.setenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "configured")
    monkeypatch.setenv("CLOUDFLARE_R2_BUCKET_NAME", "art-bucket")
    monkeypatch.setattr(
        history_tracker,
        "_get_s3_client",
        lambda: (_ for _ in ()).throw(ValueError("secret-must-not-appear")),
    )

    with pytest.raises(RuntimeError) as caught:
        history_tracker.load_history_with_etag()
    assert "R2 client configuration invalid" in str(caught.value)
    assert "secret-must-not-appear" not in str(caught.value)


def test_missing_key_remains_distinct_from_access_failure(monkeypatch):
    class Client:
        def get_object(self, **_kwargs):
            raise _client_error("NoSuchKey", 404)

    monkeypatch.setattr(history_tracker, "_get_s3_client", Client)
    monkeypatch.setattr(history_tracker, "_get_bucket_name", lambda: "art-bucket")

    with pytest.raises(RuntimeError) as caught:
        history_tracker.load_history_with_etag()
    assert "posted_history.json is missing" in str(caught.value)
    assert "code=NoSuchKey" in str(caught.value)


def test_cli_preflight_exits_before_reconciliation_and_acquisition(monkeypatch):
    monkeypatch.setattr(main, "validate_carousel_production_preflight", lambda: {"gemini": "disabled"})
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **_kwargs: pytest.fail("preflight reconciled"),
    )
    monkeypatch.setattr(
        main,
        "run_carousel_post",
        lambda _args: pytest.fail("preflight acquired artwork"),
    )

    assert main.main(["--preflight-carousel"]) == 0


def test_production_preflight_failure_stops_before_reconciliation(monkeypatch):
    monkeypatch.setattr(
        main,
        "validate_carousel_production_preflight",
        lambda: (_ for _ in ()).throw(ProductionConfigurationError("R2 unavailable")),
    )
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **_kwargs: pytest.fail("failed preflight reconciled"),
    )
    monkeypatch.setattr(
        main,
        "run_carousel_post",
        lambda _args: pytest.fail("failed preflight acquired artwork"),
    )

    assert main.main(["--mode", "carousel"]) == 1


def test_reconciliation_errors_stop_new_carousel_acquisition(monkeypatch):
    monkeypatch.setattr(main, "validate_carousel_production_preflight", lambda: {})
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **_kwargs: SimpleNamespace(
            inspected=1,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=0,
            errors=1,
            cleanup_inspected=0,
            cleanup_deleted=0,
            cleanup_failures=0,
        ),
    )
    monkeypatch.setattr(
        main,
        "run_carousel_post",
        lambda _args: pytest.fail("reconciliation error reached acquisition"),
    )

    assert main.main(["--mode", "carousel"]) == 1
