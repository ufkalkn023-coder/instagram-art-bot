"""Read-only production gates for the versioned durable publication state."""

from types import SimpleNamespace
from pathlib import Path

import pytest

import main
from scripts.bootstrap_publication_recovery import build_candidate
from src import history_tracker, instagram_poster, production_config, publication_state
from src.production_config import ProductionConfigurationError
from tests.test_publication_state import safety_candidate, store_for
from tests.test_receipt_timestamp_compat import recovered_receipt


def _configure(monkeypatch, *, public_url="https://media.example"):
    values = {
        "INSTAGRAM_ACCOUNT_ID": "account-1",
        "INSTAGRAM_ACCESS_TOKEN": "token",
        "CLOUDFLARE_R2_ACCOUNT_ID": "account",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "media-key",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "media-secret",
        "CLOUDFLARE_R2_BUCKET_NAME": "media",
        "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID": "state-key",
        "CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY": "state-secret",
        "CLOUDFLARE_STATE_R2_BUCKET_NAME": "state",
        "CLOUDFLARE_R2_PUBLIC_URL": public_url,
        "ARTFOLIO_RIGHTS_POLICY": "strict_public_domain",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(instagram_poster, "validate_instagram_account_access", lambda *_: None)


def _fake_store(monkeypatch, *, safety=None, safety_present=True, receipts=True):
    state = publication_state.validate_safety_state(safety or safety_candidate())
    calls = []
    class Store:
        def load_safety(self):
            calls.append("safety-read")
            if not safety_present:
                raise publication_state.StateValidationError(
                    "RECOVERY_STATE_NOT_BOOTSTRAPPED: publication_safety_state.v2.json"
                )
            return state, '"etag"'
        def load_receipts(self):
            calls.append("receipts-read")
            if not receipts:
                raise publication_state.StateValidationError("Receipt ledger missing")
            return SimpleNamespace(generation=1, records=[]), '"receipt-etag"'
    monkeypatch.setattr(publication_state, "PublicationStateStore", Store)
    monkeypatch.setattr(
        publication_state,
        "validate_state_bucket_lifecycle",
        lambda _store: pytest.fail("runtime requested bucket lifecycle configuration"),
    )
    return calls


def test_preflight_reads_both_durable_objects_without_mutation(monkeypatch):
    _configure(monkeypatch)
    calls = _fake_store(monkeypatch)
    monkeypatch.setattr(history_tracker, "_upload_history", lambda *_: pytest.fail("preflight wrote state"))
    assert production_config.validate_carousel_production_preflight() == {"gemini": "disabled"}
    assert calls == ["safety-read", "receipts-read"]


def test_preflight_accepts_recovered_compact_offset_receipt_without_mutation(monkeypatch):
    _configure(monkeypatch)
    receipts = publication_state.seal({
        "schema_version": 2, "generation": 1, "source_artifact": "test-recovery",
        "source_sha256": "a" * 64, "record_count": 1,
        "records": [recovered_receipt("2026-08-24T14:39:56+0000")],
    })
    store, client = store_for(safety_candidate(), receipts)
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    assert production_config.validate_carousel_production_preflight() == {"gemini": "disabled"}
    assert client.puts == 0


def test_preflight_accepts_all_authoritative_recovered_receipts(monkeypatch):
    evidence = Path.home() / "Documents/UfukOS/900-Archive/Agent-Logs/Codex/carousel-history-recovery"
    if not evidence.exists():
        pytest.skip("external forensic artifacts unavailable")
    safety, receipts, report = build_candidate(evidence)
    assert report["publication_receipts"] == 68
    _configure(monkeypatch)
    store, client = store_for(safety, receipts)
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    assert production_config.validate_carousel_production_preflight() == {"gemini": "disabled"}
    assert client.puts == 0


def test_preflight_reports_unbootstrapped_state_before_instagram_or_mutation(monkeypatch):
    _configure(monkeypatch)
    calls = _fake_store(monkeypatch, safety_present=False)
    monkeypatch.setattr(
        instagram_poster, "validate_instagram_account_access",
        lambda *_: pytest.fail("missing state reached Instagram"),
    )
    with pytest.raises(publication_state.StateValidationError, match="RECOVERY_STATE_NOT_BOOTSTRAPPED"):
        production_config.validate_carousel_production_preflight()
    assert calls == ["safety-read"]


def test_preflight_normalizes_instagram_credentials(monkeypatch):
    _configure(monkeypatch)
    _fake_store(monkeypatch)
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", " account-1 ")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", " token ")
    seen = []
    monkeypatch.setattr(instagram_poster, "validate_instagram_account_access",
                        lambda account, token: seen.append((account, token)))
    production_config.validate_carousel_production_preflight()
    assert seen == [("account-1", "token")]


def test_preflight_rejects_missing_receipts(monkeypatch):
    _configure(monkeypatch)
    _fake_store(monkeypatch, receipts=False)
    with pytest.raises(publication_state.StateValidationError, match="ledger missing"):
        production_config.validate_carousel_production_preflight()


def test_preflight_rejects_missing_state_config(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("CLOUDFLARE_STATE_R2_BUCKET_NAME")
    with pytest.raises(ProductionConfigurationError, match="CLOUDFLARE_STATE_R2_BUCKET_NAME"):
        production_config.validate_carousel_production_preflight()


def test_preflight_rejects_same_media_and_state_bucket(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "media")
    with pytest.raises(ProductionConfigurationError, match="differ from media bucket"):
        production_config.validate_carousel_production_preflight()


@pytest.mark.parametrize("url", ["http://media.example", "https://user:pass@media.example", "https://media.example/?token=secret", "https://media.example/#", "https://bad host.example", "https://localhost"])
def test_preflight_rejects_unsafe_public_url_before_state_read(monkeypatch, url):
    _configure(monkeypatch, public_url=url)
    monkeypatch.setattr(publication_state, "PublicationStateStore",
                        lambda: pytest.fail("invalid URL reached durable state"))
    with pytest.raises(ProductionConfigurationError, match="CLOUDFLARE_R2_PUBLIC_URL"):
        production_config.validate_carousel_production_preflight()


def test_preflight_rejects_unresolved_live_ambiguity(monkeypatch):
    _configure(monkeypatch)
    state = safety_candidate()
    state["active_publication_state"]["posted_artworks"].append({
        "id": "aic_999999", "publication_id": "unit-1", "status": "AMBIGUOUS",
        "reserved_at": "2026-09-23T12:00:00Z", "ambiguity_reason": "unknown",
    })
    _fake_store(monkeypatch, safety=publication_state.seal(state))
    with pytest.raises(ProductionConfigurationError, match="Unresolved live feed"):
        production_config.validate_carousel_production_preflight()


def test_legacy_history_is_not_a_production_fallback(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("CLOUDFLARE_STATE_R2_BUCKET_NAME")
    monkeypatch.delenv("CLOUDFLARE_STATE_R2_ACCESS_KEY_ID")
    monkeypatch.delenv("CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY")
    with pytest.raises(publication_state.StateValidationError, match="Durable-state configuration"):
        history_tracker.load_history_with_etag()
    with pytest.raises(publication_state.StateValidationError, match="Legacy history writes are disabled"):
        history_tracker._upload_history({"posted_artworks": []}, '"etag"')


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
