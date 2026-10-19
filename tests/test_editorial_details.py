import pytest

from tests.test_editorial_brief import brief_data
from tests.test_story_suggestions import make_sources


def test_detail_requires_specific_heading_and_explicit_observation(tmp_path):
    from src.editorial_details import validate_editorial_details
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    assert validate_editorial_details(data, sources) == ()
    data['notes'][0]['focus'] = [0.1, 0.1, 0.7, 0.7]
    with pytest.raises(ValueError):
        validate_editorial_details(data, sources)
    data['notes'][0]['heading'] = 'Rectangular rhythm'
    details = validate_editorial_details(data, sources)
    assert len(details) == 1 and details[0].id == 'marks'


def test_geometry_is_not_a_substitute_for_detail_meaning_and_duplicates_are_rejected(tmp_path):
    from src.editorial_details import validate_editorial_details
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    data['notes'][0].update(focus=[0.1, 0.1, 0.7, 0.7], heading='A closer look')
    with pytest.raises(ValueError):
        validate_editorial_details(data, sources)
    data['notes'][0]['heading'] = 'Rectangular rhythm'
    data['notes'].append({**data['notes'][0], 'id': 'duplicate'})
    with pytest.raises(ValueError):
        validate_editorial_details(data, sources)
