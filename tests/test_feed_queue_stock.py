"""Early stock keeps active ownership and sources intact while adding future posts."""
import copy
import json
from datetime import timedelta

import pytest

from src.feed_queue import PreparedFeedQueue
from src.publication_state import canonical_bytes, seal
from tests.test_feed_queue import NOW, content
from tests.test_r2_feed_queue import install


def low_stock(tmp_path):
    queue, _ = install(tmp_path)
    claim = queue.claim('carousel', protected_ids=set(), owner='finished', now=NOW)
    queue.finish(claim.package_id, owner='finished', successful=True)
    return queue


def next_batch(tmp_path, *, first='single', offset=10):
    local = PreparedFeedQueue(tmp_path / 'future')
    index = offset
    def prepare(format_name, directory, excluded):
        nonlocal index
        index += 1
        return content(directory, format_name, index)
    local.build(target=3, first_format=first, prepare=prepare, now=NOW)
    return local


def test_two_ready_packages_trigger_early_stock_without_writes(tmp_path):
    queue = low_stock(tmp_path)
    before = copy.deepcopy(queue.client.objects)
    result = queue.stock_status(now=NOW)
    assert result['refill_needed'] is True
    assert result['reason'] == 'LOW_STOCK'
    assert result['ready_count'] == 2
    assert result['next_format'] == 'single'
    assert result['first_retained_format'] == 'single'
    assert result['queue_etag'] == queue.client.etag(queue.manifest_key)
    assert set(result['excluded_artwork_ids']) == {art['id'] for p in json.loads(before[queue.manifest_key])['packages'] for art in p['content']['artworks']}
    assert queue.client.objects == before


def test_three_ready_packages_do_not_trigger_acquisition(tmp_path):
    queue, _ = install(tmp_path)
    assert queue.stock_status(now=NOW)['refill_needed'] is False


@pytest.mark.parametrize('state,reason', [('CLAIMED',None), ('QUARANTINED','unverified_outcome')])
def test_uncertain_package_blocks_early_stock(tmp_path, state, reason):
    queue = low_stock(tmp_path)
    document, _ = queue._read_manifest()
    document['packages'][0].update(state=state, reason=reason, owner='owner')
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(document))
    before = copy.deepcopy(queue.client.objects)
    assert queue.stock_status(now=NOW)['refill_needed'] is False
    assert queue.client.objects == before


def test_replenishment_preserves_ready_entries_archives_and_appends(tmp_path):
    queue = low_stock(tmp_path)
    before, etag = queue._read_manifest()
    retained = copy.deepcopy(before['packages'][1:])
    queue.replenish(next_batch(tmp_path), expected_etag=etag, now=NOW)
    after, _ = queue._read_manifest()
    assert len(after['packages']) == 5
    assert after['packages'][:2] == retained
    assert [p['content']['publication_format'] for p in after['packages']] == ['single','carousel','single','carousel','single']
    archives = [json.loads(data) for key,data in queue.client.objects.items() if '/archives/' in key]
    assert before in archives



def test_early_replenishment_archives_original_expired_and_fresh_entries(tmp_path):
    queue = low_stock(tmp_path)
    document, _ = queue._read_manifest()
    document['packages'][1].update(
        created_at=(NOW - timedelta(days=2)).isoformat(),
        expires_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    original = canonical_bytes(seal(document))
    queue.client.objects[queue.manifest_key] = original
    before, etag = queue._read_manifest()
    queue.replenish(next_batch(tmp_path), expected_etag=etag, now=NOW)
    archives = [data for key, data in queue.client.objects.items() if '/archives/' in key]
    assert original in archives
    after, _ = queue._read_manifest()
    assert after['packages'][0] == before['packages'][2]
    assert len(after['packages']) == 4


def test_stale_etag_rejects_before_asset_upload_or_manifest_change(tmp_path):
    queue = low_stock(tmp_path)
    before = copy.deepcopy(queue.client.objects)
    with pytest.raises(RuntimeError, match='changed|conflict'):
        queue.replenish(next_batch(tmp_path), expected_etag='"stale"', now=NOW)
    assert queue.client.objects == before


def test_replenishment_cannot_repeat_retained_sources(tmp_path):
    queue = low_stock(tmp_path)
    _, etag = queue._read_manifest()
    before = copy.deepcopy(queue.client.objects)
    with pytest.raises(ValueError, match='repeat|overlap'):
        queue.replenish(next_batch(tmp_path, offset=0), expected_etag=etag, now=NOW)
    assert queue.client.objects == before


def test_replenishment_must_continue_alternation(tmp_path):
    queue = low_stock(tmp_path)
    _, etag = queue._read_manifest()
    before = copy.deepcopy(queue.client.objects)
    with pytest.raises(ValueError, match='alternat|format'):
        queue.replenish(next_batch(tmp_path, first='carousel'), expected_etag=etag, now=NOW)
    assert queue.client.objects == before


def test_claim_race_wins_without_losing_ownership(tmp_path):
    queue = low_stock(tmp_path)
    _, etag = queue._read_manifest()
    from tests.test_r2_feed_queue import remote
    winner = remote(tmp_path, queue.client, 'winner')
    queue.client.before_put = lambda: winner.claim('single', protected_ids=set(), owner='winner', now=NOW)
    with pytest.raises(RuntimeError, match='conflict'):
        queue.replenish(next_batch(tmp_path), expected_etag=etag, now=NOW)
    assert queue.status()[1]['owner'] == 'winner'
    assert queue.status()[1]['state'] == 'CLAIMED'


def test_early_cli_checks_tail_format_and_excludes_existing_sources(monkeypatch, tmp_path):
    from scripts import prepare_feed_queue as command
    from tests.test_feed_queue_refill import wire_command, cli_args
    from tests.test_feed_schedule import SHA
    queue = low_stock(tmp_path)
    wire_command(monkeypatch, queue)
    monkeypatch.setattr('src.feed_queue._now', lambda value: NOW)
    monkeypatch.setattr('src.r2_feed_queue._now', lambda value: NOW)
    original = queue.stock_status
    monkeypatch.setattr(queue, 'stock_status', lambda **_: original(now=NOW))
    # Fixture schedule's next format is carousel; retain carousel then single.
    doc, _ = queue._read_manifest()
    doc['packages'] = [doc['packages'][0], doc['packages'][2], doc['packages'][1]]
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(doc))
    seen = []
    index = 20
    def prepare(format_name, directory, excluded, **kwargs):
        nonlocal index
        seen.append(set(excluded))
        index += 1
        return content(directory, format_name, index)
    monkeypatch.setattr(command, '_prepare', prepare)
    assert command.run(cli_args(tmp_path) + ['--early']) == 0
    assert len(queue.status()) == 5
    assert seen
    assert all(art['id'] in seen[0] for p in doc['packages'] for art in p['content']['artworks'])
    assert command._refill_decision(queue, SHA, early=True)['refill_needed'] is False


def test_early_flag_is_not_allowed_on_unconditional_install(tmp_path):
    from scripts import prepare_feed_queue as command
    with pytest.raises(SystemExit) as error:
        command.run(['--directory', str(tmp_path/'new'), '--early'])
    assert error.value.code == 2


def test_uncertain_stock_write_is_never_retried_or_rearmed(tmp_path):
    queue = low_stock(tmp_path)
    _, etag = queue._read_manifest()
    before = queue.client.objects[queue.manifest_key]
    writes = len(queue.client.writes)
    queue.client.uncertain = True
    with pytest.raises(RuntimeError, match='uncertain'):
        queue.replenish(next_batch(tmp_path), expected_etag=etag, now=NOW)
    assert len(queue.client.writes) == writes + 1
    assert queue.client.objects[queue.manifest_key] == before


def test_corrupt_ready_timestamp_never_grants_stock_permission(tmp_path):
    queue = low_stock(tmp_path)
    doc, _ = queue._read_manifest()
    doc['packages'][1]['expires_at'] = 'not-a-date'
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(doc))
    before = copy.deepcopy(queue.client.objects)
    with pytest.raises(RuntimeError):
        queue.stock_status(now=NOW)
    assert queue.client.objects == before
