import json

import pytest

from tests.test_editorial_brief import brief_data
from tests.test_story_suggestions import make_sources


def series_data():
    entries = []
    for index in range(3):
        brief = brief_data()
        brief.update(source_ids=[f'cleveland_{index}'],
                     title=['A rhythm of small marks', 'Rectangles at the edge', 'Colour behind a painted shape'][index])
        brief['notes'][0]['artwork_id'] = f'cleveland_{index}'
        if index == 1:
            brief['notes'][0].update(heading='Rectangular rhythm', focus=[0.1, 0.1, 0.7, 0.7])
        entries.append(dict(theme_id='flowers_in_painting', theme_title='Flowers in Painting', brief=brief))
    brief = brief_data()
    brief.update(narrative='comparison', source_ids=['cleveland_3', 'cleveland_4'],
                 title='Two arrangements of painted marks', comparison_aspect='composition',
                 comparison_rationale='Rectangles gather around the centre in one work and along the edge in the other.',
                 notes=[dict(id='centre', artwork_id='cleveland_3', kind='visual_note',
                             text='Broad rectangles gather around the centre.'),
                        dict(id='edge', artwork_id='cleveland_4', kind='visual_note',
                             text='Narrow rectangles run along the right edge.')])
    entries.append(dict(theme_id='flowers_in_painting', theme_title='Flowers in Painting', brief=brief))
    return dict(title='Four ways of looking at five works', entries=entries)


def test_pilot_contains_five_sources_and_distinct_meaningful_sequences(tmp_path):
    from src.editorial_pilot import build_editorial_pilot
    result = build_editorial_pilot(tmp_path / 'pilot', make_sources(tmp_path, 5), series_data())
    assert result['source_count'] == 5
    assert [row['publication_format'] for row in result['entries']] == ['single', 'carousel', 'single', 'carousel']
    assert [row['style'] for row in result['entries']] == ['journal', 'detail_study', 'artwork_led', 'journal']
    assert all(row['publication_ready'] is False for row in result['entries'])
    assert (tmp_path / 'pilot' / 'index.html').is_file()
    assert len(json.loads((tmp_path / 'pilot' / 'pilot.json').read_text())['entries']) == 4


def test_pilot_rejects_source_reuse_and_keeps_existing_output(tmp_path):
    from src.editorial_pilot import build_editorial_pilot
    sources = make_sources(tmp_path, 5)
    data = series_data()
    data['entries'][2]['brief'] = data['entries'][0]['brief']
    with pytest.raises(ValueError, match='once'):
        build_editorial_pilot(tmp_path / 'bad', sources, data)
    assert not (tmp_path / 'bad').exists()
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'user.txt').write_text('preserve')
    with pytest.raises(FileExistsError):
        build_editorial_pilot(target, sources, series_data())
    assert (target / 'user.txt').read_text() == 'preserve'


@pytest.mark.parametrize('count', [3, 5])
def test_five_work_pilot_requires_four_deliberate_projects(tmp_path, count):
    from src.editorial_pilot import build_editorial_pilot
    data = series_data()
    if count == 3:
        data['entries'] = data['entries'][:3]
        brief = data['entries'][2]['brief']
        brief.update(narrative='thematic_selection', source_ids=[f'cleveland_{n}' for n in (2, 3, 4)],
                     notes=[dict(id=f'marks-{n}', artwork_id=f'cleveland_{n}', kind='visual_note',
                                 text='Small rectangular marks repeat across the painted surface.') for n in (2, 3, 4)])
    else:
        data['entries'] = data['entries'][:3]
        for n, title in ((3, 'A narrow line of blue'), (4, 'Overlapping forms in the corner')):
            brief = brief_data()
            brief.update(source_ids=[f'cleveland_{n}'], title=title)
            brief['notes'][0]['artwork_id'] = f'cleveland_{n}'
            data['entries'].append(dict(theme_id='flowers_in_painting', theme_title='Flowers in Painting', brief=brief))
    with pytest.raises(ValueError, match='entries'):
        build_editorial_pilot(tmp_path / 'invalid', make_sources(tmp_path, 5), data)
    assert not (tmp_path / 'invalid').exists()
