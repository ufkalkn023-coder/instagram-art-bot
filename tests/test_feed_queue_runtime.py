from types import SimpleNamespace
from datetime import datetime

import pytest

import main
from tests.test_feed_queue import NOW, build
from tests.test_single_feed_runtime import single_runtime
from tests.test_feed_schedule import auth
from src.feed_schedule import FeedScheduleManager


@pytest.mark.parametrize("phase,expected", [("success", "CONSUMED"), ("ambiguous", "QUARANTINED")])
def test_queue_single_uses_real_publication_boundary_and_fences_uncertain_package(monkeypatch, tmp_path, phase, expected):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)
    queue, _ = build(tmp_path)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, "datetime", Clock)
    assert hasattr(main, "run_feed_with_queue"), "Prepared queue consumption is missing"
    args = SimpleNamespace(dry_run=False, prepared_queue=queue.directory)
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    if phase == "ambiguous":
        with pytest.raises(main.instagram_poster.InstagramPublishAmbiguousError):
            main.run_feed_with_queue(args, main.ProductionMode.SINGLE, authorization)
    else:
        main.run_feed_with_queue(args, main.ProductionMode.SINGLE, authorization)
    assert queue.status()[1]["state"] == expected
    assert len([url for url, _ in sent if url.endswith("/media_publish")]) == 1
    safety, _ = store.load_safety()
    assert len(safety.active_publication_state.posted_artworks) == 1
