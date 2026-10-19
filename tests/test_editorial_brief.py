import pytest

from tests.test_story_suggestions import make_sources


def brief_data():
    return {'source_ids': ['cleveland_0'], 'narrative': 'single_study',
            'title': 'A rhythm of small marks', 'angle': 'How repeated marks change across the painted surface.',
            'lead': 'Small marks gather around the larger painted forms.',
            'notes': [{'id': 'marks', 'artwork_id': 'cleveland_0', 'kind': 'visual_note',
                       'text': 'Small rectangular marks repeat across the surface.'}]}


def test_brief_preserves_explicit_angle_and_human_review_boundary(tmp_path):
    from src.editorial_brief import validate_editorial_brief
    brief = validate_editorial_brief(brief_data(), make_sources(tmp_path, 1))
    assert brief.angle == brief_data()['angle']
    assert brief.manual_review_required is True
    assert brief.publication_ready is False


@pytest.mark.parametrize('field', ['title', 'angle'])
def test_brief_rejects_empty_editorial_choices(tmp_path, field):
    from src.editorial_brief import validate_editorial_brief
    data = brief_data()
    data[field] = ' ' * 20
    with pytest.raises(ValueError):
        validate_editorial_brief(data, make_sources(tmp_path, 1))


def test_brief_rejects_untraceable_museum_quotes_and_catalogue_claims(tmp_path):
    from src.editorial_brief import validate_editorial_brief
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    data['notes'][0].update(kind='museum_quote', text='The artist painted this after a long journey.')
    with pytest.raises(ValueError):
        validate_editorial_brief(data, sources)
    data['notes'][0].update(kind='catalogue_fact', field='medium', text='Oil on canvas')
    with pytest.raises(ValueError):
        validate_editorial_brief(data, sources)


def test_brief_accepts_only_actual_quote_and_source_membership(tmp_path):
    from src.editorial_brief import validate_editorial_brief
    source = make_sources(tmp_path, 1)[0]
    source = source.model_copy(update={'artwork': source.artwork.model_copy(update={
        'description': 'The broad marks remain visible around the edge.'})})
    data = brief_data()
    data['notes'][0].update(kind='museum_quote', text='The broad marks remain visible around the edge.')
    assert validate_editorial_brief(data, [source]).notes[0].kind == 'museum_quote'
    data['notes'][0]['artwork_id'] = 'unknown_1'
    with pytest.raises(ValueError):
        validate_editorial_brief(data, [source])
