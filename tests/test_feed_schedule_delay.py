"""Delayed GitHub starts can publish once, without a catch-up burst."""

from datetime import timedelta

import pytest

from src import feed_schedule, history_tracker
from tests.test_continuous_feed_schedule import continuous_runtime
from tests.test_feed_schedule import SHA, auth, manager
from tests.test_six_hour_feed_schedule import AT, enable_six


@pytest.mark.parametrize("delay", [60, 277, 341, 359])
def test_fresh_delayed_run_is_ready_and_can_claim_current_slot(monkeypatch, delay):
    store, client, _ = continuous_runtime(monkeypatch)
    enable_six(store)
    at = AT + timedelta(minutes=delay)
    before = client.puts
    status = manager(store).status(expected_sha=SHA, now=at)
    assert status["ready"] is True
    assert status["next_attempt_at"] == feed_schedule.stamp(at)
    assert client.puts == before
    assert manager(store).admit(auth(200, created_at=at), now=at)
    permit = store.load_safety()[0].feed_schedule_control.permits[-1]
    assert permit.slot_at == "2026-10-04T18:17:00Z"
    assert permit.expires_at == "2026-10-05T00:17:00Z"


def test_delayed_success_waits_six_hours_from_completion(monkeypatch):
    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    clock[0] = AT + timedelta(hours=5)
    authorization = auth(200, created_at=clock[0])
    manager(store).admit(authorization, now=clock[0])
    art = {"id": "aic_990080", "title": "Study", "artist": "Unknown", "museum": "Museum"}
    pid = history_tracker.reserve_single_publication(art, authorization=authorization)
    history_tracker.start_publication_attempt(
        [art["id"]], "container", expected_publication_id=pid, authorization=authorization,
    )
    clock[0] += timedelta(minutes=8)
    history_tracker.confirm_artworks_and_record_publication(
        [art["id"]], "media-delayed", "single", publication_id=pid,
    )
    manager(store).record_outcome(authorization, now=clock[0])
    assert store.load_safety()[0].feed_schedule_control.next_eligible_at == "2026-10-05T05:25:00Z"
    next_slot = AT + timedelta(hours=6)
    assert manager(store).status(expected_sha=SHA, now=next_slot)["status"] == "WAITING_COOLDOWN"
    assert manager(store).admit(auth(201, created_at=next_slot), now=next_slot) is None
    due = AT + timedelta(hours=11, minutes=8)
    assert manager(store).status(expected_sha=SHA, now=due)["ready"] is True
    assert manager(store).admit(auth(202, created_at=due), now=due)


def test_closed_failed_slot_is_skipped_until_next_slot_without_writes(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    enable_six(store)
    authorization = auth(200, created_at=AT)
    manager(store).admit(authorization, now=AT)
    manager(store).record_outcome(authorization, now=AT + timedelta(minutes=5))
    before = client.puts
    status = manager(store).status(expected_sha=SHA, now=AT + timedelta(hours=2))
    assert status["ready"] is False
    assert status["reason"] == "SLOT_ALREADY_CONSUMED"
    assert status["next_attempt_at"] == "2026-10-05T00:17:00Z"
    assert client.puts == before
    assert manager(store).status(expected_sha=SHA, now=AT + timedelta(hours=6))["ready"] is True


def test_delayed_run_does_not_bypass_freshness_or_cross_slot_boundary(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    enable_six(store)
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError, match="NOT_FRESH"):
        manager(store).admit(auth(200, created_at=AT), now=AT + timedelta(hours=5))
    with pytest.raises(feed_schedule.FeedScheduleError, match="WINDOW_INVALID"):
        manager(store).admit(
            auth(201, created_at=AT + timedelta(hours=5, minutes=59)),
            now=AT + timedelta(hours=6),
        )
    assert client.puts == before


def test_actions_summary_distinguishes_confirmed_publication_from_skip(monkeypatch, tmp_path):
    from scripts import manage_feed_schedule
    from tests.test_six_hour_feed_schedule import finish_single

    store, client, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_RUN_ID", "200")
    before = client.puts
    assert manage_feed_schedule.main(
        ["status", "--github-summary"], manager=manager(store), now=AT,
    ) == 0
    assert "No confirmed Instagram publication for this run" in summary.read_text()
    assert client.puts == before

    finish_single(store, clock, AT, 200, 990081)
    before = client.puts
    assert manage_feed_schedule.main(
        ["status", "--github-summary"], manager=manager(store), now=clock[0],
    ) == 0
    assert "Instagram publication confirmed" in summary.read_text()
    assert "2026-10-04T18:25:21Z" in summary.read_text()
    assert client.puts == before

    monkeypatch.setenv("GITHUB_RUN_ID", "201")
    assert manage_feed_schedule.main(
        ["status", "--github-summary"], manager=manager(store), now=clock[0],
    ) == 0
    assert summary.read_text().count("Instagram publication confirmed") == 1


def test_historical_one_hour_permit_keeps_its_proven_nominal_deadline(monkeypatch):
    from src.models import FeedScheduleControl
    from tests.test_six_hour_feed_schedule import finish_single

    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    finish_single(store, clock, AT, 200, 990082)
    control = store.load_safety()[0].feed_schedule_control.model_dump(mode="json")
    # The previous release allowed a late completion after a one-hour admission.
    control["permits"][-1]["expires_at"] = "2026-10-04T19:17:00Z"
    control["latest_successful_feed_at"] = "2026-10-04T19:47:00Z"
    control["permits"][-1]["outcome"]["recorded_at"] = "2026-10-04T19:47:00Z"
    assert FeedScheduleControl.model_validate(control).next_eligible_at == "2026-10-05T00:17:00Z"


def test_delayed_calendar_does_not_promise_an_immediate_catch_up():
    from src.feed_calendar import build_feed_calendar
    from tests.test_feed_dashboard_live import package

    at = AT + timedelta(hours=5)
    schedule = {"status": "READY", "next_format": "single", "cadence": "six_hourly_utc17",
                "next_eligible_at": "2026-10-04T18:17:00Z"}
    rows = build_feed_calendar(schedule, [package("one"), package("two", "carousel")], now=at)
    assert rows[0]["earliest_at"] == at
    assert rows[1]["earliest_at"] == AT + timedelta(hours=11)
