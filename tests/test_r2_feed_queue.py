import copy
import hashlib
import importlib
import io
import json
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.publication_state import StateConfiguration, canonical_bytes, seal
from tests.test_feed_queue import NOW, build, content


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.before_put = None
        self.uncertain = False
        self.writes = []
        self.reads = []

    def etag(self, key):
        return '"' + hashlib.sha256(self.objects.get(key, b'')).hexdigest() + '"'

    def get_object(self, *, Bucket, Key):
        self.reads.append(Key)
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        data = self.objects[Key]
        return {'Body': io.BytesIO(data), 'ETag': self.etag(Key), 'ContentLength': len(data)}

    def put_object(self, *, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **kwargs):
        if self.before_put:
            callback, self.before_put = self.before_put, None
            callback()
        if ((IfNoneMatch == '*' and Key in self.objects)
                or (IfMatch is not None and (Key not in self.objects or self.etag(Key) != IfMatch))):
            raise ClientError({'Error': {'Code': 'PreconditionFailed'},
                               'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
        self.objects[Key] = bytes(Body)
        self.writes.append((Key, IfMatch, IfNoneMatch))
        if self.uncertain:
            raise EndpointConnectionError(endpoint_url='https://test.invalid')


def remote(tmp_path, client=None, name='runner'):
    module = importlib.import_module('src.r2_feed_queue')
    return module.R2PreparedFeedQueue(tmp_path / name, client=client or MemoryS3(),
                                     config=StateConfiguration('account', 'private-state', 'key', 'secret'))


def install(tmp_path):
    local, _ = build(tmp_path)
    queue = remote(tmp_path)
    queue.install(local, now=NOW)
    return queue, local


def test_remote_survives_fresh_runner_and_confirms_one_claim(tmp_path):
    queue, local = install(tmp_path)
    fresh = remote(tmp_path, queue.client, 'fresh')
    claim = fresh.claim('single', protected_ids=set(), owner='run:1', now=NOW)
    assert claim.content.caption == 'Museum art credits'
    assert all('/fresh/' in path for path in claim.content.media_paths)
    fresh.finish(claim.package_id, owner='run:1', successful=True)
    assert queue.status()[1]['state'] == 'CONSUMED'
    assert local.status()[1]['state'] == 'READY'


def test_losing_cas_consumer_cannot_return_content_or_overwrite_owner(tmp_path):
    queue, _ = install(tmp_path)
    competitor = remote(tmp_path, queue.client, 'competitor')
    queue.client.before_put = lambda: competitor.claim('single', protected_ids=set(), owner='winner', now=NOW)
    with pytest.raises(RuntimeError, match='conflict'):
        queue.claim('single', protected_ids=set(), owner='loser', now=NOW)
    assert queue.status()[1]['owner'] == 'winner'


def test_uncertain_claim_write_never_rearms_or_retries(tmp_path):
    queue, _ = install(tmp_path)
    queue.client.uncertain = True
    with pytest.raises(RuntimeError, match='uncertain'):
        queue.claim('single', protected_ids=set(), owner='uncertain', now=NOW)
    queue.client.uncertain = False
    assert queue.claim('single', protected_ids=set(), owner='later', now=NOW + timedelta(days=1)) is None
    assert queue.status()[1]['state'] == 'CLAIMED'


def test_install_cannot_replace_ready_or_claimed_packages(tmp_path):
    queue, local = install(tmp_path)
    before = copy.deepcopy(queue.client.objects)
    with pytest.raises(RuntimeError, match='unfinished'):
        queue.install(local, now=NOW)
    assert queue.client.objects == before


def test_expired_install_rejected_before_any_remote_write(tmp_path):
    local, _ = build(tmp_path)
    queue = remote(tmp_path)
    with pytest.raises(ValueError, match='expired'):
        queue.install(local, now=NOW + timedelta(days=15))
    assert not queue.client.writes


def test_terminal_batches_are_archived_before_replenishment(tmp_path):
    queue, _ = install(tmp_path)
    for format_name in ('carousel', 'single', 'carousel'):
        claim = queue.claim(format_name, protected_ids=set(), owner='run', now=NOW)
        queue.finish(claim.package_id, owner='run', successful=False)
    fresh_local, _ = build(tmp_path / 'new')
    queue.install(fresh_local, now=NOW)
    assert all(item['state'] == 'READY' for item in queue.status())
    assert any('/archives/' in key for key in queue.client.objects)


def test_corrupt_remote_manifest_fails_without_fallback_or_write(tmp_path):
    queue, _ = install(tmp_path)
    key = queue.manifest_key
    queue.client.objects[key] = b'{"schema_version":1,"packages":[]}'
    before = len(queue.client.writes)
    with pytest.raises(RuntimeError):
        queue.claim('single', protected_ids=set(), owner='run', now=NOW)
    assert len(queue.client.writes) == before


def test_tampered_asset_is_quarantined_and_cannot_publish(tmp_path):
    queue, _ = install(tmp_path)
    key = next(key for key in queue.client.objects if '/packages/' in key and '/media-0.jpg' in key)
    queue.client.objects[key] = b'bad'
    claim = queue.claim('carousel', protected_ids=set(), owner='run', now=NOW)
    assert claim is not None
    assert queue.status()[0]['state'] == 'QUARANTINED'


def test_fresh_rights_failure_quarantines_before_ownership(tmp_path):
    queue, _ = install(tmp_path)
    assert queue.claim('single', protected_ids=set(), owner='run', now=NOW,
                       rights_revalidator=lambda content: False) is None
    assert queue.status()[1]['reason'] == 'fresh_rights_unconfirmed'


def test_remote_status_is_read_only_and_counts_missing_queue_as_empty(tmp_path):
    queue = remote(tmp_path)
    assert queue.status() == []
    assert not queue.client.writes


def test_all_expired_ready_packages_can_be_replaced_with_archived_evidence(tmp_path):
    queue, _ = install(tmp_path)
    later = NOW + timedelta(days=15)
    from src.feed_queue import PreparedFeedQueue
    local = PreparedFeedQueue(tmp_path / 'later')
    index = 0
    def prepare(format_name, directory, excluded):
        nonlocal index
        index += 1
        return content(directory, format_name, index + 10)
    local.build(target=3, first_format='single', prepare=prepare, now=later)
    queue.install(local, now=later)
    assert all(row['state'] == 'READY' for row in queue.status())
    archive_key = next(key for key in queue.client.objects if '/archives/' in key)
    archived = json.loads(queue.client.objects[archive_key])
    assert all(item['state'] == 'QUARANTINED' and item['reason'] == 'expired' for item in archived['packages'])


def test_oversized_asset_list_fails_before_any_asset_network_read(tmp_path):
    queue, _ = install(tmp_path)
    document = json.loads(queue.client.objects[queue.manifest_key])
    package = document['packages'][1]
    package['content']['assets'] *= 100
    package['content_sha256'] = hashlib.sha256(canonical_bytes(package['content'])).hexdigest()
    queue.client.objects[queue.manifest_key] = canonical_bytes(seal(document))
    claim = queue.claim('single', protected_ids=set(), owner='run', now=NOW)
    assert claim is None
    assert queue.status()[1]['reason'] == 'invalid_content'
    assert not any('/packages/' in key for key in queue.client.reads)


@pytest.mark.parametrize('relative', ['../../safety.json', '/etc/passwd', 'other/media.jpg'])
def test_sealed_but_unsafe_asset_path_fails_before_upload(tmp_path, relative):
    local, _ = build(tmp_path)
    document = json.loads(local.manifest.read_text())
    document['packages'][0]['content']['assets'][0]['path'] = relative
    package = document['packages'][0]
    package['content_sha256'] = hashlib.sha256(canonical_bytes(package['content'])).hexdigest()
    local.manifest.write_bytes(canonical_bytes(seal(document)))
    queue = remote(tmp_path)
    with pytest.raises(ValueError):
        queue.install(local, now=NOW)
    assert not queue.client.writes
