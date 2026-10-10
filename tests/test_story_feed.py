import importlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import build_story_plan
from src.story_project import create_project, load_project, update_project


def make_story_project(tmp_path):
    path = tmp_path / "source.jpg"
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
            source_id="101",
            title="A Painting",
            artist_name="Known Artist",
            museum_name="Cleveland",
            artwork_url="https://www.clevelandart.org/art/101",
            is_public_domain=True,
            license="CC0",
            rights_status="CONFIRMED_OPEN_ACCESS",
        ),
        image_path=str(path),
    )
    plan = build_story_plan(
        [source], "single_study", detail_focus={"cleveland_101": [(0, 0, 0.5, 0.5)]}
    )
    root = tmp_path / "story"
    create_project(root, [source], plan)
    return root


@pytest.fixture
def project(tmp_path):
    return make_story_project(tmp_path)


def integration():
    from src import story_project

    assert hasattr(story_project, "approve_story"), (
        "Revision-bound review approval is missing"
    )
    return importlib.import_module("src.story_feed")


def approved_content(project):
    module = integration()
    from src.story_project import approve_story

    approve_story(project, expected_revision=1)
    return module.prepare_story_content(project)


def test_unreviewed_story_cannot_enter_publication_queue(project):
    module = integration()
    with pytest.raises(ValueError, match="review"):
        module.prepare_story_content(project)


def test_reviewed_single_study_has_one_source_and_ordered_pages(project):
    content = approved_content(project)
    assert content.publication_format == "carousel"
    assert content.publication_ids == ("cleveland_101",)
    assert len(content.media_paths) == 4
    assert [p.role for p in content.story_delivery.pages] == [
        "cover",
        "artwork",
        "detail",
        "closing",
    ]
    assert all(
        p.artwork_ids == ("cleveland_101",) for p in content.story_delivery.pages
    )
    assert "Known Artist" in content.caption and "CC0" in content.caption
    assert "https://www.clevelandart.org/art/101" in content.caption
    assert all(Path(p).is_file() for p in content.media_paths)


def test_edit_invalidates_previous_review(project):
    approved_content(project)
    current = load_project(project)
    data = current.plan.model_dump(mode="json")
    data["public_title"] = "Painted forms in blue"
    update_project(project, data, expected_revision=1)
    with pytest.raises(ValueError, match="review"):
        integration().prepare_story_content(project)


@pytest.mark.parametrize("target", ["source", "page"])
def test_changed_bytes_invalidate_review(project, target):
    content = approved_content(project)
    path = Path(
        load_project(project).sources[0].image_path
        if target == "source"
        else content.media_paths[1]
    )
    with path.open("ab") as handle:
        handle.write(b"changed after review")
    with pytest.raises(ValueError, match="review"):
        integration().prepare_story_content(project)


def test_review_rejects_stale_revision_without_record(project):
    integration()
    from src.story_project import RevisionConflict, approve_story

    with pytest.raises(RevisionConflict):
        approve_story(project, expected_revision=2)
    assert not (project / ".story-review.json").exists()


def test_story_content_cannot_change_delivered_caption_or_source_membership(project):
    content = approved_content(project)
    with pytest.raises(ValueError):
        replace(content, artworks=({**content.artworks[0], "id": "cleveland_other"},))
    with pytest.raises(ValueError):
        replace(content, media_paths=content.media_paths[:1])


def test_export_is_portable_and_preserves_reviewed_page_order(project, tmp_path):
    content = approved_content(project)
    output = tmp_path / "export"
    integration().export_story_content(project, output)
    manifest = json.loads((output / "content.json").read_text())
    assert manifest["content_kind"] == "story"
    assert manifest["story_delivery"]["source_ids"] == ["cleveland_101"]
    assert [asset["path"] for asset in manifest["assets"]] == [
        f"media-{n}.jpg" for n in range(4)
    ]
    assert [
        Path(output / asset["path"]).read_bytes() for asset in manifest["assets"]
    ] == [Path(p).read_bytes() for p in content.media_paths]
    with pytest.raises(FileExistsError):
        integration().export_story_content(project, output)


def build_story_queue(project, tmp_path):
    from src.feed_queue import PreparedFeedQueue
    from tests.test_feed_queue import NOW, content

    reviewed = approved_content(project)
    queue = PreparedFeedQueue(tmp_path / "queue")
    index = 0

    def prepare(format_name, directory, excluded):
        nonlocal index
        index += 1
        return reviewed if index == 1 else content(directory, format_name, index * 100)

    queue.build(target=3, first_format="carousel", prepare=prepare, now=NOW)
    return queue, reviewed


def test_local_queue_round_trip_preserves_story_kind_sources_and_pages(
    project, tmp_path
):
    from tests.test_feed_queue import NOW

    queue, reviewed = build_story_queue(project, tmp_path)
    claim = queue.claim("carousel", protected_ids=set(), owner="test", now=NOW)
    assert claim.content.publication_ids == ("cleveland_101",)
    assert claim.content.story_delivery == reviewed.story_delivery
    assert [Path(p).read_bytes() for p in claim.content.media_paths] == [
        Path(p).read_bytes() for p in reviewed.media_paths
    ]
    assert queue.status()[0]["content_kind"] == "story"


def test_story_queue_respects_protected_source_and_keeps_single_slot(project, tmp_path):
    from tests.test_feed_queue import NOW

    queue, _ = build_story_queue(project, tmp_path)
    claim = queue.claim(
        "carousel", protected_ids={"cleveland_101"}, owner="test", now=NOW
    )
    assert claim.content.publication_ids[0] != "cleveland_101"
    assert queue.status()[0]["reason"] == "protected_artwork"
    assert queue.status()[1]["state"] == "READY"


def test_private_queue_round_trip_revalidates_unique_sources(project, tmp_path):
    from tests.test_feed_queue import NOW
    from tests.test_r2_feed_queue import remote

    queue, reviewed = build_story_queue(project, tmp_path)
    remote_queue = remote(tmp_path)
    remote_queue.install(queue, now=NOW)
    fresh = remote(tmp_path, remote_queue.client, "fresh")
    seen = []
    claim = fresh.claim(
        "carousel",
        protected_ids=set(),
        owner="test",
        now=NOW,
        rights_revalidator=lambda c: seen.extend(c.publication_ids) or True,
    )
    assert seen == ["cleveland_101"]
    assert claim.content.story_delivery == reviewed.story_delivery
    assert len(claim.content.media_paths) == 4


@pytest.mark.parametrize(
    "phase,expected", [("success", "CONSUMED"), ("ambiguous", "QUARANTINED")]
)
def test_story_publishes_ordered_pages_once_with_one_source_receipt(
    project, tmp_path, monkeypatch, phase, expected
):
    import main
    from datetime import datetime
    from types import SimpleNamespace
    from src import history_tracker, instagram_poster
    from src.feed_schedule import FeedScheduleManager
    from tests.test_feed_schedule import auth
    from tests.test_feed_queue import NOW
    from tests.test_single_feed_runtime import single_runtime
    from tests.test_instagram_poster import FakeResponse

    queue, reviewed = build_story_queue(project, tmp_path)
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)
    original_post = instagram_poster.requests.post
    children = []

    def post(url, **kwargs):
        if url.endswith("/media_publish"):
            return original_post(url, **kwargs)
        sent.append((url, kwargs["data"]))
        children.append(f"container-{len(children) + 1}")
        return FakeResponse(200, {"id": children[-1]})

    monkeypatch.setattr(instagram_poster.requests, "post", post)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(main, "datetime", Clock)
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    args = SimpleNamespace(
        dry_run=False, prepared_queue=queue.directory, prepared_queue_r2=False
    )
    if phase == "ambiguous":
        with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
            main.run_feed_with_queue(args, main.ProductionMode.CAROUSEL, authorization)
    else:
        main.run_feed_with_queue(args, main.ProductionMode.CAROUSEL, authorization)
    assert queue.status()[0]["state"] == expected
    assert len([url for url, _ in sent if url.endswith("/media_publish")]) == 1
    state, _ = store.load_safety()
    rows = state.active_publication_state.posted_artworks
    assert len(rows) == 1 and rows[0]["id"] == "cleveland_101"
    assert len(rows[0]["child_container_ids"]) == 4
    assert len(state.active_publication_state.consumed_authorizations) == 1
    if phase == "success":
        publication = state.operational_projection.publications[0]
        assert publication["type"] == "carousel" and publication["artwork_ids"] == [
            "cleveland_101"
        ]
        assert publication["story_delivery"] == reviewed.story_delivery.model_dump(
            mode="json"
        )
        assert state.operational_projection.grid_publication_count == 1
        receipts, _ = store.load_receipts()
        receipt = receipts.records[-1]
        assert receipt.publication_type == "carousel"
        assert len(receipt.artwork_positions) == 1
        assert receipt.story_delivery == reviewed.story_delivery
    else:
        assert rows[0]["status"] == "AMBIGUOUS"
        units = history_tracker.list_unresolved_publication_units(limit=1)
        assert len(units) == 1 and units[0].status.value == "AMBIGUOUS"
        assert units[0].artwork_ids == ("cleveland_101",)


def test_review_rejects_plan_modified_without_matching_render(project):
    integration()
    from src.story_project import approve_story

    path = project / "project.json"
    data = json.loads(path.read_text())
    data["plan"]["slides"][1]["body"] = (
        "Changed text that is absent from the inspected image"
    )
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="render|review"):
        approve_story(project, expected_revision=1)


def test_story_upload_snapshot_is_bound_to_reviewed_bytes(project):
    content = approved_content(project)
    module = integration()
    assert hasattr(module, "snapshot_story_media"), (
        "Reviewed upload snapshot is missing"
    )
    expected = [Path(p).read_bytes() for p in content.media_paths]
    with module.snapshot_story_media(content) as frozen:
        Path(content.media_paths[0]).write_bytes(b"concurrent editor change")
        assert [Path(p).read_bytes() for p in frozen.media_paths] == expected
        assert frozen.media_paths[0] != content.media_paths[0]
        assert frozen.story_delivery == content.story_delivery
    assert not Path(frozen.media_paths[0]).exists()


def test_corrupt_multisource_story_does_not_fall_back_to_legacy_reconciliation(
    project, monkeypatch
):
    from src import history_tracker
    from tests.test_carousel_history import _history_backend
    from src.story_delivery import StoryDelivery

    content = approved_content(project)
    ids = [f"cleveland_{n}" for n in range(6)]
    data = content.story_delivery.model_dump(mode="json")
    data.update(
        source_ids=ids,
        narrative="thematic_selection",
        source_sha256={i: "a" * 64 for i in ids},
        artwork_sha256={i: "b" * 64 for i in ids},
        pages=[
            dict(id="cover", role="cover", artwork_ids=[ids[0]], sha256="a" * 64),
            *[
                dict(
                    id=f"artwork-{n}", role="artwork", artwork_ids=[i], sha256="a" * 64
                )
                for n, i in enumerate(ids)
            ],
            dict(id="closing", role="closing", artwork_ids=[ids[0]], sha256="a" * 64),
        ],
    )
    delivery = StoryDelivery.model_validate(data)
    history, _ = _history_backend(monkeypatch)
    history_tracker.reserve_story(
        [{**content.artworks[0], "id": i} for i in ids],
        publication_metadata={"story_delivery": delivery.model_dump(mode="json")},
    )
    history["posted_artworks"][0]["publication_metadata"]["story_delivery"]["pages"][0][
        "artwork_ids"
    ] = ["cleveland_unknown"]
    unit = history_tracker.list_unresolved_publication_units(limit=1)[0]
    assert unit.status is None


def test_story_training_uses_source_and_narrative_without_legacy_featured_count(
    project,
):
    from src.engagement_learning import _publication_feature_keys
    from tests.test_story_analytics import NOW

    content = approved_content(project)
    publication = {
        "type": "carousel",
        "story_delivery": content.story_delivery.model_dump(mode="json"),
    }
    artwork = {**content.artworks[0], "publication_role": "COVER"}
    keys = _publication_feature_keys(publication, [artwork], NOW, None)
    assert "narrative:single_study" in keys
    assert "artist:known artist" in keys
    assert not any(k.startswith("featured_count:") for k in keys)


def reserved_story(project, tmp_path, monkeypatch):
    from src import history_tracker
    from src.feed_schedule import FeedScheduleManager
    from tests.test_feed_schedule import auth
    from tests.test_single_feed_runtime import single_runtime

    content = approved_content(project)
    store, _, _, _ = single_runtime(monkeypatch, tmp_path)
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    identity = history_tracker.reserve_story(
        content.artworks,
        publication_metadata=content.publication_metadata,
        authorization=authorization,
    )
    return store, content, identity, authorization


def test_reserved_story_delivery_cannot_be_rewritten_before_publish(
    project, tmp_path, monkeypatch
):
    from src.publication_state import StateValidationError, seal

    store, _, _, _ = reserved_story(project, tmp_path, monkeypatch)
    state, etag = store.load_safety()
    candidate = state.model_dump(mode="json")
    candidate["generation"] += 1
    candidate["active_publication_state"]["posted_artworks"][0]["publication_metadata"][
        "story_delivery"
    ]["reviewed_digest"] = "f" * 64
    with pytest.raises(StateValidationError, match="story|Story"):
        store.update_safety(seal(candidate), etag)
    assert store.load_safety()[0].generation == state.generation


def test_crossed_boundary_story_cannot_lose_its_ordered_child_evidence(
    project, tmp_path, monkeypatch
):
    from src import history_tracker
    from src.publication_state import StateValidationError, seal

    store, _, identity, authorization = reserved_story(project, tmp_path, monkeypatch)
    history_tracker.start_publication_attempt(
        ["cleveland_101"],
        "parent",
        [f"child-{n}" for n in range(4)],
        expected_publication_id=identity,
        authorization=authorization,
    )
    state, etag = store.load_safety()
    candidate = state.model_dump(mode="json")
    candidate["generation"] += 1
    del candidate["active_publication_state"]["posted_artworks"][0][
        "child_container_ids"
    ]
    with pytest.raises(
        (
            ValueError,
            RuntimeError,
            StateValidationError,
            history_tracker.CorruptedHistoryError,
        ),
        match="child|Child",
    ):
        store.update_safety(seal(candidate), etag)
