"""Offline scheduled orchestration and the real non-retrying publisher boundary."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

import main
from src import (
    feed_schedule,
    history_tracker,
    instagram_poster,
    publication_state,
    r2_media,
)
from tests.test_feed_schedule import NOW, armed, auth, fixture_store, ids, reserve
from tests.test_instagram_poster import FakeResponse


def runtime(monkeypatch):
    store, client = fixture_store()
    armed(store)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(feed_schedule, "datetime", Clock)
    monkeypatch.setattr(history_tracker, "datetime", Clock)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    monkeypatch.setattr(r2_media, "list_owned_publication_ids", lambda **_: set())
    monkeypatch.setattr(main, "validate_carousel_production_preflight", lambda: {})
    monkeypatch.setattr(main, "load_workflow_authorization", auth)
    monkeypatch.setattr(main, "_snapshot_generated_artifacts", lambda: set())
    monkeypatch.setattr(main, "_cleanup_new_generated_artifacts", lambda _: None)
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **_: SimpleNamespace(
            inspected=0,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=0,
            errors=0,
        ),
    )
    return store, client


@pytest.mark.parametrize(
    "phase",
    [
        "selection",
        "staging",
        "child",
        "parent",
        "rejection",
        "ambiguous",
        "cancelled",
        "success",
        "outcome_write",
    ],
)
def test_every_admitted_application_outcome_pauses_without_retry(monkeypatch, phase):
    store, client = runtime(monkeypatch)
    publishes = []
    creations = []

    def post(url, **kwargs):
        if url.endswith("/media_publish"):
            publishes.append(url)
            if phase == "ambiguous":
                raise requests.Timeout()
            if phase == "rejection":
                return FakeResponse(400, {"error": {"message": "definitive rejection"}})
            return FakeResponse(200, {"id": "fixture-media"})
        creations.append(url)
        return FakeResponse(200, {"id": f"container-{len(creations)}"})

    def get(*_, **__):
        failed = phase == "child" or phase == "parent" and len(creations) == 7
        return FakeResponse(200, {"status_code": "ERROR" if failed else "FINISHED"})

    monkeypatch.setattr(instagram_poster.requests, "post", post)
    monkeypatch.setattr(instagram_poster.requests, "get", get)
    monkeypatch.setattr(instagram_poster.time, "sleep", lambda _: None)

    def execute(args, authorization):
        if phase == "selection":
            raise RuntimeError("no candidates")
        publication_id = reserve()
        artwork_ids = ids(store)
        if phase == "staging":
            history_tracker.mark_publication_not_published(
                artwork_ids,
                "staging",
                authoritative=True,
                expected_publication_id=publication_id,
            )
            raise RuntimeError("staging failed")
        if phase == "cancelled":
            raise KeyboardInterrupt()

        def boundary(parent, children):
            history_tracker.start_publication_attempt(
                artwork_ids,
                parent,
                children,
                expected_publication_id=publication_id,
                authorization=authorization,
            )

        try:
            media_id = instagram_poster.post_carousel_to_instagram_graph_api(
                [f"https://fixture.invalid/{n}.jpg" for n in range(6)],
                "caption",
                "fixture-account",
                "fixture-token",
                before_publish=boundary,
            )
        except instagram_poster.InstagramPublishAmbiguousError:
            history_tracker.mark_artworks_ambiguous(
                artwork_ids, expected_publication_id=publication_id
            )
            raise
        except instagram_poster.InstagramAPIError:
            history_tracker.mark_publication_not_published(
                artwork_ids,
                "definitive",
                authoritative=True,
                expected_publication_id=publication_id,
            )
            raise
        history_tracker.record_publish_response(
            artwork_ids, media_id, expected_publication_id=publication_id
        )
        history_tracker.confirm_carousel_publication(
            artwork_ids[0], artwork_ids[1:], media_id, publication_id=publication_id
        )
        if phase == "outcome_write":
            client.uncertain = True

    monkeypatch.setattr(main, "run_carousel_post", execute)
    assert main.main(["--mode", "carousel"]) == (0 if phase == "success" else 1)
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused
    expected = {
        "selection": "DEFINITIVE_FAILURE",
        "staging": "DEFINITIVE_FAILURE",
        "child": "DEFINITIVE_FAILURE",
        "parent": "DEFINITIVE_FAILURE",
        "rejection": "DEFINITIVE_FAILURE",
        "ambiguous": "AMBIGUOUS",
        "cancelled": "CANCELLED",
        "success": "SUCCESS",
        "outcome_write": "PUBLISHING",
    }
    assert control.permits[-1].status == expected[phase]
    assert len(publishes) == (
        1 if phase in {"rejection", "ambiguous", "success", "outcome_write"} else 0
    )
    if phase == "outcome_write":
        assert control.permits[-1].outcome is None
    # Re-entering application orchestration with the same run cannot reserve or publish again.
    before = len(publishes)
    assert main.main(["--mode", "carousel"]) == 1
    assert len(publishes) == before


def test_no_permit_no_reconciliation_or_acquisition(monkeypatch):
    store, _ = runtime(monkeypatch)
    feed_schedule.FeedScheduleManager(store, owned_media=lambda **_: set()).pause(
        expected_generation=3, reason="stopped", now=NOW
    )
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **_: pytest.fail("admission failure reached reconciliation"),
    )
    monkeypatch.setattr(
        main,
        "run_carousel_post",
        lambda *a: pytest.fail("admission failure acquired candidates"),
    )
    assert main.main(["--mode", "carousel"]) == 1


def test_expired_queued_run_does_not_admit(monkeypatch):
    store, _ = runtime(monkeypatch)

    class LateClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(minutes=60)

    monkeypatch.setattr(feed_schedule, "datetime", LateClock)
    monkeypatch.setattr(
        main, "run_carousel_post", lambda *a: pytest.fail("expired run produced work")
    )
    assert main.main(["--mode", "carousel"]) == 1
    assert store.load_safety()[0].feed_schedule_control.permits[-1].owner_run_id is None
