import logging
from types import SimpleNamespace

import pytest

import main
from src.instagram_image import (
    InstagramImagePublishability,
    InstagramImagePublishabilityReason,
    PreparedSingleImage,
    SingleImageProcessing,
)


def _artwork(identifier, quality_score=90.0):
    return {
        "id": identifier,
        "title": f"Artwork {identifier}",
        "artist": "Artist",
        "date": "1900",
        "museum": "Museum",
        "local_image_path": f"{identifier}.jpg",
        "quality_score": quality_score,
        "selection_score": quality_score + 1,
        "alt_text": "Artwork alt text",
        "medium": "Oil on canvas",
        "classification": "Painting",
    }


def _result(reason, *, publishable=False):
    return InstagramImagePublishability(
        publishable=publishable,
        reason=reason,
        width=1200 if publishable else 200,
        height=1200 if publishable else 100,
        aspect_ratio=1.0 if publishable else 2.0,
        image_format="JPEG",
        file_size=1_234,
        exif_orientation=1,
        encoded_width=1200 if publishable else 200,
        encoded_height=1200 if publishable else 100,
    )


def _prepared(reason, *, path=None):
    publishable = path is not None
    result = _result(reason, publishable=publishable)
    return PreparedSingleImage(
        path=path,
        source=result,
        publishability=result,
        processing=(
            SingleImageProcessing.ZERO_TOUCH
            if publishable
            else SingleImageProcessing.NONE
        ),
        source_bytes_preserved=publishable,
        compatibility_conversion=False,
    )


def _install_publish_pipeline(monkeypatch, candidates, prepared_by_path):
    events = []
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(
        main.history_tracker, "get_grid_color_tone", lambda **kwargs: "warm"
    )
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda *args, **kwargs: iter(candidates),
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda path, output_path: prepared_by_path[path],
    )
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_artwork",
        lambda artwork: events.append(("reserve", artwork["id"])),
    )
    monkeypatch.setattr(
        main.history_tracker,
        "start_publication_attempt",
        lambda ids, *args: events.append(("publishing", tuple(ids))) or 1,
    )
    monkeypatch.setattr(main.history_tracker, "record_publish_response", lambda *args: 1)
    monkeypatch.setattr(
        main.history_tracker,
        "confirm_artwork",
        lambda artwork_id, media_id: events.append(
            ("confirm", artwork_id, media_id)
        ),
    )
    monkeypatch.setattr(
        main.content_diversity, "select_content_type", lambda history: "SINGLE_ARTWORK"
    )
    monkeypatch.setattr(
        main.gemini_ai, "analyze_artwork", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        main.image_processor,
        "upload_temp_media",
        lambda path: events.append(("upload", path))
        or "https://example.test/validated.jpg",
    )
    def publish(**kwargs):
        kwargs["before_publish"]("container-1", ())
        events.append(("publish", kwargs["media_url"]))
        return "media-123"

    monkeypatch.setattr(main.instagram_poster, "post_to_instagram_graph_api", publish)
    return events


@pytest.mark.parametrize(
    "reason",
    [
        InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE,
        InstagramImagePublishabilityReason.UNSUPPORTED_TRANSPARENCY,
        InstagramImagePublishabilityReason.SIZE_UNRECOVERABLE,
    ],
)
def test_single_ineligible_candidate_defers_to_later_publishable_candidate(
    monkeypatch, reason, caplog
):
    first = _artwork("aic_first", quality_score=94.0)
    second = _artwork("aic_second", quality_score=91.0)
    second["_single_selection_breakdown"] = {
        "quality": 91.0,
        "museum": -1.0,
        "region": 0.0,
        "orientation": -0.75,
        "artist": 0.0,
        "visual_category": -0.5,
        "discovery": 2.0,
        "serendipity": 1.25,
        "final": 92.0,
    }
    caplog.set_level(logging.INFO, logger=main.__name__)
    events = _install_publish_pipeline(
        monkeypatch,
        [first, second],
        {
            first["local_image_path"]: _prepared(reason),
            second["local_image_path"]: _prepared(
                InstagramImagePublishabilityReason.SUPPORTED_AS_IS,
                path="aic_second.jpg",
            ),
        },
    )

    resolution = main.run_single_post(
        SimpleNamespace(dry_run=False, image_url=None, pinterest=False)
    )

    assert resolution.result is main.SinglePostResolutionCode.READY
    assert resolution.attempted == 2
    assert resolution.single_ineligible == 1
    assert [item.reason for item in resolution.diagnostics] == [
        reason,
        InstagramImagePublishabilityReason.SUPPORTED_AS_IS,
    ]
    assert ("reserve", "aic_first") not in events
    assert ("reserve", "aic_second") in events
    assert ("publish", "https://example.test/validated.jpg") in events
    assert ("confirm", "aic_second", "media-123") in events
    assert first["quality_score"] == 94.0
    assert second["published_orientation"] == "SQUARE"
    assert (
        "single_selection_breakdown canonical_id=aic_second quality=91.00 "
        "museum=-1.00 region=+0.00 orientation=-0.75 artist=+0.00 "
        "visual_category=-0.50 discovery=+2.00 serendipity=+1.25 final=92.00"
        in caplog.text
    )


def test_single_candidate_attempt_budget_returns_clean_structured_outcome(monkeypatch):
    candidates = [_artwork(f"aic_{index}") for index in range(7)]
    prepared_paths = {
        candidate["local_image_path"]: _prepared(
            InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE
        )
        for candidate in candidates
    }
    attempted_paths = []
    mutation_events = []
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(
        main.history_tracker, "get_grid_color_tone", lambda **kwargs: "warm"
    )
    monkeypatch.setattr(main.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda *args, **kwargs: iter(candidates),
    )
    monkeypatch.setattr(
        main,
        "prepare_single_instagram_image",
        lambda path, output: attempted_paths.append(path) or prepared_paths[path],
    )
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_artwork",
        lambda artwork: mutation_events.append(artwork["id"]),
    )

    resolution = main.run_single_post(
        SimpleNamespace(dry_run=False, image_url=None, pinterest=False)
    )

    assert (
        resolution.result
        is main.SinglePostResolutionCode.NO_SINGLE_POST_PUBLISHABLE_CANDIDATE
    )
    assert resolution.attempted == main.SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT
    assert resolution.single_ineligible == main.SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT
    assert len(attempted_paths) == main.SINGLE_POST_CANDIDATE_ATTEMPT_LIMIT
    assert mutation_events == []


def test_actual_processing_failure_is_typed_and_not_reserved(monkeypatch):
    candidate = _artwork("aic_invalid")
    invalid = _prepared(InstagramImagePublishabilityReason.INVALID_IMAGE)
    reserved = []
    monkeypatch.setattr(main.history_tracker, "get_posted_ids", lambda: set())
    monkeypatch.setattr(
        main.art_fetcher,
        "iter_single_post_candidates",
        lambda *args, **kwargs: iter([candidate]),
    )
    monkeypatch.setattr(
        main, "prepare_single_instagram_image", lambda *args: invalid
    )
    monkeypatch.setattr(
        main.history_tracker,
        "reserve_artwork",
        lambda artwork: reserved.append(artwork["id"]),
    )

    with pytest.raises(main.SinglePostProcessingError) as raised:
        main.run_single_post(
            SimpleNamespace(dry_run=False, image_url=None, pinterest=False)
        )

    assert raised.value.canonical_id == "aic_invalid"
    assert (
        raised.value.result.reason
        is InstagramImagePublishabilityReason.INVALID_IMAGE
    )
    assert reserved == []
