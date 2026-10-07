import importlib
import json

from tests.test_feed_analytics import NOW, publication, snapshot


def test_local_report_has_no_network_or_credential_dependency_and_preserves_inputs(tmp_path, capsys):
    assert importlib.util.find_spec("scripts.report_feed_analytics") is not None, "Feed report CLI is missing"
    command = importlib.import_module("scripts.report_feed_analytics")
    pub = publication()
    history, snapshots = tmp_path / "history.json", tmp_path / "snapshots.json"
    history.write_text(json.dumps({"publications": [pub]}))
    snapshots.write_text(json.dumps([snapshot(pub)]))
    before = history.read_bytes(), snapshots.read_bytes()
    assert command.run(["--history", str(history), "--snapshots", str(snapshots),
                        "--now", NOW.isoformat(), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["feed_publications"] == 1
    assert output["summary"]["complete_windows"] == 1
    assert before == (history.read_bytes(), snapshots.read_bytes())
