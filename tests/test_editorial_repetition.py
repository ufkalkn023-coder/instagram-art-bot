from copy import deepcopy

import pytest

from tests.test_editorial_brief import brief_data


def test_angles_are_compared_even_with_different_titles_and_are_only_advisory():
    from src.editorial_repetition import repetition_warnings
    current = brief_data()
    current.update(title='The pale horizon', angle='Dark trees frame distant hills and a pale horizon.',
                   lead='The foreground is dark while distant hills recede into pale light.')
    previous = deepcopy(current)
    previous['title'] = 'Beyond the wooded foreground'
    before = deepcopy(current)
    report = repetition_warnings(current, [{'id': 'old-study', 'brief': previous}])
    assert report['warnings'] and report['advisory_only'] is True
    assert any(row['kind'] == 'similar_angle' for row in report['warnings'])
    assert any(row['kind'] == 'recurring_motif' and row['motif'] == 'Mesafe ve ölçek' for row in report['warnings'])
    assert current == before and report['compared_projects'] == 1


def test_repetition_ignores_shared_catalogue_text_and_bounds_history():
    from src.editorial_repetition import repetition_warnings
    first = brief_data()
    first.update(angle='Thin white lace forms loops around a small clasp.', lead='')
    first['notes'] = [dict(id='medium', artwork_id='cleveland_0', kind='catalogue_fact', field='medium', text='Oil on canvas')]
    second = deepcopy(first)
    second.update(title='A boat near the distant hill', angle='A distant boat crosses a broad stretch of still water.')
    assert repetition_warnings(first, [{'id': 'different', 'brief': second}])['warnings'] == []
    with pytest.raises(ValueError):
        repetition_warnings(first, [{'id': str(n), 'brief': second} for n in range(101)])
