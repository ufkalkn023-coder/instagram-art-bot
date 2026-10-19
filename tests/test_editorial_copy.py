import pytest

from tests.test_editorial_brief import brief_data
from tests.test_story_suggestions import make_sources


@pytest.mark.parametrize('text', ['A closer look', 'What connects these works?',
                                'A journey through timeless beauty', 'Save this post for later'])
def test_editorial_copy_rejects_stock_hooks_and_engagement_bait(tmp_path, text):
    from src.editorial_brief import validate_editorial_brief
    from src.editorial_copy import validate_editorial_copy
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    data['title'] = text
    with pytest.raises(ValueError):
        validate_editorial_copy(validate_editorial_brief(data, sources))


def test_empty_lead_stays_empty_and_caption_contains_only_chosen_copy_and_credits(tmp_path):
    from src.editorial_brief import validate_editorial_brief
    from src.editorial_copy import compose_editorial_caption
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    data['lead'] = ''
    brief = validate_editorial_brief(data, sources)
    caption = compose_editorial_caption(brief, sources)
    assert caption.startswith('A rhythm of small marks\n\n')
    assert 'Known Artist' in caption and '1873' in caption and 'Cleveland' in caption
    assert 'Look' not in caption and '#' not in caption
    assert brief.angle not in caption


def test_repeated_editorial_title_and_unsupported_artist_intentions_are_blocked(tmp_path):
    from src.editorial_brief import validate_editorial_brief
    from src.editorial_copy import validate_editorial_copy
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    with pytest.raises(ValueError):
        validate_editorial_copy(validate_editorial_brief(data, sources), headline_history=[data['title']])
    data['lead'] = 'The artist intended to express a private grief through these marks.'
    with pytest.raises(ValueError):
        validate_editorial_copy(validate_editorial_brief(data, sources))
