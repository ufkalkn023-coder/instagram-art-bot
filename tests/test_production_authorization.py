"""Production authorization and final reservation fence regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import history_tracker, publication_state, r2_media
from src.production_authorization import (
    ProductionAuthorization,
    ProductionAuthorizationError,
    validate_run,
)
from src.production_config import ProductionConfigurationError, require_clear_publication_state
from src.models import REEL_RELEASE_FILE_HASH_KEYS, ReelReleaseIdentity
from tests.test_publication_state import safety_candidate, store_for


NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
AUTH_ID = "dc312b8d-e9a4-4e0a-8a23-8d166a325b1f"


def _environment(event="workflow_dispatch", attempt="1"):
    return {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_RUN_ATTEMPT": attempt,
        "GITHUB_RUN_ID": "987654",
        "GITHUB_REPOSITORY": "ufkalkn023-coder/instagram-art-bot",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF": "refs/heads/main",
        "ARTFOLIO_CONFIRM_PUBLISH": "PUBLISH_TO_INSTAGRAM",
        "ARTFOLIO_AUTHORIZATION_ID": AUTH_ID,
        "ARTFOLIO_MANUAL_AUTHORIZATION_ID": AUTH_ID,
        "ARTFOLIO_MANUAL_AUTHORIZATION_ISSUED_AT": "2026-09-26T11:45:00Z",
        "ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED": "true",
    }


def _run(event="workflow_dispatch", attempt=1, created="2026-09-26T11:50:00Z"):
    return {
        "id": 987654, "run_attempt": attempt, "event": event,
        "head_sha": "a" * 40, "created_at": created,
        "head_branch": "main",
        "repository": {"full_name": "ufkalkn023-coder/instagram-art-bot"},
    }


def test_manual_first_attempt_has_one_fresh_authorization():
    auth = validate_run(_environment(), _run(), now=NOW)
    assert auth.key == f"manual:{AUTH_ID}"
    assert auth.run_id == 987654


@pytest.mark.parametrize("attempt", [2, 3, 9])
def test_manual_reruns_fail_closed(attempt):
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_RERUN_PUBLICATION_BLOCKED"):
        validate_run(_environment(attempt=str(attempt)), _run(attempt=attempt), now=NOW)


def test_scheduled_original_is_distinct_and_rerun_is_blocked():
    auth = validate_run(_environment("schedule"), _run("schedule"), now=NOW)
    assert auth.key == "schedule:987654"
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_RERUN_PUBLICATION_BLOCKED"):
        validate_run(_environment("schedule", "2"), _run("schedule", 2), now=NOW)


def test_reel_uses_same_single_use_ledger_with_its_own_schedule_switch():
    environment = _environment()
    environment["ARTFOLIO_CONFIRM_PUBLISH"] = "PUBLISH_REEL_TO_INSTAGRAM"
    assert validate_run(environment, _run(), publication_kind="reel", now=NOW).key == (
        f"manual:{AUTH_ID}"
    )
    environment["ARTFOLIO_REEL_SCHEDULE_ENABLED"] = "true"
    assert validate_run(
        environment | {"GITHUB_EVENT_NAME": "schedule"},
        _run("schedule"), publication_kind="reel", now=NOW,
    ).key == "schedule:987654"


@pytest.mark.parametrize("created,issued", [
    ("2026-09-26T10:59:00Z", "2026-09-26T10:45:00Z"),
    ("2026-09-26T11:50:00Z", "2026-09-26T10:45:00Z"),
    ("2026-09-26T11:40:00Z", "2026-09-26T11:45:00Z"),
])
def test_expired_or_queued_before_authorization_is_blocked(created, issued):
    environment = _environment()
    environment["ARTFOLIO_MANUAL_AUTHORIZATION_ISSUED_AT"] = issued
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_EXPIRED"):
        validate_run(environment, _run(created=created), now=NOW)


def test_scheduled_event_delayed_past_window_is_blocked():
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_EXPIRED"):
        validate_run(
            _environment("schedule"), _run("schedule", created="2026-09-26T10:59:00Z"),
            now=NOW,
        )


def test_authorization_can_expire_during_candidate_selection():
    authorization = validate_run(_environment(), _run(), now=NOW)
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_EXPIRED"):
        authorization.require_fresh(NOW + timedelta(hours=1))


def test_copied_id_must_match_current_operator_grant():
    environment = _environment()
    environment["ARTFOLIO_MANUAL_AUTHORIZATION_ID"] = (
        "37124c82-06cd-4f52-90ad-c09d9680e8a4"
    )
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_INVALID"):
        validate_run(environment, _run(), now=NOW)


def test_authorization_is_bound_to_main_branch():
    environment = _environment()
    environment["GITHUB_REF"] = "refs/heads/codex/production-authorization-fence"
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_INVALID"):
        validate_run(environment, _run(), now=NOW)


def _artwork(identifier):
    return {"id": identifier, "title": identifier, "artist": "Artist", "museum": "Museum"}


def test_authorization_is_consumed_in_the_reservation_cas(monkeypatch):
    store, client = store_for(safety_candidate())
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    monkeypatch.setattr(r2_media, "list_owned_publication_ids", lambda **_: set())
    auth = ProductionAuthorization(f"manual:{AUTH_ID}", 987654, datetime.now(timezone.utc))
    cover = _artwork("cleveland_990001")
    featured = [_artwork(f"aic_{990002 + number}") for number in range(5)]

    publication_id = history_tracker.reserve_carousel(
        cover, featured, authorization=auth
    )
    state, _ = store.load_safety()
    consumed = state.active_publication_state.consumed_authorizations
    assert client.puts == 1
    assert consumed[0]["publication_id"] == publication_id
    assert consumed[0]["key"] == auth.key
    assert len(state.active_publication_state.posted_artworks) == 6

    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_ALREADY_CONSUMED"):
        history_tracker.reserve_carousel(
            _artwork("cleveland_990101"),
            [_artwork(f"aic_{990102 + number}") for number in range(5)],
            authorization=ProductionAuthorization(auth.key, 987655, auth.created_at),
        )
    reel_id = "aic_990200"
    release = ReelReleaseIdentity(
        version="artfolio-release-v1", reel_id=reel_id,
        created_at="2026-09-26T11:00:00Z", manifest_sha256="a" * 64,
        files_sha256={key: "b" * 64 for key in REEL_RELEASE_FILE_HASH_KEYS},
    )
    with pytest.raises(ProductionAuthorizationError, match="PRODUCTION_AUTHORIZATION_ALREADY_CONSUMED"):
        history_tracker.reserve_reel(
            reel_id, release,
            authorization=ProductionAuthorization(auth.key, 987656, auth.created_at),
        )
    assert client.puts == 1


def _state(rows=(), reels=(), feed_queue=(), reel_queue=()):
    return SimpleNamespace(active_publication_state=SimpleNamespace(
        posted_artworks=list(rows), reel_reservations=list(reels),
        staging_media_cleanup_queue=list(feed_queue),
        reel_staging_cleanup_queue=list(reel_queue),
    ))


@pytest.mark.parametrize("status", ["PENDING", "PUBLISHING", "AMBIGUOUS"])
def test_unresolved_feed_state_stops_automation(monkeypatch, status):
    monkeypatch.setattr(publication_state, "validate_live_receipt_coverage", lambda *_: None)
    row = {"publication_id": "publication-1", "status": status,
           "reserved_at": "2026-09-26T11:30:00Z"}
    with pytest.raises(ProductionConfigurationError, match="STOP_AUTOMATED_PRODUCTION"):
        require_clear_publication_state(_state(rows=[row]), object(), now=NOW)


def test_stale_pending_needs_durable_resolution_but_expired_record_is_safe(monkeypatch):
    monkeypatch.setattr(publication_state, "validate_live_receipt_coverage", lambda *_: None)
    row = {"publication_id": "publication-1", "status": "PENDING",
           "reserved_at": "2026-09-26T09:00:00Z"}
    with pytest.raises(ProductionConfigurationError, match="stale_pending_requires_reconciliation"):
        require_clear_publication_state(_state(rows=[row]), object(), now=NOW)
    row["status"] = "EXPIRED"
    require_clear_publication_state(
        _state(rows=[row]), object(), owned_feed_ids=set(), now=NOW
    )


def test_pending_receipt_and_cleanup_queue_stop_automation(monkeypatch):
    def incomplete(*_):
        raise publication_state.StateValidationError("receipt synchronization is incomplete")
    monkeypatch.setattr(publication_state, "validate_live_receipt_coverage", incomplete)
    with pytest.raises(publication_state.StateValidationError, match="receipt"):
        require_clear_publication_state(_state(), object())
    monkeypatch.setattr(publication_state, "validate_live_receipt_coverage", lambda *_: None)
    with pytest.raises(ProductionConfigurationError, match="cleanup_queue_unresolved"):
        require_clear_publication_state(_state(feed_queue=[{"publication_id": "x"}]), object())


def test_owned_success_retention_is_allowed_but_expired_or_orphan_media_blocks(monkeypatch):
    monkeypatch.setattr(publication_state, "validate_live_receipt_coverage", lambda *_: None)
    published = {"publication_id": "success", "status": "PUBLISHED"}
    require_clear_publication_state(
        _state(rows=[published]), object(), owned_feed_ids={"success"}
    )
    expired = {"publication_id": "expired", "status": "EXPIRED"}
    for owned in ({"expired"}, {"orphan"}):
        with pytest.raises(ProductionConfigurationError, match="owned_media_lifecycle_anomaly"):
            require_clear_publication_state(
                _state(rows=[published, expired]), object(), owned_feed_ids=owned
            )
