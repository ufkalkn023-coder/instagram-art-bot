"""Four nominal six-hour slots without completion-delay drift or catch-up bursts."""
from datetime import datetime, timedelta, timezone

import pytest

from src import feed_schedule, history_tracker, publication_state
from tests.test_feed_schedule import NOW, SHA, auth, fixture_store, initialize, manager
from tests.test_continuous_feed_schedule import continuous_runtime

CADENCE = 'six_hourly_utc17'
AT = NOW + timedelta(hours=1)  # 18:17 UTC / 21:17 Europe/Istanbul


def enable_six(store, *, now=NOW):
    state = store.load_safety()[0]
    return manager(store).enable_continuous(expected_generation=state.generation,
        approved_sha=SHA, main_sha=SHA, cadence=CADENCE, review_ref='operator:four-daily', now=now)


def test_explicit_six_hour_approval_migrates_without_changing_success_or_receipts():
    store, _ = fixture_store()
    initialize(store)
    before = store.load_safety()[0]
    receipts = store.load_receipts()[0].model_dump(mode='json')
    enable_six(store)
    after = store.load_safety()[0]
    control = after.feed_schedule_control
    assert control.schema_version == 2
    assert control.latest_successful_feed_at == before.feed_schedule_control.latest_successful_feed_at
    assert control.next_eligible_at == '2026-10-02T23:17:00Z'
    assert store.load_receipts()[0].model_dump(mode='json') == receipts
    assert after.active_publication_state == before.active_publication_state


def test_selected_local_times_are_four_six_hour_slots():
    from zoneinfo import ZoneInfo
    local = ZoneInfo('Europe/Istanbul')
    for hour in (3, 9, 15, 21):
        at = datetime(2026, 10, 21, hour, 17, tzinfo=local)
        slot, expiry = feed_schedule.cadence_window(at, CADENCE)
        assert slot == at.astimezone(timezone.utc)
        assert expiry - slot == timedelta(hours=6)
    at = datetime(2026, 10, 21, 4, 18, tzinfo=local)
    assert feed_schedule.next_window_at_or_after(at, CADENCE) == at


def finish_single(store, clock, slot, run_id, art_id, *, outcome=True):
    clock[0] = slot
    authorization = auth(run_id, created_at=slot)
    manager(store).admit(authorization, now=slot)
    artwork = dict(id=f'aic_{art_id}', title='Study', artist='Unknown', museum='Museum')
    pid = history_tracker.reserve_single_publication(artwork, authorization=authorization)
    history_tracker.start_publication_attempt([artwork['id']], 'container', expected_publication_id=pid,
                                            authorization=authorization)
    clock[0] = slot + timedelta(minutes=8, seconds=21)
    history_tracker.confirm_artworks_and_record_publication([artwork['id']], f'media-{run_id}', 'single', publication_id=pid)
    provisional = store.load_safety()[0].feed_schedule_control
    assert provisional.next_eligible_at == feed_schedule.stamp(clock[0] + timedelta(hours=6))
    if outcome:
        manager(store).record_outcome(authorization, now=clock[0])
    return pid


def test_delayed_completion_keeps_next_nominal_slot_and_four_posts_per_day(monkeypatch):
    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    for index in range(4):
        slot = AT + timedelta(hours=6 * index)
        finish_single(store, clock, slot, 200 + index, 990070 + index)
        control = store.load_safety()[0].feed_schedule_control
        assert control.next_eligible_at == feed_schedule.stamp(slot + timedelta(hours=6))
    control = store.load_safety()[0].feed_schedule_control
    assert len(control.permits) == 4
    assert all(permit.status == 'SUCCESS' for permit in control.permits)
    assert control.next_eligible_at == '2026-10-05T18:17:00Z'


def test_forged_deadline_cannot_shorten_six_hour_policy(monkeypatch):
    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    finish_single(store, clock, AT, 200, 990070)
    state, etag = store.load_safety()
    value = state.model_dump(mode='json')
    value['feed_schedule_control']['next_eligible_at'] = '2026-10-04T19:17:00Z'
    value['generation'] += 1
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(publication_state.seal(value), etag)


def test_safe_failure_waits_until_next_regular_slot(monkeypatch):
    store, _, _ = continuous_runtime(monkeypatch)
    enable_six(store)
    authorization = auth(200, created_at=AT)
    m = manager(store)
    m.admit(authorization, now=AT)
    m.record_outcome(authorization, now=AT + timedelta(minutes=5))
    at = AT + timedelta(minutes=15)
    assert not m.status(expected_sha=SHA, now=at)['ready']
    next_slot = AT + timedelta(hours=6)
    assert m.status(expected_sha=SHA, now=next_slot)['ready']
    assert m.admit(auth(201, created_at=next_slot), now=next_slot)


def test_release_can_explicitly_select_six_hour_policy(monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from scripts.release_editorial_feed import run
    store, _, _ = continuous_runtime(monkeypatch)
    before = store.load_safety()[0]
    ledger = store.load_receipts()[0].model_dump(mode='json')
    assert run(['--expected-sha', 'b' * 40, '--apply', '--expected-generation', str(before.generation),
                '--evidence-ref', 'operator:four-daily', '--cadence', CADENCE],
               manager=manager(store), github=SimpleNamespace(main_sha=lambda: 'b' * 40)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['cadence'] == CADENCE and status['post_interval_hours'] == 6
    assert status['last_successful_feed_at'] == '2026-10-02T17:17:00Z'
    assert status['next_eligible_at'] == '2026-10-02T23:17:00Z'
    assert store.load_receipts()[0].model_dump(mode='json') == ledger


def test_calendar_uses_next_nominal_six_hour_slot():
    from src.feed_calendar import build_feed_calendar
    from tests.test_feed_dashboard_live import package
    schedule = {'status': 'WAITING_COOLDOWN', 'next_format': 'single', 'cadence': CADENCE,
                'next_eligible_at': '2026-10-18T06:23:00Z'}
    rows = build_feed_calendar(schedule, [package('one'), package('two', 'carousel')], now=AT)
    assert rows[0]['earliest_at'] == datetime(2026, 10, 18, 6, 23, tzinfo=timezone.utc)
    assert rows[1]['earliest_at'] == datetime(2026, 10, 18, 12, 17, tzinfo=timezone.utc)


def test_missing_receipt_cannot_confirm_success_or_shorten_provisional_deadline(monkeypatch):
    store, client, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    pid = finish_single(store, clock, AT, 200, 990070, outcome=False)
    before = store.load_safety()[0]
    ledger, etag = store.load_receipts()
    missing = ledger.model_copy(deep=True)
    missing.records = [r for r in missing.records if r.publication_id != pid]
    missing.record_count = len(missing.records)
    monkeypatch.setattr(store, 'load_receipts', lambda: (missing, etag))
    puts = client.puts
    with pytest.raises(publication_state.StateValidationError):
        manager(store).record_outcome(auth(200, created_at=AT), now=clock[0])
    assert client.puts == puts
    assert store.load_safety()[0].feed_schedule_control == before.feed_schedule_control


def test_six_hour_approval_cannot_downgrade_schema(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    enable_six(store)
    state, etag = store.load_safety()
    raw = state.model_dump(mode='json')
    raw['feed_schedule_control']['schema_version'] = 1
    raw['generation'] += 1
    puts = client.puts
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(publication_state.seal(raw), etag)
    assert client.puts == puts


def test_nominal_deadline_requires_matching_receipt_media(monkeypatch):
    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    pid = finish_single(store, clock, AT, 200, 990070)
    ledger, etag = store.load_receipts()
    mismatched = ledger.model_copy(deep=True)
    next(r for r in mismatched.records if r.publication_id == pid).instagram_media_id = 'wrong-media'
    monkeypatch.setattr(store, 'load_receipts', lambda: (mismatched, etag))
    with pytest.raises(publication_state.StateValidationError, match='exact receipt'):
        manager(store).status(expected_sha=SHA, now=AT + timedelta(hours=6))


def test_late_or_unlinked_completion_uses_conservative_six_hours(monkeypatch):
    from src.feed_cadence import eligibility_deadline
    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    finish_single(store, clock, AT, 200, 990070)
    control = store.load_safety()[0].feed_schedule_control.model_dump(mode='json')
    late = AT + timedelta(hours=6, minutes=2)
    control['latest_successful_feed_at'] = feed_schedule.stamp(late)
    control['permits'][-1]['outcome']['recorded_at'] = feed_schedule.stamp(late)
    assert eligibility_deadline(control) == late + timedelta(hours=6)
    control['latest_successful_feed_id'] = 'unlinked-manual-publication'
    assert eligibility_deadline(control) == late + timedelta(hours=6)


def test_migration_rejects_stale_generation_without_writing(monkeypatch):
    store, client, _ = continuous_runtime(monkeypatch)
    generation = store.load_safety()[0].generation
    enable_six(store)
    puts = client.puts
    with pytest.raises(publication_state.StateConflictError):
        manager(store).enable_continuous(expected_generation=generation,
            approved_sha=SHA, main_sha=SHA, cadence=CADENCE, review_ref='stale-operator', now=NOW)
    assert client.puts == puts


def test_acknowledged_success_recovers_nominal_deadline_and_stays_paused(monkeypatch):
    store, _, clock = continuous_runtime(monkeypatch)
    enable_six(store)
    finish_single(store, clock, AT, 200, 990070, outcome=False)
    state = store.load_safety()[0]
    run = dict(id=200, status='completed', conclusion='failure', event='schedule',
        path=feed_schedule.FEED_WORKFLOW, head_branch='main',
        repository={'full_name': feed_schedule.FEED_REPOSITORY}, head_sha=SHA,
        run_attempt=1, created_at=feed_schedule.stamp(AT))
    manager(store).acknowledge(expected_generation=state.generation,
        evidence_ref='operator:verified-completion', audit_runs=lambda permit: [run],
        now=AT + timedelta(hours=6))
    control = store.load_safety()[0].feed_schedule_control
    assert control.permits[-1].status == 'SUCCESS' and control.paused
    assert control.next_eligible_at == feed_schedule.stamp(AT + timedelta(hours=6))
