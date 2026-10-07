"""Stage 1 regressions for hourly cadence, failure backoff, and status reads."""

from datetime import timedelta
from uuid import uuid4

import pytest

from src import feed_schedule, publication_state
from src.models import (
    FeedContinuousApproval,
    FeedScheduleControl,
    FeedScheduleOutcome,
    FeedSchedulePermit,
)
from tests.test_feed_schedule import NOW, SHA, auth, fixture_store, initialize, manager


def enable(store, *, cadence="hourly_utc17", now=None, sha=SHA):
    initialize(store)
    return manager(store).enable_continuous(
        expected_generation=2,
        approved_sha=sha,
        main_sha=sha,
        cadence=cadence,
        review_ref="operator:cadence-fixture",
        now=now or NOW - timedelta(hours=1),
    )


def test_old_control_and_permits_default_to_daily_cadence():
    store, _ = fixture_store()
    initialize(store)
    control = store.load_safety()[0].feed_schedule_control
    assert control.continuous_approvals == []
    assert control.model_dump(mode="json").get("continuous_approvals") == []
    assert FeedSchedulePermit.model_fields["cadence"].default == "daily_utc1717"
    assert FeedContinuousApproval.model_fields["cadence"].default == "daily_utc1717"


def test_uninitialized_status_is_read_only_and_has_no_attempt_due():
    store, client = fixture_store()
    before_writes = client.puts
    snapshot = manager(store).status(now=NOW)
    assert snapshot["status"] == "UNINITIALIZED" and not snapshot["ready"]
    assert snapshot["next_attempt_at"] is None
    assert client.puts == before_writes


def test_hourly_admission_uses_the_containing_minute_17_window():
    store, _ = fixture_store()
    enable(store)
    at = NOW
    permit_id = manager(store).admit(auth(created_at=NOW), now=at)
    permit = store.load_safety()[0].feed_schedule_control.permits[-1]
    assert permit.permit_id == permit_id
    assert permit.slot_at == "2026-10-04T17:17:00Z"
    assert permit.expires_at == "2026-10-04T18:17:00Z"


@pytest.mark.parametrize(
    "at,created,expected_slot,expected_expiry",
    [
        ("2026-10-04T18:16:00Z", "2026-10-04T17:17:00Z",
         "2026-10-04T17:17:00Z", "2026-10-04T18:17:00Z"),
        ("2026-10-04T18:17:00Z", "2026-10-04T18:17:00Z",
         "2026-10-04T18:17:00Z", "2026-10-04T19:17:00Z"),
        ("2026-10-05T00:16:00Z", "2026-10-04T23:17:00Z",
         "2026-10-04T23:17:00Z", "2026-10-05T00:17:00Z"),
        ("2026-10-05T00:17:00Z", "2026-10-05T00:17:00Z",
         "2026-10-05T00:17:00Z", "2026-10-05T01:17:00Z"),
    ],
)
def test_hourly_window_boundary_and_midnight_rollover(
    at, created, expected_slot, expected_expiry
):
    from datetime import datetime

    store, _ = fixture_store()
    enable(store)
    admitted_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    manager(store).admit(auth(created_at=created_at), now=admitted_at)
    permit = store.load_safety()[0].feed_schedule_control.permits[-1]
    assert permit.slot_at == expected_slot
    assert permit.expires_at == expected_expiry


def test_daily_cadence_remains_outside_its_window_until_1717():
    store, client = fixture_store()
    enable(store, cadence="daily_utc1717")
    before_window = NOW + timedelta(days=1, hours=2)
    before_writes = client.puts
    snapshot = manager(store).status(expected_sha=SHA, now=before_window)
    assert snapshot["status"] == "WAITING_WINDOW"
    assert snapshot["ready"] is False
    assert client.puts == before_writes
    assert manager(store).status(expected_sha=SHA, now=NOW)["ready"] is True


def test_legacy_daily_permit_remains_readable_and_status_ready():
    store, _ = fixture_store()
    initialize(store)
    manager(store).arm(
        expected_generation=2, approved_sha=SHA, main_sha=SHA, slot=NOW,
        expires_at=NOW + timedelta(minutes=60), review_ref="operator:legacy",
        now=NOW - timedelta(hours=1),
    )
    status = manager(store).status(expected_sha=SHA, now=NOW)
    assert status["ready"] is True
    assert status["cadence"] == "daily_utc1717"
    assert status["next_format"] == "carousel"


def legacy_armed(store, *, slot=NOW, expiry=None):
    initialize(store)
    manager(store).arm(
        expected_generation=2, approved_sha=SHA, main_sha=SHA, slot=slot,
        expires_at=expiry or slot + timedelta(minutes=60),
        review_ref="operator:legacy-window", now=NOW - timedelta(hours=1),
    )


def test_legacy_status_uses_future_permit_slot_as_next_attempt():
    store, client = fixture_store()
    future_slot = NOW + timedelta(days=1)
    legacy_armed(store, slot=future_slot)
    before_writes = client.puts
    status = manager(store).status(expected_sha=SHA, now=NOW)
    assert status["status"] == "WAITING_WINDOW" and not status["ready"]
    assert status["next_attempt_at"] == "2026-10-05T17:17:00Z"
    assert status["next_check_at"] == "2026-10-05T17:17:00Z"
    assert client.puts == before_writes


def test_legacy_status_does_not_reuse_expired_permit_window():
    store, _ = fixture_store()
    legacy_armed(store)
    status = manager(store).status(expected_sha=SHA, now=NOW + timedelta(minutes=61))
    assert status["status"] == "WAITING_WINDOW" and not status["ready"]
    assert status["reason"] == "SCHEDULE_WINDOW_CLOSED"
    assert status["next_attempt_at"] is None
    assert status["next_check_at"] is None


def test_legacy_status_honors_shortened_permit_expiry():
    store, _ = fixture_store()
    legacy_armed(store, expiry=NOW + timedelta(minutes=10))
    status = manager(store).status(expected_sha=SHA, now=NOW + timedelta(minutes=11))
    assert status["status"] == "WAITING_WINDOW" and not status["ready"]
    assert status["reason"] == "SCHEDULE_WINDOW_CLOSED"
    assert status["next_attempt_at"] is None
    assert status["next_check_at"] is None


def test_next_format_uses_latest_projected_feed_publication():
    store, _ = fixture_store()
    state, _ = store.load_safety()
    state.operational_projection.publications = [
        {"id": "single-1", "type": "single", "posted_at": "2026-10-02T16:00:00Z"},
    ]
    assert feed_schedule._next_feed_format(state) == "carousel"
    state.operational_projection.publications.append(
        {"id": "carousel-2", "type": "carousel", "posted_at": "2026-10-03T16:00:00Z"}
    )
    assert feed_schedule._next_feed_format(state) == "single"


def test_enable_continuous_is_idempotent_only_for_matching_sha_and_cadence():
    store, _ = fixture_store()
    first_id = enable(store, cadence="daily_utc1717")
    state, _ = store.load_safety()
    same_id = manager(store).enable_continuous(
        expected_generation=state.generation, approved_sha=SHA, main_sha=SHA,
        cadence="daily_utc1717", review_ref="operator:retry", now=NOW,
    )
    assert same_id == first_id
    state, _ = store.load_safety()
    next_id = manager(store).enable_continuous(
        expected_generation=state.generation, approved_sha=SHA, main_sha=SHA,
        cadence="hourly_utc17", review_ref="operator:hourly", now=NOW,
    )
    control = store.load_safety()[0].feed_schedule_control
    assert next_id != first_id
    assert [item.cadence for item in control.continuous_approvals] == [
        "daily_utc1717", "hourly_utc17"
    ]


def test_stale_generation_cas_conflict_does_not_write():
    store, client = fixture_store()
    enable(store)
    before_writes = client.puts
    with pytest.raises(publication_state.StateConflictError):
        manager(store).enable_continuous(
            expected_generation=2, approved_sha=SHA, main_sha=SHA,
            cadence="hourly_utc17", review_ref="operator:stale", now=NOW,
        )
    assert client.puts == before_writes


def test_model_rejects_hourly_unlinked_permit_and_cadence_policy_mismatch():
    permit = {
        "permit_id": str(uuid4()), "slot_at": "2026-10-04T17:17:00Z",
        "expires_at": "2026-10-04T18:17:00Z", "approved_sha": SHA,
        "created_at": "2026-10-04T17:17:00Z", "approval_ref": "fixture",
        "status": "ARMED", "cadence": "hourly_utc17",
    }
    base = {
        "schema_version": 1, "paused": True, "pause_reason": "fixture",
        "latest_successful_feed_at": "2026-10-02T17:17:00Z",
        "latest_successful_feed_id": "publication",
        "next_eligible_at": "2026-10-04T17:17:00Z",
    }
    with pytest.raises(ValueError):
        FeedScheduleControl.model_validate(base | {"permits": [permit]})

    approval_id = str(uuid4())
    approval = {
        "approval_id": approval_id, "approved_sha": SHA,
        "created_at": "2026-10-04T16:17:00Z", "approval_ref": "fixture",
        "revoked_at": None, "cadence": "daily_utc1717",
    }
    linked = permit | {
        "permit_id": str(uuid4()), "continuous_approval_id": approval_id,
        "created_at": "2026-10-04T17:17:00Z",
    }
    with pytest.raises(ValueError):
        FeedScheduleControl.model_validate(
            base | {"permits": [linked], "continuous_approvals": [approval]}
        )


def test_closed_continuous_window_can_be_followed_at_exact_expiry():
    publication_id = "66a4cac3-3db7-419d-bb05-74bd16a68801"
    first = {
        "permit_id": str(uuid4()), "slot_at": "2026-10-04T17:17:00Z",
        "expires_at": "2026-10-04T18:17:00Z", "approved_sha": SHA,
        "created_at": "2026-10-04T17:17:00Z", "approval_ref": "fixture",
        "continuous_approval_id": "d523a6c0-718d-4ef9-9cf5-1c2bcda8bd85",
        "cadence": "hourly_utc17", "status": "SUCCESS", "owner_run_id": 123,
        "owner_run_attempt": 1, "authorization_key": "schedule:123",
        "admitted_at": "2026-10-04T17:17:00Z", "publication_id": publication_id,
        "reservation_generation": 2,
        "outcome": {
            "classification": "SUCCESS", "recorded_at": "2026-10-04T17:45:00Z",
            "safety_generation": 4, "receipt_generation": 2,
            "publication_id": publication_id, "instagram_media_id": "media-1",
            "cleanup_pending": False, "reconciliation_pending": False,
        },
    }
    second = {
        "permit_id": str(uuid4()), "slot_at": "2026-10-04T18:17:00Z",
        "expires_at": "2026-10-04T19:17:00Z", "approved_sha": SHA,
        "created_at": "2026-10-04T18:17:00Z", "approval_ref": "fixture",
        "continuous_approval_id": first["continuous_approval_id"],
        "cadence": "hourly_utc17", "status": "ADMITTED", "owner_run_id": 124,
        "owner_run_attempt": 1, "authorization_key": "schedule:124",
        "admitted_at": "2026-10-04T18:17:00Z",
    }
    approval = {
        "approval_id": first["continuous_approval_id"], "approved_sha": SHA,
        "cadence": "hourly_utc17", "created_at": "2026-10-04T16:17:00Z",
        "approval_ref": "fixture", "revoked_at": None,
    }
    control = {
        "schema_version": 1, "paused": False, "pause_reason": "fixture",
        "latest_successful_feed_at": "2026-10-02T17:17:00Z",
        "latest_successful_feed_id": "baseline",
        "next_eligible_at": "2026-10-04T17:17:00Z",
        "permits": [first, second], "continuous_approvals": [approval],
    }
    assert len(FeedScheduleControl.model_validate(control).permits) == 2


def test_status_waiting_cooldown_is_read_only_and_reports_next_attempt():
    store, client = fixture_store()
    enable(store, now=NOW - timedelta(hours=1))
    before_writes = client.puts
    snapshot = manager(store).status(expected_sha=SHA, now=NOW - timedelta(seconds=1))
    assert snapshot["status"] == "WAITING_COOLDOWN"
    assert snapshot["ready"] is False
    assert snapshot["next_attempt_at"] == "2026-10-04T17:17:00Z"
    assert snapshot["generation"] == store.load_safety()[0].generation
    assert client.puts == before_writes


def test_status_distinguishes_sha_mismatch_paused_and_active_attempt():
    store, client = fixture_store()
    enable(store)
    before_writes = client.puts
    status = manager(store).status(expected_sha="b" * 40, now=NOW)
    assert status["status"] == "SHA_MISMATCH" and not status["ready"]
    assert status["next_attempt_at"] is None
    assert client.puts == before_writes

    manager(store).pause(
        expected_generation=store.load_safety()[0].generation,
        reason="operator stop", now=NOW,
    )
    before_writes = client.puts
    status = manager(store).status(now=NOW)
    assert status["status"] == "PAUSED" and not status["ready"]
    assert status["next_attempt_at"] is None
    assert client.puts == before_writes

    enable_store, enable_client = fixture_store()
    enable(enable_store)
    before_writes = enable_client.puts
    manager(enable_store).admit(auth(), now=NOW)
    before_writes = enable_client.puts
    status = manager(enable_store).status(expected_sha=SHA, now=NOW)
    assert status["status"] == "ATTEMPT_ACTIVE" and not status["ready"]
    assert status["next_attempt_at"] is None
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(enable_store).admit(auth(124), now=NOW)
    assert enable_client.puts == before_writes



def test_hourly_cadence_is_immutable_in_approval_and_permit_updates():
    store, _ = fixture_store()
    enable(store)
    old = store.load_safety()[0].feed_schedule_control
    new = old.model_copy(deep=True)
    new.continuous_approvals[-1].cadence = "daily_utc1717"
    with pytest.raises(publication_state.StateValidationError):
        feed_schedule.require_control_update(old, new)

    manager(store).admit(auth(), now=NOW)
    old = store.load_safety()[0].feed_schedule_control
    new = old.model_copy(deep=True)
    new.permits[-1].cadence = "daily_utc1717"
    with pytest.raises(publication_state.StateValidationError):
        feed_schedule.require_control_update(old, new)


def test_reapproval_does_not_reset_definitive_failure_backoff():
    store, client = fixture_store()
    enable(store, now=NOW - timedelta(hours=1))
    state, etag = store.load_safety()
    control = state.feed_schedule_control.model_copy(deep=True)
    approval = control.continuous_approvals[-1]
    failure = FeedSchedulePermit(
        permit_id=str(uuid4()), slot_at="2026-10-04T17:17:00Z",
        expires_at="2026-10-04T18:17:00Z", approved_sha=SHA,
        created_at="2026-10-04T17:17:00Z", approval_ref="fixture",
        cadence="hourly_utc17", continuous_approval_id=approval.approval_id,
        status="DEFINITIVE_FAILURE", owner_run_id=123, owner_run_attempt=1,
        authorization_key="schedule:123", admitted_at="2026-10-04T17:17:00Z",
        outcome=FeedScheduleOutcome(
            classification="DEFINITIVE_FAILURE", recorded_at="2026-10-04T17:22:00Z",
            safety_generation=state.generation + 1, receipt_generation=1,
            publication_id=None, instagram_media_id=None, cleanup_pending=False,
            reconciliation_pending=False,
        ),
    )
    control.permits.append(failure)
    raw = state.model_dump(mode="json")
    raw["feed_schedule_control"] = control.model_dump(mode="json")
    raw["generation"] += 1
    store.update_safety(publication_state.seal(raw), etag)
    state, _ = store.load_safety()
    manager(store).enable_continuous(
        expected_generation=state.generation, approved_sha="b" * 40,
        main_sha="b" * 40, cadence="hourly_utc17", review_ref="operator:sha-change",
        now=NOW + timedelta(minutes=10),
    )
    status = manager(store).status(
        expected_sha="b" * 40, now=NOW + timedelta(hours=23)
    )
    assert status["status"] == "WAITING_FAILURE_BACKOFF"
    assert status["next_attempt_at"] == "2026-10-05T17:22:00Z"
    ready = manager(store).status(
        expected_sha="b" * 40, now=NOW + timedelta(hours=24, minutes=5)
    )
    assert ready["ready"] is True

    before_writes = client.puts
    before_expiry = NOW + timedelta(hours=24, minutes=4)
    assert manager(store).admit(
        auth(124, created_at=before_expiry, head_sha="b" * 40), now=before_expiry
    ) is None
    assert client.puts == before_writes
    at_expiry = NOW + timedelta(hours=24, minutes=5)
    permit_id = manager(store).admit(
        auth(125, created_at=at_expiry, head_sha="b" * 40), now=at_expiry
    )
    assert permit_id is not None
    assert client.puts == before_writes + 1


def test_pending_cleanup_blocks_status_before_ordinary_waiting(monkeypatch):
    store, client = fixture_store()
    enable(store, now=NOW - timedelta(hours=1))
    state, etag = store.load_safety()
    raw = state.model_dump(mode="json")
    raw["active_publication_state"]["staging_media_cleanup_queue"].append(
        {
            "publication_id": "66a4cac3-3db7-419d-bb05-74bd16a68801",
            "eligible_at": "2026-10-04T17:17:00Z",
            "reason": "fixture",
        }
    )
    raw["generation"] += 1
    store.update_safety(publication_state.seal(raw), etag)
    before_writes = client.puts
    status = manager(store).status(expected_sha=SHA, now=NOW - timedelta(seconds=1))
    assert status["status"] == "BLOCKED" and not status["ready"]
    assert status["next_attempt_at"] is None
    assert client.puts == before_writes


def test_status_propagates_owned_media_lookup_failure():
    store, _ = fixture_store()
    enable(store)

    def failed_lookup(**_):
        raise RuntimeError("owned media lookup unavailable")

    with pytest.raises(RuntimeError, match="owned media lookup unavailable"):
        feed_schedule.FeedScheduleManager(store, owned_media=failed_lookup).status(
            expected_sha=SHA, now=NOW
        )
