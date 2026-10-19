import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from tests.test_editorial_project import make_editorial_project


@contextmanager
def running_editor(project, **kwargs):
    from src.story_editor import editor_server
    server = editor_server(project, port=0, **kwargs)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def get_editor(url):
    with urllib.request.urlopen(url + '/', timeout=10) as response:
        page = response.read().decode()
    config = json.loads(page.split('window.ARTFOLIO_EDITOR=', 1)[1].split(';\n', 1)[0])
    return page, config


def post(url, config, data, *, origin=True, path='/api/approve'):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    headers = {'Content-Type': 'application/json', 'X-Artfolio-Editor': config['token']}
    if origin:
        headers['Origin'] = f'{parsed.scheme}://{parsed.netloc}'
    request = urllib.request.Request(url + path, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_revision_comparison_tracks_committed_predecessor_and_actual_crops(tmp_path):
    from src.editorial_revision import revision_comparison
    from src.story_project import load_project, update_project
    target = make_editorial_project(tmp_path, detail=True)
    assert revision_comparison(target)['available'] is False
    changed = load_project(target).plan.model_dump(mode='json')
    changed['brief']['title'] = 'Blue rectangles along the edge'
    changed['brief']['notes'][0]['focus'] = [0.2, 0.1, 0.8, 0.7]
    update_project(target, changed, expected_revision=1)
    comparison = revision_comparison(target)
    assert comparison['available'] is True and comparison['previous_revision'] == 1
    assert any(row['field'] == 'title' and row['after'] == changed['brief']['title'] for row in comparison['changes'])
    assert any(row['field'] == 'notes.marks.focus' for row in comparison['changes'])
    assert len(comparison['before_pages']) == len(comparison['after_pages']) == 2
    assert comparison['before_pages'][0]['path'] != comparison['after_pages'][0]['path']


def test_approval_rejects_unseen_digest_without_writing_review(tmp_path):
    from src.story_project import approve_story, RevisionConflict
    target = make_editorial_project(tmp_path)
    with pytest.raises(RevisionConflict):
        approve_story(target, expected_revision=1, expected_digest='f' * 64)
    assert not (target / '.story-review.json').exists()


def test_comparison_rejects_changed_predecessor_image(tmp_path):
    from src.editorial_revision import revision_comparison
    from src.story_project import load_project, update_project
    target = make_editorial_project(tmp_path)
    old = json.loads((target / 'report.json').read_text())['package']['slides'][0]['path']
    changed = load_project(target).plan.model_dump(mode='json')
    changed['brief']['title'] = 'Blue forms along the painted edge'
    update_project(target, changed, expected_revision=1)
    with (target / old).open('ab') as stream:
        stream.write(b'changed predecessor')
    with pytest.raises(ValueError, match='Previous revision'):
        revision_comparison(target)


def test_editor_approval_requires_origin_token_acknowledgment_and_current_digest(tmp_path):
    from src.story_project import load_project, update_project
    from src.story_feed import prepare_story_content
    target = make_editorial_project(tmp_path)
    with running_editor(target) as url:
        page, config = get_editor(url)
        assert 'approve-review' in page and 'review-confirm' in page and 'revision-comparison' in page
        data = dict(expected_revision=1, expected_digest=config['digest'], reviewed=True)
        assert post(url, config, data, origin=False)[0] == 403
        assert post(url, {**config, 'token': 'wrong'}, data)[0] == 403
        assert post(url, config, {**data, 'reviewed': False})[0] == 400
        assert post(url, config, {**data, 'expected_digest': 'f' * 64})[0] == 409
        assert not (target / '.story-review.json').exists()
        assert post(url, config, data)[0] == 200
        assert prepare_story_content(target).story_delivery.reviewed_revision == 1
        changed = load_project(target).plan.model_dump(mode='json')
        changed['brief']['lead'] = 'Several blue rectangles overlap at the edge.'
        update_project(target, changed, expected_revision=1)
        assert post(url, config, data)[0] == 409
        with pytest.raises(ValueError, match='review'):
            prepare_story_content(target)


def test_editor_approval_cannot_accept_page_tampering_after_preview(tmp_path):
    from pathlib import Path
    from src.story_feed import review_snapshot
    from src.story_project import load_project
    target = make_editorial_project(tmp_path)
    with running_editor(target) as url:
        _, config = get_editor(url)
        snapshot = review_snapshot(target, load_project(target))
        with Path(snapshot['paths'][0]).open('ab') as stream:
            stream.write(b'changed after preview')
        assert post(url, config, dict(expected_revision=1, expected_digest=config['digest'], reviewed=True))[0] == 400
    assert not (target / '.story-review.json').exists()


def test_workspace_routes_isolate_project_tokens_and_keep_manifest_private(tmp_path):
    from src.editorial_workspace import create_editorial_workspace
    from tests.test_editorial_workspace import projects
    selected = projects(tmp_path, 2)
    workspace = tmp_path / 'workspace'
    create_editorial_workspace(workspace, selected)
    with running_editor(selected[0], workspace=workspace) as url:
        _, first = get_editor(url + '/projects/p-01')
        _, second = get_editor(url + '/projects/p-02')
        assert first['token'] != second['token']
        data = dict(expected_revision=1, expected_digest=second['digest'], reviewed=True)
        assert post(url + '/projects/p-02', first, data)[0] == 403
        assert not (selected[1] / '.story-review.json').exists()
        assert post(url + '/projects/p-99', second, data)[0] == 404
        for path in ['/workspace.json', '/projects/p-99/', '/projects/p-01/project.json']:
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + path, timeout=10)
            assert error.value.code == 404
        assert post(url + '/projects/p-02', second, data)[0] == 200
        assert not (selected[0] / '.story-review.json').exists()
