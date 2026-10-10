import json
import subprocess
import sys
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.publication_state import canonical_bytes, seal
from src.feed_dashboard import render_feed_dashboard


ROOT = Path(__file__).resolve().parents[1]


def _inputs(tmp_path):
    schedule = {
        "status": "READY", "reason": "READY", "ready": True,
        "next_format": "single", "next_attempt_at": "2026-10-12T17:17:00Z",
        "next_eligible_at": "2026-10-12T15:00:00Z",
        "cadence": "hourly_utc17",
        "last_successful_feed_at": "2026-10-10T17:17:00Z",
        "last_successful_feed_id": "publication-1",
    }
    packages = []
    for state in ("READY", "CLAIMED", "CONSUMED", "QUARANTINED"):
        content = {"publication_format": "single", "source_ids": ["source-1"],
                   "assets": [{"path": f"{state}.jpg"}]}
        packages.append({"id": str(uuid.uuid4()), "state": state,
                         "owner": "runner" if state in ("CLAIMED", "CONSUMED") else None,
                         "reason": None, "created_at": "2026-10-11T10:00:00Z",
                         "expires_at": "2026-10-13T00:00:00Z", "content": content,
                         "content_sha256": hashlib.sha256(canonical_bytes(content)).hexdigest()})
    queue = seal({"schema_version": 1, "packages": packages})
    for name, value in (("schedule", schedule), ("history", {"publications": []}),
                        ("snapshots", []), ("queue", queue)):
        (tmp_path / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    return tmp_path


def _run(tmp_path, output, extra=()):
    args = [sys.executable, str(ROOT / "scripts/report_feed_dashboard.py"),
            "--schedule", str(tmp_path / "schedule.json"),
            "--history", str(tmp_path / "history.json"),
            "--snapshots", str(tmp_path / "snapshots.json"),
            "--queue", str(tmp_path), "--output", str(output),
            "--now", "2026-10-11T12:00:00+03:00", *extra]
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


def test_local_dashboard_shows_schedule_actual_success_queue_and_unavailable_metrics(tmp_path):
    _inputs(tmp_path)
    output = tmp_path / "dashboard.html"

    result = _run(tmp_path, output)

    assert result.returncode == 0, result.stderr
    page = output.read_text(encoding="utf-8")
    assert "2026-10-12 18:00:00 +03" in page
    assert "2026-10-12 20:17:00 +03" in page
    assert "yayın garantisi değildir" in page
    assert "2026-10-10 20:17:00 +03" in page
    for state in ("Hazır", "Sahiplenildi", "Tüketildi", "Karantinada"):
        assert state in page
    assert "kullanılamıyor" in page
    assert "son karşılaştırılabilir kayıt: kullanılamıyor." in page
    assert "<html lang=\"tr\"" in page


def test_cooldown_and_blocked_schedule_states_are_visible(tmp_path):
    _inputs(tmp_path)
    schedule_path = tmp_path / "schedule.json"
    schedule = json.loads(schedule_path.read_text())
    output = tmp_path / "dashboard.html"

    for status, reason in (("WAITING_COOLDOWN", "WAITING_COOLDOWN"),
                           ("BLOCKED", "PUBLICATION_STATE_BLOCKED")):
        schedule_path.write_text(json.dumps({**schedule, "status": status, "reason": reason}),
                                 encoding="utf-8")
        result = _run(tmp_path, output)
        assert result.returncode == 0, result.stderr
        assert status in output.read_text(encoding="utf-8")
        output.unlink()


def test_dashboard_preserves_cooldown_seconds_and_explains_hourly_schedule(tmp_path):
    _inputs(tmp_path)
    schedule_path = tmp_path / "schedule.json"
    schedule = json.loads(schedule_path.read_text())
    schedule.update(status="WAITING_COOLDOWN", reason="WAITING_COOLDOWN",
                    next_eligible_at="2026-10-11T21:00:12Z")
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    output = tmp_path / "dashboard.html"
    result = _run(tmp_path, output)
    assert result.returncode == 0, result.stderr
    page = output.read_text(encoding="utf-8")
    assert "2026-10-12 00:00:12 +03" in page
    assert "Yayın aralığının dolması bekleniyor" in page
    assert "Her saatin 17. dakikasında" in page


def test_real_cohort_schema_shows_reach_and_save_rate_with_sample_caveat():
    now = datetime(2026, 10, 7, 19, tzinfo=timezone.utc)
    published_at = now - timedelta(hours=80)
    history = {"publications": [{"id": "p-1", "media_id": "m-1", "type": "single",
                                 "artwork_ids": ["aic_1"], "posted_at": published_at.isoformat()}]}
    snapshots = [{"publication_id": "p-1", "media_id": "m-1", "target_age_hours": 72,
                  "captured_at": (published_at + timedelta(hours=72)).isoformat(),
                  "metrics": {"reach": 100, "saved": 20}}]

    page = render_feed_dashboard({}, [], history, snapshots, now=now)

    assert "100 · 1 gözlem" in page
    assert "20.0% · 1 gözlem" in page
    assert "1 kullanılabilir / 1 uygun yayın" in page
    assert "minimum örneklem 5" in page


def test_queue_report_input_renders_validated_operator_rows(tmp_path):
    _inputs(tmp_path)
    report = [{"id": "package-1", "state": "CONSUMED", "publication_format": "carousel",
               "expires_at": "2026-10-13T00:00:00Z", "title": "Operatör girdisi",
               "source_ids": ["a", "b"], "page_count": 7}]
    (tmp_path / "queue-report.json").write_text(json.dumps(report), encoding="utf-8")
    output = tmp_path / "dashboard.html"
    args = [sys.executable, str(ROOT / "scripts/report_feed_dashboard.py"),
            "--schedule", str(tmp_path / "schedule.json"), "--history", str(tmp_path / "history.json"),
            "--snapshots", str(tmp_path / "snapshots.json"), "--queue-report",
            str(tmp_path / "queue-report.json"), "--output", str(output)]

    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    page = output.read_text(encoding="utf-8")
    assert "Operatör girdisi" in page
    assert ">2</td>" in page and ">7</td>" in page
    assert "snapshot" in page.lower() or "anlık görüntüsü" in page


def test_local_cli_never_constructs_external_stores(tmp_path, monkeypatch):
    import scripts.report_feed_dashboard as cli

    _inputs(tmp_path)
    output = tmp_path / "dashboard.html"
    def forbidden(*args, **kwargs):
        raise AssertionError("local mode attempted external storage access")
    monkeypatch.setattr(cli, "FeedScheduleManager", forbidden)
    monkeypatch.setattr(cli, "InsightsStorage", forbidden)
    monkeypatch.setattr(cli, "R2PreparedFeedQueue", forbidden)

    result = cli.run(["--schedule", str(tmp_path / "schedule.json"),
                      "--history", str(tmp_path / "history.json"),
                      "--snapshots", str(tmp_path / "snapshots.json"),
                      "--output", str(output)])

    assert result == 0
    assert output.is_file()


def test_r2_uses_authoritative_publication_state_history_and_guards_writes(tmp_path, monkeypatch):
    import scripts.report_feed_dashboard as cli

    authoritative = {"publications": [{"id": "from-state"}]}
    captured = {}
    class Store:
        client = object()
        def load_safety(self):
            return object(), None
    class ScheduleManager:
        store = Store()
        def status(self, *, expected_sha, now):
            assert expected_sha == "abc123"
            return {"status": "READY", "next_eligible_at": None}
    class Insights:
        def __init__(self, client, bucket_name):
            assert isinstance(client, cli.ReadOnlyClient)
            assert bucket_name == "analytics-bucket"
        def load_history(self):
            raise AssertionError("legacy Insights history must not be used")
        def load_all_snapshots(self):
            return []
    class QueueStore:
        def __init__(self, directory):
            self.client = object()
        def _read_manifest(self):
            return {"packages": []}, None
    def capture(schedule, queue, history, snapshots, **kwargs):
        captured["history"] = history
        return "<html></html>"
    monkeypatch.setattr(cli, "FeedScheduleManager", ScheduleManager)
    monkeypatch.setattr(cli, "InsightsStorage", Insights)
    monkeypatch.setattr(cli, "R2PreparedFeedQueue", QueueStore)
    monkeypatch.setattr(cli, "render_feed_dashboard", capture)
    monkeypatch.setattr("src.publication_state.history_view", lambda state: authoritative)
    from types import SimpleNamespace
    monkeypatch.setattr("src.r2_media._load_configuration",
                        lambda *, require_public_url: SimpleNamespace(bucket_name="analytics-bucket"))
    monkeypatch.setattr("src.r2_media._get_s3_client", lambda config: object())
    output = tmp_path / "r2-dashboard.html"

    result = cli.run(["--r2", "--expected-sha", "abc123", "--output", str(output)])

    assert result == 0
    assert captured["history"] is authoritative
    assert output.read_text(encoding="utf-8").strip() == "<html></html>"
    with pytest.raises(RuntimeError, match="read-only"):
        cli.ReadOnlyClient(object()).put_object(Bucket="b", Key="k")


def test_local_dashboard_escapes_untrusted_schedule_text(tmp_path):
    _inputs(tmp_path)
    schedule_path = tmp_path / "schedule.json"
    schedule = json.loads(schedule_path.read_text())
    schedule["reason"] = '<script>alert("x")</script>'
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    output = tmp_path / "dashboard.html"

    result = _run(tmp_path, output)

    assert result.returncode == 0, result.stderr
    page = output.read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert '<script>alert("x")</script>' not in page


def test_local_dashboard_refuses_to_overwrite_existing_output(tmp_path):
    _inputs(tmp_path)
    output = tmp_path / "dashboard.html"
    output.write_text("keep me", encoding="utf-8")

    result = _run(tmp_path, output)

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == "keep me"
