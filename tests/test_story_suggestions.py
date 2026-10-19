import json

import pytest
from PIL import Image, ImageDraw

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork


def make_sources(directory, count=3):
    sources = []
    for index in range(count):
        path = directory / f'art-{index}.jpg'
        image = Image.new('RGB', (1600, 1800), 'ivory')
        draw = ImageDraw.Draw(image)
        for n in range(15):
            draw.rectangle((n * 80, n * 90, n * 80 + 400, n * 90 + 400), fill=(n * 15, 80, 160))
        image.save(path)
        art = NormalizedArtwork(source='cleveland', source_id=str(index), title=f'Painting {index}',
                                artist_name='Known Artist', museum_name='Cleveland', creation_date='1873',
                                is_public_domain=True, license='CC0', rights_status='CONFIRMED_OPEN_ACCESS')
        sources.append(SourceArtwork(artwork=art, image_path=str(path)))
    return sources


def test_suggestions_cover_existing_narratives_without_approval_or_invented_claims(tmp_path):
    from src.story_suggestions import suggest_stories
    sources = make_sources(tmp_path)
    proposals = suggest_stories(sources, maximum=3)
    assert [p['plan']['narrative'] for p in proposals] == ['single_study', 'comparison', 'thematic_selection']
    assert all(p['manual_review_required'] and p['publication_ready'] is False for p in proposals)
    assert all(p['quality']['can_render'] for p in proposals)
    assert proposals[0]['plan']['public_title'] == 'Painting 0'
    assert proposals[1]['basis'] == 'Shared recorded artist'
    assert all(set(p['plan']['artwork_ids']) <= {s.artwork.canonical_id for s in sources} for p in proposals)
    assert suggest_stories(sources, maximum=3) == proposals


def test_suggestions_revalidate_rights_and_unique_sources(tmp_path):
    from src.story_suggestions import suggest_stories
    source = make_sources(tmp_path, 1)[0]
    with pytest.raises(ValueError):
        suggest_stories([source, source])
    unsafe = source.model_copy(update={'artwork': source.artwork.model_copy(update={
        'is_public_domain': False, 'rights_status': 'UNKNOWN', 'license': None})})
    with pytest.raises(ValueError):
        suggest_stories([unsafe])


def test_story_suggestion_cli_renders_real_unapproved_projects_and_preserves_existing_output(tmp_path):
    from scripts.suggest_stories import run
    from src.story_project import load_project

    sources = make_sources(tmp_path, 1)
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'artworks': [
        {'artwork': source.artwork.model_dump(mode='json'), 'image_path': 'art-0.jpg'} for source in sources]}))
    output = tmp_path / 'review'
    args = ['--manifest', str(manifest), '--output', str(output), '--maximum', '1']
    assert run(args) == 0
    project = load_project(output / 'story-01')
    assert project.plan.public_title == 'Painting 0'
    assert not (output / 'story-01' / '.story-review.json').exists()
    assert (output / 'story-01' / 'index.html').is_file()
    assert 'alternatif' in (output / 'index.html').read_text()
    original = (output / 'suggestions.json').read_bytes()
    assert run(args) == 1
    assert (output / 'suggestions.json').read_bytes() == original


def test_suggestions_respect_headline_history_and_limits(tmp_path):
    from src.story_suggestions import suggest_stories
    sources = make_sources(tmp_path, 1)
    assert suggest_stories(sources, headline_history=['Painting 0']) == []
    for limit in (0, 7, True):
        with pytest.raises(ValueError):
            suggest_stories(sources, maximum=limit)
