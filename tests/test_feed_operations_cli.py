import importlib
import json

from tests.test_feed_analytics import NOW


def test_offline_combined_report_needs_no_credentials_or_writes(tmp_path, capsys):
    command = importlib.import_module('scripts.report_feed_status')
    files = {}
    for name, value in [('schedule', {'status': 'WAITING_COOLDOWN', 'next_format': 'carousel'}),
                        ('history', {'publications': []}), ('snapshots', [])]:
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps(value))
        files[name] = path
    before = {path: path.read_bytes() for path in files.values()}
    assert command.run(['--schedule', str(files['schedule']), '--history', str(files['history']),
                        '--snapshots', str(files['snapshots']), '--now', NOW.isoformat()]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['analytics']['freshness'] == 'cold_start'
    assert {path: path.read_bytes() for path in files.values()} == before


def test_local_status_cannot_silently_write_notification_ledger(tmp_path):
    import pytest
    command = importlib.import_module('scripts.report_feed_status')
    with pytest.raises(SystemExit) as result:
        command.run(['--notify'])
    assert result.value.code == 2
