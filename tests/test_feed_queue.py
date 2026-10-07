import importlib
from datetime import datetime, timedelta, timezone

from PIL import Image
import pytest

from src.feed_content import PreparedFeedContent

NOW = datetime(2026, 10, 7, 19, tzinfo=timezone.utc)


def queue_type():
    assert importlib.util.find_spec("src.feed_queue") is not None, "Prepared Feed queue is missing"
    return importlib.import_module("src.feed_queue").PreparedFeedQueue


def content(directory, format_name, index=0, *, duplicate=False):
    artworks, paths = [], []
    count = 1 if format_name == "single" else 6
    for n in range(count):
        paths.append(str(directory / f"asset-{n}.jpg"))
        Image.new("RGB", (1080, 1350), "navy").save(paths[-1])
        artworks.append({"id": f"aic_{0 if duplicate else index}-{n}", "title": f"Work {n}",
                         "artist": "Known artist", "is_public_domain": True,
                         "rights_status": "CONFIRMED_PUBLIC_DOMAIN", "source": "aic"})
    return PreparedFeedContent(format_name, tuple(artworks), tuple(paths), "Museum art credits")


def build(tmp_path, *, target=3):
    queue = queue_type()(tmp_path / "queue")
    calls = []
    def prepare(format_name, directory, excluded):
        calls.append((format_name, set(excluded)))
        return content(directory, format_name, len(calls))
    queue.build(target=target, first_format="carousel", prepare=prepare, now=NOW)
    return queue, calls


def test_bounded_queue_alternates_and_preparation_excludes_earlier_packages(tmp_path):
    queue, calls = build(tmp_path)
    assert [name for name, _ in calls] == ["carousel", "single", "carousel"]
    assert len(calls[1][1]) == 6
    assert len(calls[2][1]) == 7
    assert [row["state"] for row in queue.status()] == ["READY"] * 3
    with pytest.raises(ValueError):
        queue_type()(tmp_path / "bad").build(target=6, first_format="single", prepare=lambda *_: None, now=NOW)


def test_claim_has_one_owner_and_uncertain_outcome_is_never_reused(tmp_path):
    queue, _ = build(tmp_path)
    claim = queue.claim("carousel", protected_ids=set(), owner="run:1", now=NOW)
    assert claim.content.publication_format == "carousel"
    assert queue.status()[0]["state"] == "CLAIMED"
    # A crashed owner has no lease expiry that could silently rearm its package.
    queue.finish(claim.package_id, owner="run:1", successful=False)
    assert queue.status()[0]["state"] == "QUARANTINED"
    second = queue.claim("carousel", protected_ids=set(), owner="run:2", now=NOW)
    assert second.package_id != claim.package_id
    with pytest.raises(RuntimeError):
        queue.finish(second.package_id, owner="run:1", successful=True)
    queue.finish(second.package_id, owner="run:2", successful=True)
    assert queue.status()[2]["state"] == "CONSUMED"
    assert queue.claim("carousel", protected_ids=set(), owner="run:3", now=NOW) is None


def test_expiry_and_current_duplicate_protection_are_rechecked(tmp_path):
    queue, _ = build(tmp_path)
    protected = {"aic_1-0"}
    claim = queue.claim("carousel", protected_ids=protected, owner="run:1", now=NOW)
    assert "aic_1-0" not in claim.content.publication_ids
    assert queue.status()[0]["state"] == "QUARANTINED"
    assert queue.claim("single", protected_ids=set(), owner="run:2", now=NOW + timedelta(days=15)) is None
    assert queue.status()[1]["reason"] == "expired"


def test_changed_media_bytes_are_quarantined_before_claim(tmp_path):
    queue, _ = build(tmp_path)
    asset = queue.directory / queue.status()[0]["id"] / "media-0.jpg"
    asset.write_bytes(b"tampered")
    queue.claim("carousel", protected_ids=set(), owner="run:1", now=NOW)
    assert any(row["state"] == "QUARANTINED" and row["reason"] == "invalid_content" for row in queue.status())


def test_builder_rejects_duplicate_artworks(tmp_path):
    queue = queue_type()(tmp_path / "queue")
    def duplicated(format_name, directory, excluded):
        return content(directory, format_name, duplicate=True)
    with pytest.raises(ValueError):
        queue.build(target=3, first_format="single", prepare=duplicated, now=NOW)
    assert queue.status() == []


def test_builder_rejects_unconfirmed_rights_without_publishing_a_manifest(tmp_path):
    queue = queue_type()(tmp_path / "queue")
    def unconfirmed(format_name, directory, excluded):
        prepared = content(directory, format_name)
        prepared.artworks[0]["rights_status"] = "UNKNOWN"
        return prepared
    with pytest.raises(ValueError, match="confirmed source rights"):
        queue.build(target=3, first_format="single", prepare=unconfirmed, now=NOW)
    assert queue.status() == []


def test_queue_lock_conflict_does_not_rearm_or_mutate_entries(tmp_path):
    import fcntl
    queue, _ = build(tmp_path)
    before = queue.status()
    with (tmp_path / "queue" / ".queue.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError):
            queue.claim("carousel", protected_ids=set(), owner="run:1", now=NOW)
    assert queue.status() == before
