import json

import pytest
from PIL import Image, ImageDraw

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import build_story_plan
from src.story_project import (
    create_project,
    load_project,
    update_project,
    RevisionConflict,
    QualityBlocked,
)


@pytest.fixture
def story(tmp_path):
    path = tmp_path / "input.jpg"
    image = Image.new("RGB", (1600, 1800), "ivory")
    draw = ImageDraw.Draw(image)
    for n in range(15):
        draw.rectangle(
            (n * 80, n * 90, n * 80 + 400, n * 90 + 400), fill=(n * 15, 80, 160)
        )
    image.save(path)
    source = SourceArtwork(
        artwork=NormalizedArtwork(
            source="cleveland",
            source_id="1",
            title="A Painting",
            artist_name="Known Artist",
            museum_name="Cleveland",
            is_public_domain=True,
            license="CC0",
            rights_status="CONFIRMED_OPEN_ACCESS",
        ),
        image_path=str(path),
    )
    plan = build_story_plan(
        [source], "single_study", detail_focus={"cleveland_1": [(0, 0, 0.5, 0.5)]}
    )
    return [source], plan


def test_project_keeps_source_identity_separate_and_reuses_unchanged_pages(
    tmp_path, story
):
    sources, plan = story
    root = tmp_path / "project"
    first = create_project(root, sources, plan)
    assert first["package"]["artwork_ids"] == ["cleveland_1"]
    assert len(first["package"]["slides"]) == len(plan.slides)
    assert first["rendered_count"] == len(plan.slides)
    changed = plan.model_dump(mode="json")
    next(s for s in changed["slides"] if s["role"] == "detail")["title"] = (
        "Look more closely"
    )
    second = update_project(root, changed, expected_revision=1)
    assert second["rendered_count"] == 1
    assert second["reused_count"] == len(plan.slides) - 1
    assert load_project(root).plan.slides[2].title == "Look more closely"


def test_invalid_edit_preserves_last_valid_project(tmp_path, story):
    sources, plan = story
    root = tmp_path / "project"
    create_project(root, sources, plan)
    original = (root / "project.json").read_bytes()
    changed = plan.model_dump(mode="json")
    next(s for s in changed["slides"] if s["role"] == "detail")["focus"] = [
        0,
        0.1,
        0.01,
        0.2,
    ]
    with pytest.raises(ValueError):
        update_project(root, changed, expected_revision=1)
    assert (root / "project.json").read_bytes() == original


def test_stale_revision_cannot_overwrite_changes(tmp_path, story):
    sources, plan = story
    root = tmp_path / "project"
    create_project(root, sources, plan)
    update_project(root, plan.model_dump(mode="json"), expected_revision=1)
    with pytest.raises(RevisionConflict):
        update_project(root, plan.model_dump(mode="json"), expected_revision=1)


def test_render_failure_preserves_plan_and_gallery(tmp_path, story):
    sources, plan = story
    root = tmp_path / "project"
    create_project(root, sources, plan)
    before = [(root / name).read_bytes() for name in ("project.json", "index.html")]
    changed = plan.model_dump(mode="json")
    changed["slides"][1]["title"] = "W" * 180
    with pytest.raises(QualityBlocked):
        update_project(root, changed, expected_revision=1)
    assert [
        (root / name).read_bytes() for name in ("project.json", "index.html")
    ] == before


def test_project_manifest_cannot_escape_source_directory(tmp_path, story):
    sources, plan = story
    root = tmp_path / "project"
    create_project(root, sources, plan)
    data = json.loads((root / "project.json").read_text())
    data["sources"][0]["image_path"] = "../input.jpg"
    (root / "project.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="project directory"):
        load_project(root)


def test_snapshot_write_failure_preserves_complete_previous_revision(
    tmp_path, story, monkeypatch
):
    import src.story_project as storage

    sources, plan = story
    root = tmp_path / "project"
    create_project(root, sources, plan)
    before = {
        name: (root / name).read_bytes()
        for name in ("project.json", "index.html", "report.json")
    }
    original = storage._atomic

    def failed_snapshot(path, content):
        if path.name == "report.json":
            raise OSError("Disk full")
        original(path, content)

    monkeypatch.setattr(storage, "_atomic", failed_snapshot)
    with pytest.raises(OSError):
        update_project(root, plan.model_dump(mode="json"), expected_revision=1)
    assert {name: (root / name).read_bytes() for name in before} == before


def test_concurrent_saves_cannot_both_commit_same_revision(tmp_path, story):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    sources, plan = story
    root = tmp_path / "project"
    create_project(root, sources, plan)

    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        results = list(
            pool.map(
                _save_revision_one, [(str(root), plan.model_dump(mode="json"))] * 2
            )
        )
    assert sorted(map(str, results)) == ["2", "conflict"]
    assert load_project(root).revision == 2


def test_corrupt_cached_media_is_rebuilt(tmp_path, story):
    sources, plan = story
    root = tmp_path / "project"
    report = create_project(root, sources, plan)
    path = root / report["package"]["slides"][1]["path"]
    path.write_bytes(b"not a JPEG")
    report = update_project(root, plan.model_dump(mode="json"), expected_revision=1)
    assert report["rendered_count"] == 1
    with Image.open(path) as image:
        assert image.size == (1080, 1350)


def test_review_escapes_user_copy_in_markup_and_bootstrap(tmp_path, story):
    sources, plan = story
    data = plan.model_dump(mode="json")
    data["public_title"] = '</script><script>alert("x")</script>'
    root = tmp_path / "project"
    from src.story_plan import StoryPlan

    create_project(root, sources, StoryPlan.model_validate(data))
    page = (root / "index.html").read_text()
    assert '</script><script>alert("x")</script>' not in page
    assert "&lt;/script&gt;" in page
    assert "\\u003c/script>" in page


def _save_revision_one(args):
    root, data = args
    from pathlib import Path

    try:
        return update_project(Path(root), data, expected_revision=1)["revision"]
    except RevisionConflict:
        return "conflict"


def test_failed_creation_can_be_corrected_and_retried(tmp_path, story):
    from src.story_plan import StoryPlan

    sources, plan = story
    root = tmp_path / "project"
    invalid = plan.model_dump(mode="json")
    invalid["slides"][1]["title"] = "W" * 180
    with pytest.raises(QualityBlocked):
        create_project(root, sources, StoryPlan.model_validate(invalid))
    assert not root.exists()
    report = create_project(root, sources, plan)
    assert report["revision"] == 1
    assert load_project(root).plan == plan
