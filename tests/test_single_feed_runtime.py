"""Single Feed uses the same durable scheduled lifecycle as a carousel."""

from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest
import requests

import main
from src import feed_schedule, history_tracker, instagram_poster, r2_media
from tests.test_feed_schedule import NOW
from tests.test_feed_schedule_runtime import runtime
from tests.test_instagram_poster import FakeResponse


def single_runtime(monkeypatch, tmp_path, *, phase="success"):
    store, client = runtime(monkeypatch)
    source = tmp_path / "raw_artwork.jpg"
    Image.new("RGB", (1080, 1350), "navy").save(source)
    monkeypatch.setattr(main.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main.config, "OUTPUT_IMAGE_PATH", str(tmp_path / "output_post.jpg"))
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "123456")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "fixture-token")
    artwork = {
        "id": "aic_987654", "title": "Test Artwork", "artist": "Artist unknown",
        "date": "1900", "museum": "Fixture Museum", "local_image_path": str(source),
        "alt_text": "A blue artwork", "region": "europe",
    }
    def candidate_iterator(_, *, max_candidates):
        yield artwork

    monkeypatch.setattr(main.art_fetcher, "iter_single_post_candidates", candidate_iterator)
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda **_: None)

    def upload(path, publication_id):
        if phase == "staging":
            raise RuntimeError("staging unavailable")
        return r2_media.TempMediaUpload(
            f"images/publications/{publication_id}/20261004171700_{'a' * 32}.jpg",
            "https://media.example/single.jpg", publication_id,
        )

    monkeypatch.setattr(main.image_processor, "upload_temp_media", upload)
    monkeypatch.setattr(main, "_get_published_instagram_permalink", lambda *_: None)
    # Cleanup is an external operation; lifecycle transitions remain real.
    monkeypatch.setattr(main, "_cleanup_authoritatively_expired_media", lambda *_, **__: None)
    monkeypatch.setattr(r2_media, "rollback_temp_media_uploads", lambda *_: None)
    monkeypatch.setattr(r2_media, "cleanup_publication_media", lambda *_, **__: SimpleNamespace(complete=False))
    requests_sent = []

    def post(url, **kwargs):
        requests_sent.append((url, kwargs["data"]))
        if url.endswith("/media_publish"):
            if phase == "ambiguous":
                raise requests.Timeout()
            if phase == "rejection":
                return FakeResponse(400, {"error": {"message": "rejected"}})
            return FakeResponse(200, {"id": "fixture-single-media"})
        return FakeResponse(200, {"id": "fixture-container"})

    monkeypatch.setattr(instagram_poster.requests, "post", post)
    monkeypatch.setattr(instagram_poster.requests, "get", lambda *_, **__: FakeResponse(200, {"status_code": "FINISHED"}))
    monkeypatch.setattr(instagram_poster.time, "sleep", lambda _: None)
    return store, client, requests_sent, artwork


@pytest.mark.parametrize("mode", ["single", "auto"])
def test_single_schedule_publishes_one_image_and_one_receipt(monkeypatch, tmp_path, mode):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    if mode == "auto":
        monkeypatch.setattr(history_tracker, "get_recent_publications", lambda: [
            {"type": "carousel", "posted_at": "2026-10-02T17:17:00Z"},
        ])
    assert main.main(["--mode", mode]) == 0
    assert len(sent) == 2
    assert sent[0][1]["image_url"] == "https://media.example/single.jpg"
    assert "Artist unknown" in sent[0][1]["caption"]
    assert "Fixture Museum" in sent[0][1]["caption"]
    assert "children" not in sent[0][1]
    state, _ = store.load_safety()
    ledger, _ = store.load_receipts()
    records = state.active_publication_state.posted_artworks
    assert len(records) == 1 and records[0]["status"] == "PUBLISHED"
    assert records[0]["publication_type"] == "single"
    assert ledger.records[-1].publication_type == "single"
    assert len(ledger.records[-1].artwork_positions) == 1
    control = state.feed_schedule_control
    assert control.paused and control.permits[-1].status == "SUCCESS"
    assert control.latest_successful_feed_at == "2026-10-04T17:17:00Z"
    assert len(state.active_publication_state.consumed_authorizations) == 1


@pytest.mark.parametrize("phase,status,attempts", [
    ("staging", "EXPIRED", 0),
    ("rejection", "EXPIRED", 1),
    ("ambiguous", "AMBIGUOUS", 1),
])
def test_single_failure_preserves_lifecycle_and_never_retries(monkeypatch, tmp_path, phase, status, attempts):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)
    assert main.main(["--mode", "single"]) == 1
    assert sum(url.endswith("/media_publish") for url, _ in sent) == attempts
    state, _ = store.load_safety()
    assert state.active_publication_state.posted_artworks[0]["status"] == status
    assert state.feed_schedule_control.paused
    assert len(store.load_receipts()[0].records) == 1


def test_single_dry_run_does_not_reserve_stage_or_publish(monkeypatch, tmp_path):
    store, client, sent, _ = single_runtime(monkeypatch, tmp_path)
    before = client.puts
    assert main.main(["--dry-run", "--mode", "single"]) == 0
    assert client.puts == before and sent == []
    assert store.load_safety()[0].active_publication_state.posted_artworks == []


def test_single_boundary_failure_blocks_publish_request(monkeypatch, tmp_path):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)

    def failed_boundary(*_, **__):
        raise RuntimeError("CAS conflict at boundary")

    monkeypatch.setattr(history_tracker, "start_publication_attempt", failed_boundary)
    assert main.main(["--mode", "single"]) == 1
    assert len(sent) == 1 and not sent[0][0].endswith("/media_publish")
    state, _ = store.load_safety()
    assert state.active_publication_state.posted_artworks[0]["status"] == "EXPIRED"
    assert state.feed_schedule_control.paused
    assert len(store.load_receipts()[0].records) == 1


def test_single_confirmation_failure_retains_published_media_evidence(monkeypatch, tmp_path):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)

    def failed_confirmation(*_, **__):
        raise RuntimeError("receipt store unavailable")

    monkeypatch.setattr(history_tracker, "confirm_artworks_and_record_publication", failed_confirmation)
    assert main.main(["--mode", "single"]) == 1
    assert sum(url.endswith("/media_publish") for url, _ in sent) == 1
    state, _ = store.load_safety()
    record = state.active_publication_state.posted_artworks[0]
    assert record["status"] == "PUBLISHING"
    assert record["publish_response_media_id"] == "fixture-single-media"
    assert state.feed_schedule_control.paused
    assert len(store.load_receipts()[0].records) == 1


def test_single_no_permit_stops_before_acquisition(monkeypatch, tmp_path):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    feed_schedule.FeedScheduleManager(store, owned_media=lambda **_: set()).pause(
        expected_generation=3, reason="stopped", now=NOW,
    )
    monkeypatch.setattr(main.art_fetcher, "iter_single_post_candidates", lambda *_args, **_kwargs: pytest.fail("unpermitted acquisition"))
    assert main.main(["--mode", "single"]) == 1
    assert sent == []


def test_single_skips_unsuitable_candidate_and_publishes_one_valid_image(monkeypatch, tmp_path):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    unsuitable_path = tmp_path / "too_wide.jpg"
    valid_path = tmp_path / "valid.jpg"
    Image.new("RGB", (2200, 900), "red").save(unsuitable_path)
    Image.new("RGB", (1080, 1350), "green").save(valid_path)
    unsuitable = {
        "id": "aic_111111", "title": "Unsuitable Artwork", "artist": "Artist",
        "museum": "Fixture Museum", "local_image_path": str(unsuitable_path),
    }
    valid = {
        "id": "aic_222222", "title": "Valid Artwork", "artist": "Artist",
        "museum": "Fixture Museum", "local_image_path": str(valid_path),
    }
    generator_closed = []
    analyzed = []

    def candidate_iterator(_posted_ids, *, max_candidates):
        assert max_candidates == main.art_fetcher.SINGLE_DIVERSITY_FINALIST_TARGET
        try:
            yield unsuitable
            yield valid
        finally:
            generator_closed.append(True)

    monkeypatch.setattr(main.art_fetcher, "iter_single_post_candidates", candidate_iterator)
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda **kwargs: analyzed.append(kwargs) or None)
    uploaded_paths = []
    prepared_paths = []
    prepare = main.image_processor.create_feed_post

    def prepare_and_record(raw_path, **kwargs):
        result = prepare(raw_path, **kwargs)
        prepared_paths.append((raw_path, result))
        return result

    monkeypatch.setattr(main.image_processor, "create_feed_post", prepare_and_record)

    def upload(path, publication_id):
        uploaded_paths.append(path)
        return r2_media.TempMediaUpload(
            f"images/publications/{publication_id}/20261004171700_{'a' * 32}.jpg",
            "https://media.example/single.jpg", publication_id,
        )

    monkeypatch.setattr(main.image_processor, "upload_temp_media", upload)

    assert main.main(["--mode", "single"]) == 0
    assert len(analyzed) == 1 and analyzed[0]["image_path"] == str(valid_path)
    assert len(sent) == 2
    assert "Valid Artwork" in sent[0][1]["caption"]
    assert len(uploaded_paths) == 1
    assert prepared_paths == [(str(valid_path), uploaded_paths[0])]
    assert Path(uploaded_paths[0]).is_file()
    assert generator_closed == [True]
    state = store.load_safety()[0]
    assert len(state.active_publication_state.posted_artworks) == 1
    assert state.active_publication_state.posted_artworks[0]["id"] == "aic_222222"


def test_single_candidate_pool_exhaustion_stops_before_gemini_or_publication(monkeypatch, tmp_path):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    unsuitable_paths = []
    for index, dimensions in enumerate(((2200, 900), (900, 1600))):
        source = tmp_path / f"unsuitable_{index}.jpg"
        Image.new("RGB", dimensions, "red").save(source)
        unsuitable_paths.append(str(source))
    candidates = [
        {"id": f"aic_{index + 1:06d}", "title": f"Unsuitable {index}",
         "artist": "Artist", "museum": "Fixture Museum", "local_image_path": path}
        for index, path in enumerate(unsuitable_paths)
    ]
    generator_closed = []

    def candidate_iterator(_posted_ids, *, max_candidates):
        assert max_candidates == main.art_fetcher.SINGLE_DIVERSITY_FINALIST_TARGET
        try:
            yield from candidates
        finally:
            generator_closed.append(True)

    monkeypatch.setattr(main.art_fetcher, "iter_single_post_candidates", candidate_iterator)
    monkeypatch.setattr(main.gemini_ai, "analyze_artwork", lambda **_: pytest.fail("Gemini must not run"))
    monkeypatch.setattr(main.image_processor, "upload_temp_media", lambda *_: pytest.fail("upload must not run"))

    assert main.main(["--mode", "single"]) == 1
    assert generator_closed == [True]
    assert sent == []
    assert store.load_safety()[0].active_publication_state.posted_artworks == []


@pytest.mark.parametrize("publications,expected", [
    ([], "carousel"),
    ([{"type": "carousel", "posted_at": "2026-10-01T17:54:07Z"}], "single"),
    ([{"type": "single", "posted_at": "2026-10-01T17:54:07Z"}], "carousel"),
    ([{"type": "single", "posted_at": "2026-10-02T17:54:07Z"},
      {"type": "carousel", "posted_at": "2026-10-01T17:54:07Z"}], "carousel"),
    ([{"type": "carousel", "posted_at": "2026-10-01T17:54:07Z"},
      {"type": "reel", "posted_at": "2026-10-02T17:54:07Z"}], "single"),
])
def test_auto_alternates_using_latest_successful_feed(monkeypatch, publications, expected):
    monkeypatch.setattr(history_tracker, "get_recent_publications", lambda: publications)
    assert main._resolve_production_mode(SimpleNamespace(mode="auto")).value == expected


def test_auto_history_failure_stops_before_reconciliation_or_acquisition(monkeypatch):
    runtime(monkeypatch)
    monkeypatch.setattr(history_tracker, "get_recent_publications", lambda: (_ for _ in ()).throw(RuntimeError("unreadable history")))
    monkeypatch.setattr(main.publication_reconciliation, "reconcile_publications", lambda **_: pytest.fail("unreadable format state mutated history"))
    assert main.main(["--mode", "auto"]) == 1


def test_auto_refreshes_format_after_reconciliation(monkeypatch):
    runtime(monkeypatch)
    publications = [{"type": "single", "posted_at": "2026-10-01T17:17:00Z"}]
    monkeypatch.setattr(history_tracker, "get_recent_publications", lambda: publications)

    def reconcile(**_):
        publications.append({"type": "carousel", "posted_at": "2026-10-02T17:17:00Z"})
        return SimpleNamespace(
            inspected=1, confirmed_published=1, confirmed_not_published=0,
            still_ambiguous=0, errors=0,
        )

    calls = []
    monkeypatch.setattr(main.publication_reconciliation, "reconcile_publications", reconcile)
    monkeypatch.setattr(main, "run_single_post", lambda *_: calls.append("single"))
    monkeypatch.setattr(main, "run_carousel_post", lambda *_: calls.append("carousel"))
    assert main.main(["--mode", "auto"]) == 0
    assert calls == ["single"]


def test_single_caption_keeps_source_credits_when_story_is_long():
    caption = main._format_single_caption(
        {"title": "Study", "artist": "Attributed to Jane", "date": "c. 1900", "museum": "Museum"},
        {"caption": "A visual observation. " * 200, "hashtags": "#Art"},
    )
    assert len(caption) <= 2200
    assert "Attributed to Jane" in caption and "c. 1900" in caption
    assert caption.endswith("#Art")


def test_single_caption_fallback_does_not_invent_missing_metadata():
    caption = main._format_single_caption({"title": "Study", "museum": "Museum"}, None)
    assert "Study" in caption and "Museum" in caption
    assert "Artist:" not in caption and "Date:" not in caption
