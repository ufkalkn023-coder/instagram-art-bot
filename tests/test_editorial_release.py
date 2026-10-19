"""Release approval changes the exact code version, never content or cadence."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_continuous_feed_schedule import continuous_runtime
from tests.test_feed_schedule import NOW, SHA, manager

NEW_SHA = 'b' * 40


@pytest.mark.parametrize('cadence', ['daily_utc1717', 'hourly_utc17'])
def test_release_activation_preserves_cadence_cooldown_receipts_and_stock(monkeypatch, capsys, cadence):
    from scripts.release_editorial_feed import run
    store, client, _ = continuous_runtime(monkeypatch)
    control = manager(store)
    if cadence == 'hourly_utc17':
        control.enable_continuous(expected_generation=store.load_safety()[0].generation,
                                  approved_sha=SHA, main_sha=SHA, cadence=cadence,
                                  review_ref='fixture:hourly', now=NOW)
    before = store.load_safety()[0]
    receipts = store.load_receipts()[0].model_dump(mode='json')
    puts = client.puts
    assert run(['--expected-sha', NEW_SHA, '--expected-generation', str(before.generation),
                '--apply', '--evidence-ref', 'operator:release'], manager=control,
               github=SimpleNamespace(main_sha=lambda: NEW_SHA)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['approved_sha'] == NEW_SHA and value['cadence'] == cadence
    after = store.load_safety()[0]
    assert after.feed_schedule_control.next_eligible_at == before.feed_schedule_control.next_eligible_at
    assert after.feed_schedule_control.latest_successful_feed_at == before.feed_schedule_control.latest_successful_feed_at
    assert after.active_publication_state == before.active_publication_state
    assert store.load_receipts()[0].model_dump(mode='json') == receipts
    assert client.puts == puts + 1


def test_release_inspection_is_read_only(monkeypatch, capsys):
    from scripts.release_editorial_feed import run
    store, client, _ = continuous_runtime(monkeypatch)
    puts = client.puts
    assert run(['--expected-sha', NEW_SHA], manager=manager(store),
               github=SimpleNamespace(main_sha=lambda: NEW_SHA)) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'SHA_MISMATCH'
    assert client.puts == puts


@pytest.mark.parametrize('kind', ['main_changed', 'generation_changed', 'paused'])
def test_release_rejects_stale_or_paused_state_without_writing(monkeypatch, capsys, kind):
    from scripts.release_editorial_feed import run
    store, client, _ = continuous_runtime(monkeypatch)
    control = manager(store)
    if kind == 'paused':
        control.pause(expected_generation=store.load_safety()[0].generation, reason='operator:pause', now=NOW)
    generation = store.load_safety()[0].generation + (kind == 'generation_changed')
    puts = client.puts
    result = run(['--expected-sha', NEW_SHA, '--expected-generation', str(generation),
                  '--apply', '--evidence-ref', 'operator:release'], manager=control,
                 github=SimpleNamespace(main_sha=lambda: 'c' * 40 if kind == 'main_changed' else NEW_SHA))
    assert result == 1 and 'RELEASE_CONTROL_FAILED' in capsys.readouterr().err
    assert client.puts == puts


def test_release_workflow_is_manual_main_only_and_has_no_publishing_credentials():
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/release_editorial_feed.yml').read_text())
    assert set(workflow['on']) == {'workflow_dispatch'}
    job = workflow['jobs']['release']
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert 'DEPLOY_REVIEWED_EDITORIAL_FEED' in job['if']
    assert workflow['concurrency']['group'] == 'instagram-bot'
    assert workflow['concurrency']['cancel-in-progress'] is False
    for step in job['steps']:
        assert not any(name.startswith('INSTAGRAM') or 'GEMINI' in name for name in step.get('env', {}))
    step = next(step for step in job['steps'] if '--expected-sha' in step.get('run', ''))
    assert '${{' not in step['run']
    assert '--expected-generation' in step['run']
