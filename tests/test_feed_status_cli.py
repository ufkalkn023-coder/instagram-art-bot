"""Readiness emits a strict advisory gate without publishing or state writes."""

import json

from scripts import manage_feed_schedule
from tests.test_feed_schedule import NOW, SHA, fixture_store, initialize, manager


def test_status_reads_state_without_mutation(capsys):
    store, client = fixture_store()
    initialize(store)
    manager(store).enable_continuous(
        expected_generation=2, approved_sha=SHA, main_sha=SHA,
        review_ref="fixture:hourly", cadence="hourly_utc17", now=NOW,
    )
    before = client.puts
    assert manage_feed_schedule.main(
        ["status", "--expected-sha", SHA], manager=manager(store), now=NOW,
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True and result["status"] == "READY"
    assert result["next_format"] == "carousel"
    assert client.puts == before


def test_github_readiness_output_is_explicit_boolean(monkeypatch, tmp_path, capsys):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    class WaitingManager:
        def status(self, **kwargs):
            return {"ready": False, "status": "WAITING_COOLDOWN"}

    assert manage_feed_schedule.main(
        ["status", "--github-output"], manager=WaitingManager(), now=NOW,
    ) == 0
    assert output.read_text() == "ready=false\nstatus=WAITING_COOLDOWN\n"
    assert json.loads(capsys.readouterr().out)["ready"] is False


def test_status_does_not_leak_unreadable_store_error(capsys):
    class UnreadableManager:
        def status(self, **kwargs):
            raise RuntimeError("SECRET_SENTINEL must never appear")

    assert manage_feed_schedule.main(["status"], manager=UnreadableManager()) == 1
    output = capsys.readouterr()
    assert "RuntimeError" in output.err
    assert "SECRET_SENTINEL" not in output.err + output.out


def test_malformed_readiness_never_emits_true(monkeypatch, tmp_path):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    class MalformedManager:
        def status(self, **kwargs):
            return {"ready": "false", "status": "READY"}

    assert manage_feed_schedule.main(
        ["status", "--github-output"], manager=MalformedManager(),
    ) == 1
    assert not output.exists()
