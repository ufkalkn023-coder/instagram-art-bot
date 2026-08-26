import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import main
from src import gemini_ai, history_tracker, image_processor, instagram_poster
from src.production_config import (
    OPTIONAL_INTEGRATION_VARIABLES,
    REQUIRED_PRODUCTION_VARIABLES,
)


def _resolution(result=main.SinglePostResolutionCode.READY):
    return main.SinglePostResolution(
        result=result,
        attempted=0,
        zero_touch=0,
        compatibility_processed=0,
        single_ineligible=0,
        fatal_failures=0,
        diagnostics=(),
    )


def _set_required_environment(monkeypatch):
    for name in REQUIRED_PRODUCTION_VARIABLES:
        monkeypatch.setenv(name, "configured")


def _clear_environment(monkeypatch):
    for name in REQUIRED_PRODUCTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for names in OPTIONAL_INTEGRATION_VARIABLES.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)


def _mock_reconciliation(monkeypatch, calls=None):
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **kwargs: (
            calls.append("reconcile") if calls is not None else None
        )
        or SimpleNamespace(
            inspected=0,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=0,
            errors=0,
        ),
    )


def test_dry_run_main_does_not_require_production_credentials(monkeypatch):
    _clear_environment(monkeypatch)
    calls = []
    monkeypatch.setattr(
        main.history_tracker,
        "recover_stale_reservations",
        lambda: pytest.fail("dry-run recovered production history"),
    )
    monkeypatch.setattr(
        main,
        "run_single_post",
        lambda args: calls.append(args.dry_run) or _resolution(),
    )

    assert main.main(["--dry-run", "--mode", "single"]) == 0
    assert calls == [True]


def test_missing_required_configuration_fails_before_production_path(
    monkeypatch, caplog
):
    _clear_environment(monkeypatch)
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "must-not-appear")
    calls = []
    monkeypatch.setattr(
        main.history_tracker,
        "recover_stale_reservations",
        lambda: calls.append("recover"),
    )
    monkeypatch.setattr(
        main,
        "run_single_post",
        lambda args: calls.append("single") or _resolution(),
    )
    caplog.set_level(logging.INFO, logger=main.__name__)

    assert main.main(["--mode", "single"]) == 1
    assert calls == []
    assert "Missing required production configuration" in caplog.text
    assert "must-not-appear" not in caplog.text


def test_optional_integrations_do_not_fail_production_startup(monkeypatch):
    _set_required_environment(monkeypatch)
    for names in OPTIONAL_INTEGRATION_VARIABLES.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    calls = []
    _mock_reconciliation(monkeypatch, calls)
    monkeypatch.setattr(
        main,
        "run_single_post",
        lambda args: calls.append("single") or _resolution(),
    )

    assert main.main(["--mode", "single"]) == 0
    assert calls == ["reconcile", "single"]


def test_config_only_gate_exits_before_history_or_acquisition(monkeypatch):
    _set_required_environment(monkeypatch)
    monkeypatch.setattr(
        main.history_tracker,
        "recover_stale_reservations",
        lambda: pytest.fail("config gate read production history"),
    )
    monkeypatch.setattr(
        main,
        "run_single_post",
        lambda args: pytest.fail("config gate entered acquisition"),
    )

    assert main.main(["--validate-production-config"]) == 0


def test_no_candidate_is_clean_no_publish_outcome(monkeypatch, caplog):
    _set_required_environment(monkeypatch)
    _mock_reconciliation(monkeypatch)
    monkeypatch.setattr(
        main,
        "run_single_post",
        lambda args: _resolution(
            main.SinglePostResolutionCode.NO_SINGLE_POST_PUBLISHABLE_CANDIDATE
        ),
    )
    caplog.set_level(logging.INFO, logger=main.__name__)

    assert main.main(["--mode", "single"]) == 0
    assert "production_no_publish mode=single" in caplog.text
    assert "production_success mode=single" not in caplog.text


def test_publish_failure_returns_failure_status(monkeypatch):
    _set_required_environment(monkeypatch)
    _mock_reconciliation(monkeypatch)
    monkeypatch.setattr(
        main,
        "run_single_post",
        lambda args: (_ for _ in ()).throw(
            instagram_poster.InstagramAPIError("publish rejected")
        ),
    )

    assert main.main(["--mode", "single"]) == 1


def test_explicit_mode_does_not_depend_on_wall_clock():
    carousel_hour = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    single_args = SimpleNamespace(mode="single", force_carousel=False)
    carousel_args = SimpleNamespace(mode="carousel", force_carousel=False)

    assert main._resolve_production_mode(single_args, carousel_hour) is main.ProductionMode.SINGLE
    assert (
        main._resolve_production_mode(carousel_args, carousel_hour)
        is main.ProductionMode.CAROUSEL
    )


def test_cleanup_removes_only_new_production_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(main.config, "DATA_DIR", str(tmp_path))
    existing = tmp_path / "output_post.jpg"
    existing.write_bytes(b"preserve")
    snapshot = main._snapshot_generated_artifacts()
    generated = tmp_path / "raw_artwork.jpg"
    generated.write_bytes(b"remove")
    qc_directory = tmp_path / "qc_carousels" / "review"
    qc_directory.mkdir(parents=True)
    qc_artifact = qc_directory / "carousel_01.jpg"
    qc_artifact.write_bytes(b"preserve")

    main._cleanup_new_generated_artifacts(snapshot)

    assert existing.read_bytes() == b"preserve"
    assert not generated.exists()
    assert qc_artifact.read_bytes() == b"preserve"


def test_outbound_sdk_clients_have_explicit_bounded_timeouts(monkeypatch):
    assert history_tracker.R2_CLIENT_CONFIG.connect_timeout == 10
    assert history_tracker.R2_CLIENT_CONFIG.read_timeout == 30
    assert history_tracker.R2_CLIENT_CONFIG.retries["total_max_attempts"] == 1
    assert image_processor.R2_CLIENT_CONFIG.connect_timeout == 10
    assert image_processor.R2_CLIENT_CONFIG.read_timeout == 30
    assert image_processor.R2_CLIENT_CONFIG.retries["total_max_attempts"] == 1

    captured = {}
    sentinel = object()

    def client_factory(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(gemini_ai.genai, "Client", client_factory)

    assert gemini_ai._create_client("secret") is sentinel
    options = captured["http_options"]
    assert options.timeout == gemini_ai.GEMINI_HTTP_TIMEOUT_MILLISECONDS
    assert options.retry_options.attempts == 1


def test_r2_upload_retries_only_transient_failures():
    permanent = ClientError(
        {
            "Error": {"Code": "AccessDenied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    transient = ClientError(
        {
            "Error": {"Code": "ServiceUnavailable"},
            "ResponseMetadata": {"HTTPStatusCode": 503},
        },
        "PutObject",
    )

    assert not image_processor._is_transient_r2_upload_error(permanent)
    assert image_processor._is_transient_r2_upload_error(transient)
