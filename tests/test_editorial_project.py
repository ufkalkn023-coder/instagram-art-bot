import json

import pytest

from tests.test_editorial_brief import brief_data
from tests.test_story_suggestions import make_sources


def make_editorial_project(tmp_path, *, detail=False):
    from src.editorial_story import EditorialStoryPlan
    from src.story_project import create_project
    sources = make_sources(tmp_path, 1)
    brief = brief_data()
    if detail:
        brief['notes'][0].update(heading='Rectangular rhythm', focus=[0.1, 0.1, 0.7, 0.7])
    plan = EditorialStoryPlan(theme_id='flowers_in_painting', theme_title='Flowers in Painting',
                              brief=brief, style='detail_study' if detail else 'journal')
    target = tmp_path / 'project'
    create_project(target, sources, plan)
    return target


def test_editorial_project_uses_existing_revision_editor_and_no_padding(tmp_path):
    from src.story_project import load_project, update_project, RevisionConflict
    target = make_editorial_project(tmp_path, detail=True)
    project = load_project(target)
    assert [slide.role for slide in project.plan.slides] == ['detail', 'artwork']
    page = (target / 'index.html').read_text()
    assert 'editorial-angle' in page and 'editorial-lead' in page
    assert 'note-text' in page and 'coordinate' in page
    assert 'Small rectangular marks' in page and 'caption-preview' in page
    changed = project.plan.model_dump(mode='json')
    changed['brief']['title'] = 'Blue rectangles at the painted edge'
    report = update_project(target, changed, expected_revision=1)
    assert report['revision'] == 2
    with pytest.raises(RevisionConflict):
        update_project(target, changed, expected_revision=1)
    assert not (target / '.story-review.json').exists()


def test_failed_editorial_save_preserves_entire_previous_revision(tmp_path):
    from src.story_project import load_project, update_project
    target = make_editorial_project(tmp_path, detail=True)
    before = {name: (target / name).read_bytes() for name in ('project.json', 'report.json', 'index.html')}
    changed = load_project(target).plan.model_dump(mode='json')
    changed['brief']['notes'][0]['focus'] = [0.1, 0.1, 0.11, 0.11]
    with pytest.raises(ValueError):
        update_project(target, changed, expected_revision=1)
    assert before == {name: (target / name).read_bytes() for name in before}


def test_editorial_review_binds_render_caption_and_every_edit(tmp_path):
    from src.story_project import approve_story, load_project, update_project
    from src.story_feed import prepare_story_content
    target = make_editorial_project(tmp_path)
    with pytest.raises(ValueError, match='review'):
        prepare_story_content(target)
    approve_story(target, expected_revision=1)
    content = prepare_story_content(target)
    assert content.publication_format == 'single'
    assert content.story_delivery.schema_version == 'artfolio-story-delivery-v2'
    report = json.loads((target / 'report.json').read_text())
    assert content.caption == report['caption']
    assert 'Small marks gather' in content.caption
    data = load_project(target).plan.model_dump(mode='json')
    data['brief']['lead'] = 'Blue rectangles overlap along the painted surface.'
    update_project(target, data, expected_revision=1)
    with pytest.raises(ValueError, match='review'):
        prepare_story_content(target)
