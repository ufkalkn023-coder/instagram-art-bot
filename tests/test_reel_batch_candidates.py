import json

from PIL import Image

from src.reel_batch_candidates import (
    BATCH_CANDIDATE_VERSION,
    build_batch_candidate_queue,
    load_reel_production_history,
    resolve_batch_candidate_limit,
)


def _handoff(root, source_id: str, *, color=(80, 110, 140), size=(2400, 1600)):
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    image = assets / f"met_{source_id}.jpg"
    Image.new("RGB", size, color=color).save(image, "JPEG")
    path = root / f"met_{source_id}.json"
    path.write_text(json.dumps({
        "canonicalId": f"met_{source_id}", "source": "met", "title": f"Artwork {source_id}",
        "artist": f"Artist {source_id}", "date": "1900", "medium": "Oil on canvas",
        "museum": "Metropolitan Museum of Art", "classification": "Painting",
        "imagePath": str(image), "imageWidth": size[0], "imageHeight": size[1],
        "rightsStatus": "CONFIRMED_PUBLIC_DOMAIN",
    }), encoding="utf-8")


def test_batch_queue_reuses_approved_layers_and_is_deterministic(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for source_id in ("c", "a", "b"):
        _handoff(source, source_id)

    first = build_batch_candidate_queue(target=2, candidate_limit=3, source_directory=source, output_directory=tmp_path / "first")
    second = build_batch_candidate_queue(target=2, candidate_limit=3, source_directory=source, output_directory=tmp_path / "second")

    assert first.as_contract()["batchCandidateVersion"] == BATCH_CANDIDATE_VERSION
    assert first.candidate_count == 3
    assert [item["canonicalId"] for item in first.candidates] == [item["canonicalId"] for item in second.candidates]
    assert all((tmp_path / "first" / "handoffs" / f"{item['canonicalId']}.json").is_file() for item in first.candidates)


def test_candidate_limit_must_cover_the_single_central_target():
    assert resolve_batch_candidate_limit(4, environment={"REEL_BATCH_CANDIDATE_LIMIT": "4"}) == 4
    for value in (3, 0, "bad", True):
        try:
            resolve_batch_candidate_limit(4, value)
        except ValueError:
            continue
        raise AssertionError("invalid candidate limit was accepted")


def test_production_history_excludes_a_previously_produced_canonical_id(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _handoff(source, "old")
    _handoff(source, "fresh")
    history_path = tmp_path / "reel-production-history.json"
    history_path.write_text(json.dumps({
        "version": "reel-production-history-v1",
        "entries": [{
            "canonicalId": "met_old", "artist": "Artist old", "museum": "Metropolitan Museum of Art", "source": "met",
            "template": "one-artwork", "batchId": "batch-old", "status": "RENDERED", "qcPassedAt": "2026-08-23T00:00:00Z",
            "renderedAt": "2026-08-23T00:01:00Z", "renderPath": "/safe/met_old.mp4",
        }],
    }), encoding="utf-8")

    queue = build_batch_candidate_queue(
        target=2, candidate_limit=2, source_directory=source, output_directory=tmp_path / "output", reel_history_path=history_path,
    )

    assert [item["canonicalId"] for item in queue.candidates] == ["met_fresh"]
    assert load_reel_production_history(history_path)[0]["canonicalId"] == "met_old"


def test_corrupt_production_history_fails_closed(tmp_path):
    history_path = tmp_path / "reel-production-history.json"
    history_path.write_text("{not-json", encoding="utf-8")

    try:
        load_reel_production_history(history_path)
    except ValueError:
        return
    raise AssertionError("corrupt Reel production history was accepted")


def test_missing_production_history_is_an_empty_history(tmp_path):
    assert load_reel_production_history(tmp_path / "not-created.json") == ()


def test_history_is_excluded_before_the_limited_preselector_shortlist(tmp_path):
    """A 24-item acquired pool must not lose slots to retained historical handoffs."""
    source = tmp_path / "source"
    source.mkdir()
    historical_ids = [f"history_{index}" for index in range(6)]
    for source_id in historical_ids:
        # Ensure history records would otherwise occupy the top technical ranks.
        _handoff(source, source_id, size=(4000, 4000))
    for index in range(24):
        _handoff(source, f"fresh_{index:02d}", size=(1080, 720))

    history_path = tmp_path / "reel-production-history.json"
    history_path.write_text(json.dumps({
        "version": "reel-production-history-v1",
        "entries": [{
            "canonicalId": f"met_{source_id}", "artist": f"Artist {source_id}",
            "museum": "Metropolitan Museum of Art", "source": "met", "template": "one-artwork",
            "batchId": f"batch-{source_id}", "status": "RENDERED", "qcPassedAt": "2026-08-23T00:00:00Z",
            "renderedAt": "2026-08-23T00:01:00Z", "renderPath": f"/safe/{source_id}.mp4",
        } for source_id in historical_ids],
    }), encoding="utf-8")

    queue = build_batch_candidate_queue(
        target=4, candidate_limit=8, source_directory=source,
        output_directory=tmp_path / "output", reel_history_path=history_path,
    )

    assert queue.stage_counts.acquired_usable == 24
    assert queue.stage_counts.preselector_eligible == 24
    assert queue.stage_counts.portfolio_available == 8
    assert queue.stage_counts.queued == 8
    assert queue.candidate_count == 8
    assert not {f"met_{source_id}" for source_id in historical_ids}.intersection(
        candidate["canonicalId"] for candidate in queue.candidates
    )
