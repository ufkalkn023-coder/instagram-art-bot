"""Persistent approval keeps safe Feed runs enabled without repeat publication."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import main
from src import feed_schedule, history_tracker, publication_state, r2_media
from tests.test_feed_schedule import NOW, SHA, auth, fixture_store, initialize, manager, armed
from tests.test_single_feed_runtime import single_runtime
from scripts import manage_feed_schedule


def continuous_runtime(monkeypatch):
    store, client = fixture_store()
    initialize(store)
    manager(store).enable_continuous(
        expected_generation=2, approved_sha=SHA, main_sha=SHA,
        review_ref="operator:continuous", now=NOW - timedelta(hours=1),
    )
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(feed_schedule, "datetime", Clock)
    monkeypatch.setattr(history_tracker, "datetime", Clock)
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(r2_media, "list_owned_publication_ids", lambda **_: set())
    return store, client, clock


def test_enable_replaces_unconsumed_old_permit_without_erasing_it():
    store, client = fixture_store()
    armed(store)
    prior = store.load_safety()[0].feed_schedule_control.permits[-1]
    before = client.puts
    approval_id = manager(store).enable_continuous(
        expected_generation=3, approved_sha=SHA, main_sha=SHA,
        review_ref="operator:continuous", now=NOW - timedelta(minutes=30),
    )
    control = store.load_safety()[0].feed_schedule_control
    assert not control.paused and client.puts == before + 1
    assert control.permits[-1].permit_id == prior.permit_id
    assert control.permits[-1].approved_sha == prior.approved_sha
    assert control.permits[-1].status == "REVOKED"
    assert control.continuous_approvals[-1].approval_id == approval_id
    assert control.continuous_approvals[-1].approved_sha == SHA


def test_continuous_admission_binds_one_run_and_blocks_reuse(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    m = manager(store)
    permit_id = m.admit(auth(), now=NOW + timedelta(minutes=2))
    control = store.load_safety()[0].feed_schedule_control
    assert not control.paused
    assert control.permits[-1].permit_id == permit_id
    assert control.permits[-1].status == "ADMITTED"
    assert control.permits[-1].continuous_approval_id == control.continuous_approvals[-1].approval_id
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError):
        m.admit(auth(), now=NOW + timedelta(minutes=3))
    assert client.puts == before


def test_success_automatically_allows_next_due_slot_with_same_approval(monkeypatch):
    store, client, clock = continuous_runtime(monkeypatch)
    m = manager(store)
    m.admit(auth(), now=NOW)
    artwork = {"id": "aic_990007", "title": "Study", "artist": "Unknown", "museum": "Museum"}
    pid = history_tracker.reserve_single_publication(artwork, authorization=auth())
    history_tracker.start_publication_attempt(
        [artwork["id"]], "container", expected_publication_id=pid, authorization=auth(),
    )
    history_tracker.confirm_artworks_and_record_publication(
        [artwork["id"]], "media", "single", publication_id=pid,
    )
    m.record_outcome(auth(), now=NOW)
    control = store.load_safety()[0].feed_schedule_control
    assert not control.paused and control.permits[-1].status == "SUCCESS"
    clock[0] = NOW + timedelta(days=1)
    before = client.puts
    assert m.admit(auth(456, created_at=clock[0]), now=clock[0]) is None
    assert client.puts == before
    clock[0] = NOW + timedelta(days=2)
    assert m.admit(auth(789, created_at=clock[0]), now=clock[0])
    control = store.load_safety()[0].feed_schedule_control
    assert len(control.permits) == 2 and len(control.continuous_approvals) == 1
    assert not control.paused and control.permits[-1].owner_run_id == 789


def test_definitive_pre_reservation_failure_keeps_future_schedule_enabled(monkeypatch):
    store, _, _ = continuous_runtime(monkeypatch)
    m = manager(store)
    m.admit(auth(), now=NOW)
    m.record_outcome(auth(), now=NOW)
    control = store.load_safety()[0].feed_schedule_control
    assert not control.paused and control.permits[-1].status == "DEFINITIVE_FAILURE"
    with pytest.raises(feed_schedule.FeedScheduleError):
        m.admit(auth(456), now=NOW + timedelta(minutes=5))
    tomorrow = NOW + timedelta(days=1)
    assert m.admit(auth(789, created_at=tomorrow), now=tomorrow)


@pytest.mark.parametrize("change", [
    {"head_sha": "b" * 40}, {"run_attempt": 2}, {"repository": "other/repo"},
    {"workflow_path": ".github/workflows/other.yml"},
    {"created_at": NOW - timedelta(hours=2)},
])
def test_continuous_still_rejects_wrong_identity_without_write(monkeypatch, change):
    store, client, _ = continuous_runtime(monkeypatch)
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(**change), now=NOW)
    assert client.puts == before


def test_continuous_rejects_closed_daily_window_without_write(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    at = NOW + timedelta(hours=2)
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(created_at=at), now=at)
    assert client.puts == before


def test_operator_pause_revokes_persistent_approval_and_pre_publish_owner(monkeypatch):
    store, _, _ = continuous_runtime(monkeypatch)
    m = manager(store)
    m.admit(auth(), now=NOW)
    m.pause(expected_generation=4, reason="operator stop", now=NOW)
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused and control.continuous_approvals[-1].revoked_at is not None
    with pytest.raises(feed_schedule.FeedScheduleError):
        feed_schedule.require_owner(store.load_safety()[0], auth(), status="ADMITTED", now=NOW)


def test_cooldown_skip_returns_before_acquisition_and_reconciliation(monkeypatch):
    monkeypatch.setattr(main, "validate_carousel_production_preflight", lambda: {})
    monkeypatch.setattr(main, "load_workflow_authorization", auth)
    monkeypatch.setattr(feed_schedule, "FeedScheduleManager", lambda: SimpleNamespace(admit=lambda _: None))
    monkeypatch.setattr(main.history_tracker, "get_recent_publications", lambda: [])
    monkeypatch.setattr(main, "_snapshot_generated_artifacts", lambda: pytest.fail("skip snapshot"))
    monkeypatch.setattr(main.publication_reconciliation, "reconcile_publications", lambda **_: pytest.fail("skip reconciliation"))
    monkeypatch.setattr(main, "run_carousel_post", lambda *_: pytest.fail("skip acquisition"))
    assert main.main(["--mode", "auto"]) == 0


@pytest.mark.parametrize("phase,paused,status", [
    ("success", False, "SUCCESS"), ("ambiguous", True, "AMBIGUOUS"),
])
def test_continuous_single_uses_real_durable_publisher_lifecycle(monkeypatch, tmp_path, phase, paused, status):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)
    manager(store).enable_continuous(
        expected_generation=3, approved_sha=SHA, main_sha=SHA,
        review_ref="operator:continuous", now=NOW - timedelta(minutes=30),
    )
    assert main.main(["--mode", "single"]) == (1 if paused else 0)
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused is paused and control.permits[-1].status == status
    assert sum(url.endswith("/media_publish") for url, _ in sent) == 1


def test_operator_cli_enables_only_exact_authenticated_main(capsys):
    store, _ = fixture_store()
    initialize(store)
    github = SimpleNamespace(main_sha=lambda: SHA)
    assert manage_feed_schedule.main([
        "enable-continuous", "--apply", "--expected-generation", "2",
        "--approved-sha", SHA, "--evidence-ref", "operator:continuous",
    ], manager=manager(store), github=github, now=NOW - timedelta(hours=1)) == 0
    control = store.load_safety()[0].feed_schedule_control
    assert not control.paused and control.continuous_approvals[-1].approved_sha == SHA


def test_persistent_approval_cannot_be_rewritten(monkeypatch):
    store, _, _ = continuous_runtime(monkeypatch)
    state, etag = store.load_safety()
    raw = state.model_dump(mode="json")
    raw["generation"] += 1
    raw["feed_schedule_control"]["continuous_approvals"][-1]["approved_sha"] = "b" * 40
    with pytest.raises(publication_state.StateValidationError, match="immutable"):
        store.update_safety(publication_state.seal(raw), etag)


def test_old_sealed_control_without_new_fields_remains_readable_and_enableable():
    store, client = fixture_store()
    armed(store)
    raw = store.load_safety()[0].model_dump(mode="json")
    del raw["feed_schedule_control"]["continuous_approvals"]
    for permit in raw["feed_schedule_control"]["permits"]:
        del permit["continuous_approval_id"]
    client.objects[publication_state.SAFETY_KEY] = publication_state.seal(raw)
    before = client.puts
    assert store.load_safety()[0].feed_schedule_control.continuous_approvals == []
    assert client.puts == before
    assert manager(store).enable_continuous(
        expected_generation=3, approved_sha=SHA, main_sha=SHA,
        review_ref="operator:continuous", now=NOW - timedelta(minutes=30),
    )


def test_continuous_reapproval_preserves_old_approval_and_binds_new_sha(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    old = store.load_safety()[0].feed_schedule_control.continuous_approvals[-1]
    new_sha = "b" * 40
    new_id = manager(store).enable_continuous(
        expected_generation=3, approved_sha=new_sha, main_sha=new_sha,
        review_ref="operator:new main", now=NOW - timedelta(minutes=1),
    )
    control = store.load_safety()[0].feed_schedule_control
    assert len(control.continuous_approvals) == 2
    assert control.continuous_approvals[0].approval_id == old.approval_id
    assert control.continuous_approvals[0].revoked_at == control.continuous_approvals[-1].created_at
    assert control.continuous_approvals[-1].approval_id == new_id
    assert manager(store).admit(auth(head_sha=new_sha), now=NOW)
