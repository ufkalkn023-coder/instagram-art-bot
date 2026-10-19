import hashlib

import pytest

from tests.test_editorial_project import make_editorial_project


def projects(tmp_path, count=3):
    result = []
    for n in range(count):
        directory = tmp_path / f'input-{n}'
        directory.mkdir()
        result.append(make_editorial_project(directory, detail=n == 1))
    return result


def test_workspace_uses_real_pages_without_approving_or_modifying_projects(tmp_path):
    from src.editorial_workspace import create_editorial_workspace
    selected = projects(tmp_path)
    originals = [(path / 'project.json').read_bytes() for path in selected]
    output = tmp_path / 'workspace'
    report = create_editorial_workspace(output, selected)
    assert len(report['projects']) == 3 and report['publication_ready'] is False
    page = (output / 'index.html').read_text()
    assert 'feed-grid' in page and 'Öne al' in page and 'Yalnız önizleme' in page
    for entry, source in zip(report['projects'], selected):
        copied = output / entry['preview_path']
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == entry['preview_sha256']
        assert not (source / '.story-review.json').exists()
    assert originals == [(path / 'project.json').read_bytes() for path in selected]
    with pytest.raises(FileExistsError):
        create_editorial_workspace(output, selected)


def test_workspace_live_view_follows_saved_revision_without_mutating_manifest(tmp_path):
    from src.editorial_workspace import create_editorial_workspace, workspace_rows
    from src.story_project import load_project, update_project
    selected = projects(tmp_path, 1)
    output = tmp_path / 'workspace'
    report = create_editorial_workspace(output, selected)
    before = (output / 'workspace.json').read_bytes()
    changed = load_project(selected[0]).plan.model_dump(mode='json')
    changed['brief']['title'] = 'Blue forms meet along the painted edge'
    update_project(selected[0], changed, expected_revision=1)
    rows = workspace_rows(report)
    assert rows[0]['revision'] == 2 and rows[0]['title'] == changed['brief']['title']
    assert rows[0]['preview_path'] != report['projects'][0]['preview_path']
    assert (output / 'workspace.json').read_bytes() == before
