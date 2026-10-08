import copy
import importlib
from datetime import timedelta

from tests.test_feed_analytics import NOW, publication, snapshot
from tests.test_r2_feed_queue import remote


def module():
    return importlib.import_module('src.feed_operations')


def test_combined_status_distinguishes_waiting_queue_expiry_and_missing_analytics():
    queue = [{'state': 'READY', 'publication_format': 'carousel', 'expires_at': (NOW + timedelta(days=1)).isoformat()},
             {'state': 'READY', 'publication_format': 'single', 'expires_at': (NOW - timedelta(days=1)).isoformat()},
             {'state': 'CLAIMED', 'publication_format': 'single', 'expires_at': NOW.isoformat()}]
    schedule = {'status': 'WAITING_COOLDOWN', 'next_format': 'carousel', 'last_success_at': '2026-10-07T18:16:07Z'}
    before = copy.deepcopy((queue, schedule))
    report = module().build_feed_operations(schedule, queue, {}, [], now=NOW)
    assert report['schedule']['status'] == 'WAITING_COOLDOWN'
    assert report['queue']['ready'] == 1 and report['queue']['expired'] == 1
    assert report['queue']['claimed'] == 1 and report['queue']['next_format_ready'] == 1
    assert report['analytics']['freshness'] == 'cold_start'
    assert (queue, schedule) == before


def test_unknown_age_and_media_mismatch_do_not_become_fresh_analytics():
    pub = publication()
    wrong = {**snapshot(pub), 'media_id': 'wrong'}
    report = module().build_feed_operations({'status': 'READY'}, [], {'publications': [pub]}, [wrong], now=NOW)
    assert report['analytics']['last_comparable_capture_at'] is None
    assert report['analytics']['freshness'] == 'missing'


def test_changed_block_failure_and_recovery_notify_once_waiting_stays_quiet(tmp_path):
    queue = remote(tmp_path)
    store = module().FeedStatusNotifications(queue)
    def report(status):
        return module().build_feed_operations({'status': status, 'next_format': 'carousel'}, [], {}, [], now=NOW)
    assert store.observe(report('WAITING_COOLDOWN'), now=NOW) is None
    assert store.observe(report('SHA_MISMATCH'), now=NOW)['event'] == 'blocked'
    assert store.observe(report('SHA_MISMATCH'), now=NOW + timedelta(hours=1)) is None
    assert store.observe(report('UNSAFE_STATE'), now=NOW)['event'] == 'blocked'
    assert store.observe(report('WAITING_COOLDOWN'), now=NOW)['event'] == 'recovered'
    assert store.observe(report('WAITING_COOLDOWN'), now=NOW + timedelta(hours=1)) is None
    assert store.observe(report('READY'), now=NOW) is None


def test_notification_state_never_writes_safety_receipts_or_queue_manifest(tmp_path):
    queue = remote(tmp_path)
    store = module().FeedStatusNotifications(queue)
    report = module().build_feed_operations({'status': 'SHA_MISMATCH'}, [], {}, [], now=NOW)
    store.observe(report, now=NOW)
    assert all(key.startswith('feed-status/') for key in queue.client.objects)


def test_unverified_notification_write_never_retries(tmp_path):
    import pytest
    queue = remote(tmp_path)
    store = module().FeedStatusNotifications(queue)
    queue.client.uncertain = True
    report = module().build_feed_operations({'status': 'SHA_MISMATCH'}, [], {}, [], now=NOW)
    with pytest.raises(RuntimeError, match='uncertain'):
        store.observe(report, now=NOW)
    assert len(queue.client.writes) == 1


def test_success_notification_tracks_authoritative_scheduler_completion(tmp_path):
    store = module().FeedStatusNotifications(remote(tmp_path))
    def report(date):
        return module().build_feed_operations({'status': 'WAITING_COOLDOWN', 'last_successful_feed_at': date},
                                               [], {}, [], now=NOW)
    assert store.observe(report('2026-10-05T19:00:00Z'), now=NOW) is None
    assert store.observe(report('2026-10-07T19:00:00Z'), now=NOW)['event'] == 'published'
    assert store.observe(report('2026-10-07T19:00:00Z'), now=NOW) is None
