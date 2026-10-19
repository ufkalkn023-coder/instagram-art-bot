import pytest

from tests.test_story_suggestions import make_sources


def comparison_data():
    return {'source_ids': ['cleveland_0', 'cleveland_1'], 'narrative': 'comparison',
            'title': 'Two ways of building a surface',
            'angle': 'Compare the recorded materials of two works through their surfaces.',
            'notes': [{'id': 'first', 'artwork_id': 'cleveland_0', 'kind': 'catalogue_fact',
                       'field': 'medium', 'text': 'Oil on canvas'},
                      {'id': 'second', 'artwork_id': 'cleveland_1', 'kind': 'catalogue_fact',
                       'field': 'medium', 'text': 'Watercolour on paper'}]}


def test_shared_artist_alone_cannot_create_editorial_comparison(tmp_path):
    from src.editorial_comparison import validate_editorial_comparison
    sources = make_sources(tmp_path, 2)
    data = comparison_data()
    data['notes'] = [{'id': 'artist-' + str(index), 'artwork_id': f'cleveland_{index}',
                      'kind': 'catalogue_fact', 'field': 'artist', 'text': 'Known Artist'} for index in range(2)]
    data['comparison_rationale'] = 'Both works share the same recorded artist.'
    with pytest.raises(ValueError):
        validate_editorial_comparison(data, sources)


def test_comparison_requires_distinct_evidence_for_declared_aspect(tmp_path):
    from src.editorial_comparison import validate_editorial_comparison
    sources = make_sources(tmp_path, 2)
    sources = [s.model_copy(update={'artwork': s.artwork.model_copy(update={'medium': medium})})
               for s, medium in zip(sources, ('Oil on canvas', 'Watercolour on paper'))]
    data = comparison_data()
    data.update(comparison_aspect='medium', comparison_rationale='The different recorded materials provide the basis for this comparison.')
    assert validate_editorial_comparison(data, sources).comparison_aspect == 'medium'
    sources[1] = sources[1].model_copy(update={'artwork': sources[1].artwork.model_copy(update={'medium': 'Oil on canvas'})})
    data['notes'][1]['text'] = 'Oil on canvas'
    with pytest.raises(ValueError):
        validate_editorial_comparison(data, sources)
