"""Operator CLI is read-only by default and keeps production data intact."""

import json

import pytest

from scripts import manage_feed_schedule
from src import feed_schedule, publication_state
from tests.test_feed_schedule import (
    NOW,
    SHA,
    PUB,
    armed,
    auth,
    fixture_store,
    initialize,
    manager,
)


class GitHub:
    def main_sha(self):
        return SHA

    def audit_runs(self, permit):
        return [
            {
                "id": 123,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
                "event": "schedule",
                "path": feed_schedule.FEED_WORKFLOW,
                "head_branch": "main",
                "head_sha": SHA,
                "repository": {"full_name": feed_schedule.FEED_REPOSITORY},
                "created_at": "2026-10-04T17:17:00Z",
            }
        ]


def test_inspect_is_read_only(capsys):
    store, client = fixture_store()
    assert (
        manage_feed_schedule.main(
            ["inspect"], manager=manager(store), github=GitHub(), now=NOW
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schedule_control"] is None
    assert client.puts == 0


def test_cli_mutations_require_explicit_apply_and_generation():
    with pytest.raises(SystemExit):
        manage_feed_schedule.main(["pause"], manager=object())


def test_cli_arm_pause_and_revoke():
    store, _ = fixture_store()
    initialize(store)
    now = NOW.replace(hour=16)
    assert (
        manage_feed_schedule.main(
            [
                "arm",
                "--apply",
                "--expected-generation",
                "2",
                "--approved-sha",
                SHA,
                "--slot",
                "2026-10-04T17:17:00Z",
                "--expires-at",
                "2026-10-04T18:17:00Z",
                "--evidence-ref",
                "operator:test",
            ],
            manager=manager(store),
            github=GitHub(),
            now=now,
        )
        == 0
    )
    assert (
        manage_feed_schedule.main(
            [
                "revoke",
                "--apply",
                "--expected-generation",
                "3",
                "--reason",
                "stop",
            ],
            manager=manager(store),
            github=GitHub(),
            now=now,
        )
        == 0
    )
    assert store.load_safety()[0].feed_schedule_control.permits[-1].status == "REVOKED"
    assert (
        manage_feed_schedule.main(
            [
                "pause",
                "--apply",
                "--expected-generation",
                "4",
                "--reason",
                "review",
            ],
            manager=manager(store),
            github=GitHub(),
            now=now,
        )
        == 0
    )


def test_ack_preserves_failure_and_requires_new_explicit_arm():
    store, _ = fixture_store()
    armed(store)
    manager(store).admit(auth(), now=NOW)
    manager(store).record_outcome(auth(), now=NOW)
    before = store.load_safety()[0].feed_schedule_control.permits[-1].outcome
    assert (
        manage_feed_schedule.main(
            [
                "acknowledge",
                "--apply",
                "--expected-generation",
                "5",
                "--evidence-ref",
                "operator:reviewed",
            ],
            manager=manager(store),
            github=GitHub(),
            now=NOW.replace(hour=19),
        )
        == 0
    )
    state, _ = store.load_safety()
    assert state.feed_schedule_control.paused
    assert state.feed_schedule_control.permits[-1].outcome == before
    assert state.feed_schedule_control.permits[-1].acknowledgement.run_ids == [123]
    assert len(state.published_artwork_protection.entries) == 634
    assert store.load_receipts()[0].records[0].publication_id == PUB


def test_ack_before_window_closed_fails_without_a_write():
    store, client = fixture_store()
    armed(store)
    before = client.puts
    assert (
        manage_feed_schedule.main(
            [
                "acknowledge",
                "--apply",
                "--expected-generation",
                "3",
                "--evidence-ref",
                "review",
            ],
            manager=manager(store),
            github=GitHub(),
            now=NOW,
        )
        == 1
    )
    assert client.puts == before


def test_generation_conflict_fails_without_writes():
    store, client = fixture_store()
    initialize(store)
    assert (
        manage_feed_schedule.main(
            [
                "pause",
                "--apply",
                "--expected-generation",
                "1",
                "--reason",
                "stop",
            ],
            manager=manager(store),
            github=GitHub(),
            now=NOW,
        )
        == 1
    )
    assert client.puts == 1


def test_wrong_main_sha_cannot_arm():
    store, client = fixture_store()
    initialize(store)
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).arm(
            expected_generation=2,
            approved_sha="b" * 40,
            main_sha=SHA,
            slot=NOW,
            expires_at=NOW.replace(hour=18),
            now=NOW.replace(hour=16),
            review_ref="review",
        )
    assert client.puts == 1


def test_schedule_evidence_cannot_be_deleted():
    store, _ = fixture_store()
    armed(store)
    state, etag = store.load_safety()
    value = state.model_dump(mode="json")
    value["feed_schedule_control"] = None
    value["generation"] += 1
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(publication_state.seal(value), etag)


def test_github_audit_rejects_changed_or_truncated_pagination(monkeypatch):
    from src.models import FeedSchedulePermit

    store, _ = fixture_store()
    armed(store)
    permit = store.load_safety()[0].feed_schedule_control.permits[-1]
    assert isinstance(permit, FeedSchedulePermit)
    evidence = manage_feed_schedule.GitHubEvidence("fake")
    page = {"total_count": 101, "workflow_runs": GitHub().audit_runs(None)}
    monkeypatch.setattr(evidence, "_get", lambda *_, **__: page)
    with pytest.raises(feed_schedule.FeedScheduleError):
        evidence.audit_runs(permit)


@pytest.mark.parametrize("total", [1000, -1, True, None])
def test_github_audit_rejects_unbounded_or_malformed_counts(monkeypatch, total):
    store, _ = fixture_store()
    armed(store)
    evidence = manage_feed_schedule.GitHubEvidence("fake")
    monkeypatch.setattr(
        evidence, "_get", lambda *_, **__: {"total_count": total, "workflow_runs": []}
    )
    with pytest.raises(feed_schedule.FeedScheduleError):
        evidence.audit_runs(store.load_safety()[0].feed_schedule_control.permits[-1])


def test_github_evidence_only_uses_authenticated_get(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))

        class Response:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": SHA},
                }

        return Response()

    monkeypatch.setattr(manage_feed_schedule.requests, "get", get)
    assert manage_feed_schedule.GitHubEvidence("fake").main_sha() == SHA
    assert calls[0][0].endswith("/git/ref/heads/main")
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == 10


def test_missing_github_credentials_fail_closed():
    evidence = manage_feed_schedule.GitHubEvidence()
    evidence.token = ""
    with pytest.raises(feed_schedule.FeedScheduleError):
        evidence.main_sha()


def test_inspect_reports_expired_permit_effectively_paused_without_writing():
    from datetime import timedelta

    store, client = fixture_store()
    armed(store)
    before = client.puts
    report = manager(store).inspect(now=NOW + timedelta(minutes=60))
    assert report["effective_paused"] is True
    assert report["effective_pause_reason"] == "SCHEDULE_WINDOW_CLOSED"
    assert client.puts == before


def test_unknown_github_conclusion_cannot_acknowledge():
    from datetime import timedelta

    store, client = fixture_store()
    armed(store)
    runs = GitHub().audit_runs(None)
    runs[0]["conclusion"] = "UNKNOWN_UNSAFE"
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).acknowledge(
            expected_generation=3,
            evidence_ref="review",
            audit_runs=lambda _: runs,
            now=NOW + timedelta(hours=2),
        )
    assert client.puts == before
