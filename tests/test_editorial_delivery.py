from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_editorial_project import make_editorial_project


def approved_editorial(tmp_path, *, detail=False):
    from src.story_project import approve_story
    from src.story_feed import prepare_story_content
    project = make_editorial_project(tmp_path, detail=detail)
    approve_story(project, expected_revision=1)
    return project, prepare_story_content(project)


@pytest.mark.parametrize('detail,expected', [(False, 'single'), (True, 'carousel')])
def test_v2_delivery_has_exact_meaningful_pages_and_preserves_v1_contract(tmp_path, detail, expected):
    from src.story_delivery import StoryDelivery
    from src.story_feed import export_story_content
    project, content = approved_editorial(tmp_path, detail=detail)
    assert content.publication_format == expected
    assert [page.role for page in content.story_delivery.pages] == (['detail', 'artwork'] if detail else ['artwork'])
    data = content.story_delivery.model_dump(mode='json')
    data['schema_version'] = 'artfolio-story-delivery-v1'
    with pytest.raises(ValueError):
        StoryDelivery.model_validate(data)
    wrong = 'single' if detail else 'carousel'
    with pytest.raises(ValueError):
        replace(content, publication_format=wrong)
    exported = export_story_content(project, tmp_path / 'export')
    assert exported['publication_format'] == expected
    assert exported['caption'] == content.caption
    assert exported['story_delivery'] == content.story_delivery.model_dump(mode='json')


@pytest.mark.parametrize('target', ['source', 'page', 'caption'])
def test_v2_review_rejects_tampering(tmp_path, target):
    import json
    from src.story_project import load_project
    from src.story_feed import prepare_story_content
    project, content = approved_editorial(tmp_path)
    if target == 'caption':
        path = project / 'report.json'
        data = json.loads(path.read_text())
        data['caption'] = 'Changed after review'
        path.write_text(json.dumps(data))
    else:
        path = Path(load_project(project).sources[0].image_path if target == 'source' else content.media_paths[0])
        with path.open('ab') as stream:
            stream.write(b'changed after review')
    with pytest.raises(ValueError, match='review|render'):
        prepare_story_content(project)


def test_render_dependency_change_requires_a_new_editorial_preview(tmp_path, monkeypatch):
    from src import editorial_design
    from src.story_feed import prepare_story_content
    project, _ = approved_editorial(tmp_path)
    changed = tmp_path / 'changed-editorial-design.py'
    changed.write_text('INK = (0, 0, 0)')
    monkeypatch.setattr(editorial_design, '__file__', str(changed))
    with pytest.raises(ValueError, match='review|render'):
        prepare_story_content(project)


@pytest.mark.parametrize('phase,expected', [('success', 'CONSUMED'), ('ambiguous', 'QUARANTINED')])
def test_reviewed_single_has_one_boundary_and_exact_story_receipt(tmp_path, monkeypatch, phase, expected):
    from datetime import datetime
    from types import SimpleNamespace
    import main
    from src import history_tracker, instagram_poster
    from src.feed_queue import PreparedFeedQueue
    from src.feed_schedule import FeedScheduleManager
    from tests.test_feed_queue import NOW, content as legacy_content
    from tests.test_feed_schedule import auth
    from tests.test_single_feed_runtime import single_runtime

    _, reviewed = approved_editorial(tmp_path)
    queue = PreparedFeedQueue(tmp_path / 'queue')
    count = 0

    def prepare(format_name, directory, excluded):
        nonlocal count
        count += 1
        return reviewed if count == 1 else legacy_content(directory, format_name, count * 100)

    queue.build(target=3, first_format='single', prepare=prepare, now=NOW)
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(main, 'datetime', Clock)
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    args = SimpleNamespace(dry_run=False, prepared_queue=queue.directory, prepared_queue_r2=False)
    if phase == 'ambiguous':
        with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
            main.run_feed_with_queue(args, main.ProductionMode.SINGLE, authorization)
    else:
        main.run_feed_with_queue(args, main.ProductionMode.SINGLE, authorization)
    assert queue.status()[0]['state'] == expected
    assert len([url for url, _ in sent if url.endswith('/media_publish')]) == 1
    state, _ = store.load_safety()
    record = state.active_publication_state.posted_artworks[0]
    assert record['id'] == 'cleveland_0' and record['child_container_ids'] == []
    assert record['publication_type'] == 'single'
    assert len(state.active_publication_state.consumed_authorizations) == 1
    if phase == 'success':
        event = state.operational_projection.publications[0]
        assert event['type'] == 'single' and event['story_delivery'] == reviewed.story_delivery.model_dump(mode='json')
        receipt = store.load_receipts()[0].records[-1]
        assert receipt.publication_type == 'single' and receipt.story_delivery == reviewed.story_delivery
        assert len(receipt.artwork_positions) == 1
    else:
        units = history_tracker.list_unresolved_publication_units(limit=1)
        assert len(units) == 1 and units[0].status.value == 'AMBIGUOUS'
        assert units[0].child_container_ids == ()


def test_queue_cli_accepts_reviewed_single_in_a_single_slot(tmp_path, monkeypatch):
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'legacy')
    from scripts import prepare_feed_queue
    from src.feed_queue import PreparedFeedQueue
    from tests.test_feed_queue import content
    project, reviewed = approved_editorial(tmp_path)
    calls = []

    def prepare(format_name, directory, excluded, **kwargs):
        calls.append(format_name)
        assert 'cleveland_0' in excluded
        return content(directory, format_name, len(calls) * 100)

    monkeypatch.setattr(prepare_feed_queue, '_prepare', prepare)
    result = prepare_feed_queue.run(['--directory', str(tmp_path / 'cli-queue'), '--skip-keychain',
                                     '--first-format', 'single', '--story-project', str(project)])
    assert result == 0
    queue = PreparedFeedQueue(tmp_path / 'cli-queue')
    first = queue._content(queue._load()['packages'][0])
    assert first.story_delivery == reviewed.story_delivery and first.publication_format == 'single'
    assert calls == ['carousel', 'single']


def test_single_story_reconciliation_and_receipt_replay_preserve_review(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    import main
    from src import history_tracker, publication_state, publication_reconciliation
    from src.feed_schedule import FeedScheduleManager
    from tests.test_feed_schedule import NOW, auth
    from tests.test_single_feed_runtime import single_runtime

    _, reviewed = approved_editorial(tmp_path)
    reconcile = publication_reconciliation.reconcile_publications
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    confirm = history_tracker.confirm_artworks_and_record_publication

    def fail_confirm(*args, **kwargs):
        raise RuntimeError('simulated finalization interruption')

    monkeypatch.setattr(history_tracker, 'confirm_artworks_and_record_publication', fail_confirm)
    with pytest.raises(RuntimeError, match='interruption'):
        main.run_single_post(SimpleNamespace(dry_run=False), authorization, prepared_content=reviewed)
    monkeypatch.setattr(history_tracker, 'confirm_artworks_and_record_publication', confirm)
    state, _ = store.load_safety()
    record = state.active_publication_state.posted_artworks[0]
    assert record['publish_response_media_id'] == 'fixture-single-media'
    append = store.append_receipt

    def fail_append(receipt):
        raise publication_state.StateWriteUncertainError('simulated receipt outage')

    monkeypatch.setattr(store, 'append_receipt', fail_append)
    summary = reconcile(access_token='fixture', now=NOW + timedelta(hours=1))
    assert summary.errors == 1
    state, _ = store.load_safety()
    assert state.active_publication_state.posted_artworks[0]['status'] == 'PUBLISHED'
    event = state.operational_projection.publications[0]
    assert event['type'] == 'single' and event['content_type'] == 'SINGLE_ARTWORK'
    assert event['theme'] == reviewed.theme_id
    assert event['story_delivery'] == reviewed.story_delivery.model_dump(mode='json')
    assert state.active_publication_state.receipt_sync_pending == [record['publication_id']]
    monkeypatch.setattr(store, 'append_receipt', append)
    assert publication_state.replay_pending_receipts(store) == 1
    assert publication_state.replay_pending_receipts(store) == 0
    receipt = store.load_receipts()[0].records[-1]
    assert receipt.publication_type == 'single' and len(receipt.artwork_positions) == 1
    assert receipt.story_delivery == reviewed.story_delivery
    assert len([url for url, _ in sent if url.endswith('/media_publish')]) == 1


def test_ambiguous_single_story_operator_recovery_keeps_exact_delivery(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    import main
    from src import instagram_poster, publication_reconciliation
    from src.feed_schedule import FeedScheduleManager
    from tests.test_feed_schedule import NOW, auth
    from tests.test_single_feed_runtime import single_runtime
    _, reviewed = approved_editorial(tmp_path)
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase='ambiguous')
    authorization = auth()
    FeedScheduleManager().admit(authorization)
    with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
        main.run_single_post(SimpleNamespace(dry_run=False), authorization, prepared_content=reviewed)
    state, _ = store.load_safety()
    identity = state.active_publication_state.posted_artworks[0]['publication_id']
    monkeypatch.setattr(instagram_poster, 'get_container_status', lambda *_: 'PUBLISHED')
    monkeypatch.setattr(instagram_poster, 'get_instagram_media_id', lambda media_id, *_: media_id)
    monkeypatch.setattr(publication_reconciliation, '_best_effort_permalink', lambda *_: None)
    publication_reconciliation.recover_publication_media_id(publication_id=identity, media_id='verified-media',
                                                           access_token='fixture', now=NOW + timedelta(hours=1))
    state, _ = store.load_safety()
    assert state.operational_projection.publications[0]['story_delivery'] == reviewed.story_delivery.model_dump(mode='json')
    assert store.load_receipts()[0].records[-1].story_delivery == reviewed.story_delivery
    assert len([url for url, _ in sent if url.endswith('/media_publish')]) == 1


@pytest.mark.parametrize('mutation', [dict(publication_role='COVER'), dict(publication_type='carousel'),
                                     dict(child_container_ids=['unexpected-child']),
                                     dict(container_id='parent', status='PUBLISHING')])
def test_single_story_corruption_cannot_fall_back_to_legacy(tmp_path, monkeypatch, mutation):
    from src import history_tracker
    from tests.test_carousel_history import _history_backend
    _, reviewed = approved_editorial(tmp_path)
    history, _ = _history_backend(monkeypatch)
    identity = history_tracker.reserve_single_publication(reviewed.artworks[0], publication_metadata=reviewed.publication_metadata)
    assert identity
    record = history['posted_artworks'][0]
    record.update(mutation)
    with pytest.raises(history_tracker.CorruptedHistoryError):
        history_tracker._story_delivery_from_records([record])


def test_reviewed_single_cannot_be_downgraded_to_plain_content(tmp_path):
    from src.feed_content import PreparedFeedContent
    _, reviewed = approved_editorial(tmp_path)
    with pytest.raises(ValueError, match='reviewed|story|Story'):
        PreparedFeedContent('single', reviewed.artworks, reviewed.media_paths, 'Changed after review',
                            publication_metadata=reviewed.publication_metadata)


def test_single_runner_revalidates_metadata_mutated_after_construction(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import main
    from src.feed_content import PreparedFeedContent
    _, reviewed = approved_editorial(tmp_path)
    plain = PreparedFeedContent('single', reviewed.artworks, reviewed.media_paths, 'Changed caption')
    plain.publication_metadata.update(reviewed.publication_metadata)
    reservations = []

    def reserve(*args, **kwargs):
        reservations.append(True)
        raise RuntimeError('unexpected reservation boundary')

    monkeypatch.setattr(main.history_tracker, 'reserve_single_publication', reserve)
    with pytest.raises(ValueError, match='reviewed|story|Story'):
        main.run_single_post(SimpleNamespace(dry_run=False), prepared_content=plain)
    assert reservations == []


@pytest.mark.parametrize('marker', [None, 'legacy'])
def test_queue_rejects_review_payload_with_missing_story_marker(tmp_path, marker):
    from src.feed_queue import PreparedFeedQueue
    from tests.test_feed_queue import NOW, content as legacy_content
    _, reviewed = approved_editorial(tmp_path)
    queue = PreparedFeedQueue(tmp_path / 'queue')
    count = 0

    def prepare(format_name, directory, excluded):
        nonlocal count
        count += 1
        return reviewed if count == 1 else legacy_content(directory, format_name, count * 100)

    queue.build(target=3, first_format='single', prepare=prepare, now=NOW)
    package = queue._load()['packages'][0]
    if marker is None:
        package['content'].pop('content_kind')
    else:
        package['content']['content_kind'] = marker
    with pytest.raises(ValueError, match='reviewed|story|Story'):
        queue._content(package)
    # Even metadata removal cannot leave an ignored top-level delivery payload.
    package['content']['publication_metadata'] = {}
    with pytest.raises(ValueError, match='reviewed|story|Story'):
        queue._content(package)
