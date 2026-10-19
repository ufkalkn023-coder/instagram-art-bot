"""The normal Feed entrypoint must never silently revert to older presentation."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.test_feed_queue import NOW, build
from tests.test_feed_schedule import auth


def test_feed_content_policy_defaults_to_editorial_and_rejects_unknown_modes(monkeypatch):
    from src.feed_content_policy import resolve_content_mode
    monkeypatch.delenv('ARTFOLIO_FEED_CONTENT_MODE', raising=False)
    assert resolve_content_mode() == 'editorial'
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'legacy')
    assert resolve_content_mode() == 'legacy'
    assert resolve_content_mode('editorial') == 'editorial'
    with pytest.raises(ValueError):
        resolve_content_mode('unknown')


@pytest.mark.parametrize('backend', ['local', 'r2'])
def test_editorial_claim_filter_leaves_valid_legacy_stock_untouched(tmp_path, backend):
    from src.feed_content_policy import is_editorial_content
    queue, _ = build(tmp_path)
    if backend == 'r2':
        from tests.test_r2_feed_queue import remote
        remote_queue = remote(tmp_path)
        remote_queue.install(queue, now=NOW)
        queue = remote_queue
    before = queue.status()
    assert queue.claim('single', protected_ids=set(), owner='editorial', now=NOW,
                       content_filter=is_editorial_content) is None
    assert queue.status() == before


@pytest.mark.parametrize('with_legacy_stock', [False, True])
def test_default_feed_waits_without_acquiring_or_publishing_legacy(monkeypatch, tmp_path, with_legacy_stock):
    import main
    monkeypatch.delenv('ARTFOLIO_FEED_CONTENT_MODE', raising=False)
    from src.feed_queue import PreparedFeedQueue
    queue = build(tmp_path)[0] if with_legacy_stock else PreparedFeedQueue(tmp_path / 'queue')
    before = queue.status()
    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(main, 'run_single_post', lambda *_args, **_kwargs: pytest.fail('legacy publication'))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, 'datetime', Clock)
    with pytest.raises(RuntimeError, match='EDITORIAL_REVIEW_REQUIRED'):
        main.run_feed_with_queue(SimpleNamespace(dry_run=False, prepared_queue=queue.directory),
                                 main.ProductionMode.SINGLE, auth())
    assert queue.status() == before


def editorial_projects(tmp_path, *, approve=True):
    from src.editorial_story import EditorialStoryPlan
    from src.editorial_v2 import SourceArtwork
    from src.story_project import create_project, approve_story
    from tests.test_story_suggestions import make_sources
    from tests.test_editorial_brief import brief_data
    sources = make_sources(tmp_path, 3)
    projects = []
    for index, source in enumerate(sources):
        brief = brief_data()
        brief['source_ids'] = [source.artwork.canonical_id]
        brief['notes'][0]['artwork_id'] = source.artwork.canonical_id
        if index == 1:
            brief['notes'][0].update(heading='Rectangular rhythm', focus=[0.1, 0.1, 0.7, 0.7])
        plan = EditorialStoryPlan(theme_id='flowers_in_painting', theme_title='Flowers in Painting', brief=brief)
        project = tmp_path / f'project-{index}'
        create_project(project, [SourceArtwork.model_validate(source.model_dump())], plan)
        if approve:
            approve_story(project, expected_revision=1)
        projects.append(project)
    return projects


@pytest.mark.parametrize('approve', [False, True])
def test_workspace_editorial_readiness_is_read_only_and_requires_each_approval(tmp_path, capsys, approve):
    from scripts.prepare_feed_queue import run
    from src.editorial_workspace import create_editorial_workspace
    selected = editorial_projects(tmp_path, approve=approve)
    workspace = tmp_path / 'workspace'
    create_editorial_workspace(workspace, selected)
    queue = tmp_path / 'queue'
    assert run(['--directory', str(queue), '--editorial-workspace', str(workspace),
                '--first-format', 'single', '--check-editorial']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ready'] is approve
    assert report['missing'] == ({'single': 0, 'carousel': 0} if approve else {'single': 2, 'carousel': 1})
    assert not queue.exists()


def test_workspace_prepares_only_approved_editorial_packages_without_legacy_acquisition(tmp_path, monkeypatch, capsys):
    from scripts import prepare_feed_queue as command
    from src.editorial_workspace import create_editorial_workspace
    from src.feed_queue import PreparedFeedQueue
    selected = editorial_projects(tmp_path)
    workspace = tmp_path / 'workspace'
    create_editorial_workspace(workspace, selected)
    monkeypatch.setattr(command, '_prepare', lambda *_args, **_kwargs: pytest.fail('legacy acquisition'))
    monkeypatch.setattr(command, 'load_keychain_credentials', lambda *_: pytest.fail('unneeded credentials'))
    directory = tmp_path / 'queue'
    assert command.run(['--directory', str(directory), '--editorial-workspace', str(workspace),
                        '--first-format', 'single']) == 0
    capsys.readouterr()
    queue = PreparedFeedQueue(directory)
    assert [item['publication_format'] for item in queue.status()] == ['single', 'carousel', 'single']
    for package in queue._load()['packages']:
        assert queue._content(package).story_delivery.schema_version == 'artfolio-story-delivery-v2'


def test_default_preparer_has_no_legacy_fallback_before_credentials(tmp_path, monkeypatch, capsys):
    from scripts import prepare_feed_queue as command
    monkeypatch.setattr(command, '_prepare', lambda *_args, **_kwargs: pytest.fail('legacy acquisition'))
    monkeypatch.setattr(command, 'load_keychain_credentials', lambda *_: pytest.fail('unneeded credentials'))
    with pytest.raises(SystemExit):
        command.run(['--directory', str(tmp_path / 'queue')])
    assert 'EDITORIAL_REVIEW_REQUIRED' in capsys.readouterr().err
    assert not (tmp_path / 'queue').exists()


def test_default_scheduled_check_explains_legacy_only_stock_without_mutation(tmp_path, monkeypatch, capsys):
    from scripts import prepare_feed_queue as command
    from tests.test_feed_queue_refill import cli_args, wire_command
    from tests.test_r2_feed_queue import install
    queue, _ = install(tmp_path)
    wire_command(monkeypatch, queue)
    monkeypatch.delenv('ARTFOLIO_FEED_CONTENT_MODE', raising=False)
    before = dict(queue.client.objects)
    assert command.run(cli_args(tmp_path, '--check-refill')) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['reason'] == 'EDITORIAL_REVIEW_REQUIRED'
    assert report['editorial_ready_count'] == 0
    assert report['refill_needed'] is False
    assert report['legacy_stock_preserved'] is True
    assert queue.client.objects == before


def test_default_feed_publishes_reviewed_editorial_through_real_guarded_lifecycle(tmp_path, monkeypatch):
    import main
    from src.feed_queue import PreparedFeedQueue
    from src.story_feed import prepare_story_content
    from tests.test_single_feed_runtime import single_runtime
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv('ARTFOLIO_FEED_CONTENT_MODE', raising=False)
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    projects = editorial_projects(inputs)
    contents = [prepare_story_content(path) for path in projects]
    queue = PreparedFeedQueue(tmp_path / 'editorial-queue')
    iterator = iter(contents)
    queue.build(target=3, first_format='single', prepare=lambda *_: next(iterator), now=NOW)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, 'datetime', Clock)
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: pytest.fail('legacy acquisition'))
    assert main.main(['--mode', 'single', '--prepared-queue', str(queue.directory)]) == 0
    assert queue.status()[0]['state'] == 'CONSUMED'
    assert len([url for url, _ in sent if url.endswith('/media_publish')]) == 1
    state = store.load_safety()[0]
    assert len(state.active_publication_state.posted_artworks) == 1
    assert state.operational_projection.publications[0]['story_delivery'] == contents[0].story_delivery.model_dump(mode='json')
    assert store.load_receipts()[0].records[-1].story_delivery == contents[0].story_delivery


def test_editorial_policy_excludes_reviewed_v1_padding(tmp_path):
    from src.feed_content_policy import is_editorial_content
    from tests.test_story_feed import make_story_project, approved_content
    assert is_editorial_content(approved_content(make_story_project(tmp_path))) is False


def test_default_refill_does_not_acquire_when_reviewed_stock_is_absent(tmp_path, monkeypatch, capsys):
    from scripts import prepare_feed_queue as command
    from tests.test_feed_queue_refill import cli_args, wire_command
    from tests.test_r2_feed_queue import remote
    queue = remote(tmp_path)
    _, client = wire_command(monkeypatch, queue)
    monkeypatch.delenv('ARTFOLIO_FEED_CONTENT_MODE', raising=False)
    monkeypatch.setattr(command, '_prepare', lambda *_args, **_kwargs: pytest.fail('legacy acquisition'))
    before = client.puts
    assert command.run(cli_args(tmp_path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['refill_needed'] is False and result['requires_editorial_review'] is True
    assert result['reason'] == 'EDITORIAL_REVIEW_REQUIRED'
    assert client.puts == before and not queue.client.writes
    assert not (tmp_path / 'prepared').exists()
