"""Refill must preserve unfinished evidence and avoid unnecessary acquisition."""

import json
from datetime import timedelta

import pytest

from scripts import prepare_feed_queue as command
from src.publication_state import canonical_bytes, seal
from tests.test_feed_queue import NOW, content
from tests.test_r2_feed_queue import install, remote
from tests.test_feed_schedule import SHA, fixture_store, initialize, manager


def test_missing_queue_needs_refill_without_writes_or_directory_creation(tmp_path):
    queue = remote(tmp_path)
    assert queue.refill_status(now=NOW) == {'refill_needed': True, 'reason': 'EMPTY'}
    assert not queue.client.writes
    assert not queue.directory.exists()


@pytest.mark.parametrize('state,age,needed,reason', [
    ('READY', timedelta(), False, 'READY_PRESENT'),
    ('READY', timedelta(days=14), True, 'EXPIRED'),
    ('CLAIMED', timedelta(days=15), False, 'CLAIMED_PRESENT'),
    ('CONSUMED', timedelta(), True, 'EXHAUSTED'),
    ('QUARANTINED', timedelta(), True, 'EXHAUSTED'),
])
def test_refill_preserves_live_packages_and_never_rearms_expired_claims(tmp_path, state, age, needed, reason):
    queue, _ = install(tmp_path)
    document = json.loads(queue.client.objects[queue.manifest_key])
    for package in document['packages']:
        package.update(state=state, owner='run:owner' if state in {'CLAIMED', 'CONSUMED'} else None)
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(document))
    before = dict(queue.client.objects)
    writes = len(queue.client.writes)
    queue.client.reads.clear()
    assert queue.refill_status(now=NOW + age) == {'refill_needed': needed, 'reason': reason}
    assert queue.client.objects == before and len(queue.client.writes) == writes
    assert queue.client.reads == [queue.manifest_key]


def test_one_fresh_package_prevents_replacing_partly_expired_batch(tmp_path):
    queue, _ = install(tmp_path)
    document = json.loads(queue.client.objects[queue.manifest_key])
    document['packages'][0]['expires_at'] = '2026-10-07T20:00:00Z'
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(document))
    assert queue.refill_status(now=NOW + timedelta(hours=2))['refill_needed'] is False


@pytest.mark.parametrize('created,expiry', [
    ('bad', '2026-10-01T00:00:00Z'),
    ('2026-10-08T00:00:00Z', '2026-10-22T00:00:00Z'),
    ('2026-10-01T00:00:00Z', '2026-11-01T00:00:00Z'),
])
def test_invalid_ready_age_is_an_error_not_refill_permission(tmp_path, created, expiry):
    queue, _ = install(tmp_path)
    document = json.loads(queue.client.objects[queue.manifest_key])
    document['packages'][0].update(created_at=created, expires_at=expiry)
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(document))
    before = dict(queue.client.objects)
    with pytest.raises(RuntimeError):
        queue.refill_status(now=NOW)
    assert queue.client.objects == before


def test_refill_check_rejects_corrupt_manifest(tmp_path):
    queue = remote(tmp_path)
    queue.client.objects[queue.manifest_key] = b'bad'
    with pytest.raises(RuntimeError):
        queue.refill_status(now=NOW)
    assert not queue.client.writes


def wire_command(monkeypatch, queue, *, sha=SHA):
    store, client = fixture_store()
    initialize(store)
    schedule = manager(store)
    schedule.enable_continuous(expected_generation=2, approved_sha=sha, main_sha=sha,
                               cadence='hourly_utc17', review_ref='fixture:refill', now=NOW)
    queue_status, schedule_status = queue.refill_status, schedule.status
    monkeypatch.setattr(queue, 'refill_status', lambda **_: queue_status(now=NOW))
    monkeypatch.setattr(schedule, 'status', lambda **kwargs: schedule_status(now=NOW, **kwargs))
    monkeypatch.setattr(command, 'R2PreparedFeedQueue', lambda *_: queue, raising=False)
    monkeypatch.setattr(command, 'PublicationStateStore', lambda **_: store, raising=False)
    monkeypatch.setattr(command, 'FeedScheduleManager', lambda _: schedule, raising=False)
    return store, client


def cli_args(tmp_path, mode='--refill'):
    return ['--directory', str(tmp_path / 'prepared'), '--r2', '--skip-keychain',
            mode, '--expected-sha', SHA]


def test_full_queue_skips_without_acquisition_or_state_writes(monkeypatch, tmp_path, capsys):
    queue, _ = install(tmp_path)
    _, state_client = wire_command(monkeypatch, queue)
    before = state_client.puts
    writes = len(queue.client.writes)
    monkeypatch.setattr(command, '_prepare', lambda *_args, **_kwargs: pytest.fail('unexpected acquisition'))
    assert command.run(cli_args(tmp_path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['refill_needed'] is False and result['reason'] == 'READY_PRESENT'
    assert state_client.puts == before and len(queue.client.writes) == writes
    assert not (tmp_path / 'prepared').exists()


@pytest.mark.parametrize('paused,approved_sha,reason', [
    # Pausing revokes the continuous approval, so the exact-SHA gate also closes.
    (True, SHA, 'SHA_MISMATCH'), (False, 'b' * 40, 'SHA_MISMATCH'),
])
def test_refill_respects_pause_and_exact_sha(monkeypatch, tmp_path, capsys, paused, approved_sha, reason):
    queue = remote(tmp_path)
    store, client = wire_command(monkeypatch, queue, sha=approved_sha)
    if paused:
        state, _ = store.load_safety()
        manager(store).pause(expected_generation=state.generation, reason='fixture:pause', now=NOW)
    before = client.puts
    assert command.run(cli_args(tmp_path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['refill_needed'] is False and result['reason'] == f'SCHEDULE_{reason}'
    assert client.puts == before and not queue.client.writes


def test_read_only_preflight_emits_github_gate_without_building(monkeypatch, tmp_path, capsys):
    queue = remote(tmp_path)
    _, client = wire_command(monkeypatch, queue)
    before = client.puts
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    assert command.run(cli_args(tmp_path, '--check-refill') + ['--github-output']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['refill_needed'] is True and result['next_format'] == 'carousel'
    assert output.read_text() == 'refill_needed=true\n'
    assert client.puts == before and not queue.client.writes
    assert not (tmp_path / 'prepared').exists()


def test_needed_refill_builds_correct_next_format_and_installs(monkeypatch, tmp_path, capsys):
    queue = remote(tmp_path)
    _, client = wire_command(monkeypatch, queue)
    before = client.puts
    index = 0
    def prepare(format_name, directory, excluded, **kwargs):
        nonlocal index
        index += 1
        return content(directory, format_name, index)
    monkeypatch.setattr(command, '_prepare', prepare)
    assert command.run(cli_args(tmp_path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row['publication_format'] for row in result] == ['carousel', 'single', 'carousel']
    assert [row['state'] for row in result] == ['READY'] * 3
    assert client.puts == before
    assert all(key.startswith('feed-queue/v1/') for key, *_ in queue.client.writes)


def test_new_live_batch_between_check_and_install_is_preserved(monkeypatch, tmp_path):
    queue = remote(tmp_path)
    wire_command(monkeypatch, queue)
    index = 0
    def prepare(format_name, directory, excluded, **kwargs):
        nonlocal index
        index += 1
        if index == 1:
            other, _ = install(tmp_path / 'other')
            queue.client.objects.update(other.client.objects)
        return content(directory, format_name, index)
    monkeypatch.setattr(command, '_prepare', prepare)
    with pytest.raises(SystemExit) as error:
        command.run(cli_args(tmp_path))
    assert error.value.code == 1
    assert not queue.client.writes
    assert [row['publication_format'] for row in queue.status()] == ['carousel', 'single', 'carousel']


def test_unreadable_state_never_writes_true_gate_or_leaks_error(monkeypatch, tmp_path, capsys):
    queue = remote(tmp_path)
    wire_command(monkeypatch, queue)
    def unreadable(**kwargs):
        raise RuntimeError('SECRET_SENTINEL')
    monkeypatch.setattr(queue, 'refill_status', unreadable, raising=False)
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    with pytest.raises(SystemExit) as error:
        command.run(cli_args(tmp_path, '--check-refill') + ['--github-output'])
    assert error.value.code == 1 and not output.exists() and not queue.client.writes
    captured = capsys.readouterr()
    assert 'SECRET_SENTINEL' not in captured.out + captured.err


def test_pause_during_acquisition_blocks_installation(monkeypatch, tmp_path):
    queue = remote(tmp_path)
    store, _ = wire_command(monkeypatch, queue)
    index = 0
    def prepare(format_name, directory, excluded, **kwargs):
        nonlocal index
        index += 1
        if index == 1:
            state, _ = store.load_safety()
            manager(store).pause(expected_generation=state.generation, reason='fixture:pause', now=NOW)
        return content(directory, format_name, index)
    monkeypatch.setattr(command, '_prepare', prepare)
    with pytest.raises(SystemExit) as error:
        command.run(cli_args(tmp_path))
    assert error.value.code == 1 and not queue.client.writes
