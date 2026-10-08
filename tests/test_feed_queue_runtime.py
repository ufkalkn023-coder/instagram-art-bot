from types import SimpleNamespace
from datetime import datetime

import pytest

import main
from tests.test_feed_queue import NOW, build
from tests.test_single_feed_runtime import single_runtime
from tests.test_feed_schedule import auth
from src.feed_schedule import FeedScheduleManager


@pytest.mark.parametrize("phase,expected", [("success", "CONSUMED"), ("ambiguous", "QUARANTINED")])
@pytest.mark.parametrize("backend", ["local", "r2"])
def test_queue_single_uses_real_publication_boundary_and_fences_uncertain_package(monkeypatch, tmp_path, phase, expected, backend):
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)
    queue, _ = build(tmp_path)
    if backend == "r2":
        from tests.test_r2_feed_queue import remote
        from src import r2_feed_queue, feed_queue_rights
        remote_queue = remote(tmp_path)
        remote_queue.install(queue, now=NOW)
        queue = remote_queue
        monkeypatch.setattr(r2_feed_queue, "R2PreparedFeedQueue", lambda *_: queue)
        monkeypatch.setattr(feed_queue_rights, "revalidate_source_rights", lambda content: True)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, "datetime", Clock)
    assert hasattr(main, "run_feed_with_queue"), "Prepared queue consumption is missing"
    args = SimpleNamespace(dry_run=False, prepared_queue=queue.directory if backend == "local" else None,
                           prepared_queue_r2=backend == "r2")
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


def test_expiry_during_revalidation_stops_before_reservation_or_media_staging(monkeypatch, tmp_path):
    from datetime import timedelta
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    queue, _ = build(tmp_path)
    class Clock(datetime):
        calls = 0
        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return NOW if cls.calls == 1 else NOW + timedelta(days=15)
    monkeypatch.setattr(main, 'datetime', Clock)
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    before = store.load_safety()[0].model_dump(mode='json')
    with pytest.raises(RuntimeError, match='expired during revalidation'):
        main.run_feed_with_queue(SimpleNamespace(dry_run=False, prepared_queue=queue.directory),
                                 main.ProductionMode.SINGLE, authorization)
    assert sent == []
    assert store.load_safety()[0].model_dump(mode='json') == before
    assert queue.status()[1]['state'] == 'QUARANTINED'
