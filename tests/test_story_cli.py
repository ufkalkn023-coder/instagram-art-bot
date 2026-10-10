import json

import pytest

from scripts import prepare_feed_queue
from src.story_project import approve_story
from tests.test_story_feed import make_story_project


@pytest.fixture
def story_project(tmp_path):
    # Reuse the real, rendered project fixture from the publication integration tests.
    root = make_story_project(tmp_path)
    approve_story(root, expected_revision=1)
    return root


def test_preview_story_approve_and_export_commands(
    story_project, tmp_path, monkeypatch, capsys
):
    from scripts import preview_story

    monkeypatch.setattr(
        "sys.argv",
        [
            "preview_story",
            "approve",
            "--project",
            str(story_project),
            "--expected-revision",
            "1",
        ],
    )
    preview_story.main()
    assert json.loads(capsys.readouterr().out)["revision"] == 1

    output = tmp_path / "export"
    monkeypatch.setattr(
        "sys.argv",
        [
            "preview_story",
            "export",
            "--project",
            str(story_project),
            "--output",
            str(output),
        ],
    )
    preview_story.main()
    assert json.loads((output / "content.json").read_text())["content_kind"] == "story"


def test_story_projects_are_prevalidated_before_any_single_acquisition(
    story_project, tmp_path, monkeypatch, capsys
):
    from src.story_project import load_project, update_project

    current = load_project(story_project)
    plan = current.plan.model_dump(mode="json")
    plan["public_title"] = "Changed after approval"
    update_project(story_project, plan, expected_revision=1)
    acquisitions = []
    monkeypatch.setattr(
        prepare_feed_queue,
        "_prepare",
        lambda *args, **kwargs: acquisitions.append(args) or None,
    )
    with pytest.raises(SystemExit):
        prepare_feed_queue.run(
            [
                "--directory",
                str(tmp_path / "queue"),
                "--skip-keychain",
                "--first-format",
                "carousel",
                "--story-project",
                str(story_project),
            ]
        )
    assert "ValueError" in capsys.readouterr().err
    assert acquisitions == []


def test_story_replaces_carousel_slot_and_keeps_other_slots_on_planner(
    story_project, tmp_path, monkeypatch, capsys
):
    from tests.test_feed_queue import content

    acquisitions = []

    def prepare(format_name, directory, excluded, **kwargs):
        acquisitions.append(format_name)
        return content(directory, format_name, len(acquisitions))

    monkeypatch.setattr(prepare_feed_queue, "_prepare", prepare)
    result = prepare_feed_queue.run(
        [
            "--directory",
            str(tmp_path / "queue"),
            "--skip-keychain",
            "--first-format",
            "carousel",
            "--story-project",
            str(story_project),
        ]
    )
    assert result == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["content_kind"] == "story"
    assert [row["publication_format"] for row in rows] == [
        "carousel",
        "single",
        "carousel",
    ]
    assert acquisitions == ["single", "carousel"]


def test_story_projects_rejected_before_remote_queue_initialization(
    tmp_path, monkeypatch
):
    initialized = []
    monkeypatch.setattr(
        prepare_feed_queue, "R2PreparedFeedQueue", lambda *_: initialized.append(True)
    )
    with pytest.raises(SystemExit):
        prepare_feed_queue.run(
            [
                "--directory",
                str(tmp_path / "queue"),
                "--r2",
                "--status",
                "--story-project",
                str(tmp_path / "story"),
            ]
        )
    assert initialized == []


def test_normal_preparation_excludes_sources_reserved_for_future_story_slots(
    story_project, tmp_path, monkeypatch
):
    from tests.test_feed_queue import content

    calls = []

    def prepare(format_name, directory, excluded, **kwargs):
        calls.append(set(excluded))
        return content(directory, format_name, len(calls))

    monkeypatch.setattr(prepare_feed_queue, "_prepare", prepare)
    prepare_feed_queue.run(
        [
            "--directory",
            str(tmp_path / "queue"),
            "--skip-keychain",
            "--first-format",
            "single",
            "--story-project",
            str(story_project),
        ]
    )
    assert "cleveland_101" in calls[0]
