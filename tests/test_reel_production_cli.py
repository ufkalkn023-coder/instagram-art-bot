"""Structure and CLI contract tests for the scheduled Reel production workflow."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import produce_reel, reconcile_reels

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "instagram_reels.yml"


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file()


def test_workflow_has_dispatch_with_explicit_confirmation_phrase():
    text = _workflow_text()
    assert "workflow_dispatch:" in text
    assert "confirm_publish:" in text
    assert "PUBLISH_REEL_TO_INSTAGRAM" in text
    assert "required: true" in text


def test_workflow_schedule_cron_is_gated_by_repository_variable():
    text = _workflow_text()
    assert "cron: \"0 7,12,17,22 * * *\"" in text
    assert "vars.ARTFOLIO_REEL_SCHEDULE_ENABLED == 'true'" in text


def test_workflow_uses_shared_carousel_production_concurrency_group():
    text = _workflow_text()
    assert "group: instagram-bot" in text
    assert "cancel-in-progress: false" in text


def test_workflow_checks_out_pinned_reels_repository_without_floating_main():
    text = _workflow_text()
    assert "repository: ufkalkn023-coder/artfolio-reels" in text
    assert "ref: ${{ vars.ARTFOLIO_REELS_PRODUCTION_REF }}" in text
    assert "path: artfolio-reels" in text
    assert "ref: main" not in text


def test_workflow_validates_pinned_ref_shape_before_checkout():
    text = _workflow_text()
    validation_index = text.index("Validate pinned artfolio-reels ref")
    checkout_index = text.index("repository: ufkalkn023-coder/artfolio-reels")
    assert validation_index < checkout_index
    assert "^[0-9a-f]{40}$" in text
    assert "^stable-" in text


def test_workflow_node_version_comes_from_reels_checkout():
    text = _workflow_text()
    assert "node-version-file:" in text
    assert "artfolio-reels/.node-version" in text
    assert "node-version: 20" not in text
    assert "node-version: 24" not in text
    assert "node-version: '20'" not in text
    assert "node-version: '24'" not in text


def test_workflow_maps_one_gemini_secret_to_both_env_names():
    text = _workflow_text()
    assert "GOOGLE_GEMINI_API_KEY: ${{ secrets.GOOGLE_GEMINI_API_KEY }}" in text
    assert "GEMINI_API_KEY: ${{ secrets.GOOGLE_GEMINI_API_KEY }}" in text


def test_workflow_requires_all_publication_secrets():
    text = _workflow_text()
    for secret in (
        "INSTAGRAM_ACCOUNT_ID",
        "INSTAGRAM_ACCESS_TOKEN",
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_BUCKET_NAME",
        "CLOUDFLARE_R2_PUBLIC_URL",
        "SMITHSONIAN_API_KEY",
        "EUROPEANA_API_KEY",
    ):
        assert f"secrets.{secret}" in text


def test_workflow_runs_preflight_reconciliation_then_single_production_step():
    text = _workflow_text()
    assert "python -m compileall" in text
    assert "pytest -q" in text
    assert "--validate-production-config" in text
    assert text.count("scripts/reconcile_reels.py") == 1
    assert text.count("scripts/produce_reel.py") == 1
    assert text.index("scripts/reconcile_reels.py") < text.index(
        "scripts/produce_reel.py"
    )


def test_workflow_never_uploads_reel_mp4_or_uses_retry_actions():
    text = _workflow_text()
    assert "upload-artifact" not in text
    assert "reel.mp4" not in text
    assert "actions/retry" not in text
    assert "nick-fields/retry" not in text


def test_workflow_installs_ffmpeg_before_production():
    text = _workflow_text()
    assert "Install FFmpeg" in text
    assert "apt-get install -y ffmpeg" in text
    assert "ffmpeg -version" in text
    assert "ffprobe -version" in text
    install_index = text.index("Install FFmpeg")
    produce_index = text.index("scripts/produce_reel.py")
    assert install_index < produce_index
    assert text.count("scripts/produce_reel.py") == 1


def _set_production_credentials(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "account")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "token")


def test_produce_reel_cli_delegates_once_and_reports_success(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_production_credentials(monkeypatch)
    calls = []

    def fake_produce_and_publish(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            canonical_id="met_123",
            release_directory=Path("/tmp/reels/output/releases/met_123"),
            publication=SimpleNamespace(
                id="12345678-1234-4234-8234-123456789abc",
                media_id="media-1",
            ),
        )

    monkeypatch.setattr(produce_reel, "produce_and_publish_reel", fake_produce_and_publish)

    assert produce_reel.main(["--artfolio-reels-root", "/tmp/artfolio-reels"]) == 0

    assert len(calls) == 1
    assert calls[0]["reels_repository"] == Path("/tmp/artfolio-reels")
    assert calls[0]["account_id"] == "account"
    assert calls[0]["access_token"] == "token"
    assert "media-1" in caplog.text
    assert "token" not in caplog.text


def test_produce_reel_cli_fails_closed_without_credentials(monkeypatch, caplog):
    monkeypatch.delenv("INSTAGRAM_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    delegate = Mock()
    monkeypatch.setattr(produce_reel, "produce_and_publish_reel", delegate)

    assert produce_reel.main(["--artfolio-reels-root", "/tmp/artfolio-reels"]) == 1

    delegate.assert_not_called()
    assert "INSTAGRAM_ACCOUNT_ID" in caplog.text


def test_produce_reel_cli_failure_is_sanitized_and_returns_nonzero(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_production_credentials(monkeypatch)

    def failing_produce(**kwargs):
        raise RuntimeError(
            "unexpected failure access_token=SUPERSECRET caption=A verified caption."
        )

    monkeypatch.setattr(produce_reel, "produce_and_publish_reel", failing_produce)

    assert produce_reel.main(["--artfolio-reels-root", "/tmp/artfolio-reels"]) == 1

    assert "RuntimeError" in caplog.text
    assert "SUPERSECRET" not in caplog.text
    assert "A verified caption." not in caplog.text


def test_produce_reel_cli_reports_safe_domain_error_messages(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_production_credentials(monkeypatch)
    from src.reel_production import ReelProductionSelectionError

    def failing_produce(**kwargs):
        raise ReelProductionSelectionError(
            "No eligible Reel candidate survived acquisition and selection"
        )

    monkeypatch.setattr(produce_reel, "produce_and_publish_reel", failing_produce)

    assert produce_reel.main(["--artfolio-reels-root", "/tmp/artfolio-reels"]) == 1

    assert "ReelProductionSelectionError" in caplog.text
    assert "No eligible Reel candidate" in caplog.text


def test_reconcile_reels_cli_delegates_once_and_reports_summary(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "token")
    calls = []

    def fake_reconcile(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            inspected=1,
            confirmed_published=1,
            confirmed_not_published=0,
            still_ambiguous=0,
            errors=0,
            cleanup_inspected=1,
            cleanup_deleted=1,
            cleanup_failures=0,
            results=(),
        )

    monkeypatch.setattr(
        reconcile_reels, "reconcile_reel_publications", fake_reconcile
    )

    assert reconcile_reels.main([]) == 0

    assert len(calls) == 1
    assert calls[0]["access_token"] == "token"
    assert "confirmed_published=1" in caplog.text
    assert "token" not in caplog.text


def test_reconcile_reels_cli_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    delegate = Mock()
    monkeypatch.setattr(reconcile_reels, "reconcile_reel_publications", delegate)

    assert reconcile_reels.main([]) == 1

    delegate.assert_not_called()


def test_reconcile_reels_cli_failure_is_sanitized_and_returns_nonzero(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "token")

    def failing_reconcile(**kwargs):
        raise RuntimeError("history unavailable token=SUPERSECRET")

    monkeypatch.setattr(reconcile_reels, "reconcile_reel_publications", failing_reconcile)

    assert reconcile_reels.main([]) == 1

    assert "RuntimeError" in caplog.text
    assert "SUPERSECRET" not in caplog.text
