import hashlib
import json

import pytest
from PIL import Image

from tests.test_editorial_brief import brief_data
from tests.test_story_suggestions import make_sources


def test_editorial_draft_has_no_padding_or_generic_closing(tmp_path):
    from src.editorial_draft import build_editorial_draft
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    assert [page['role'] for page in build_editorial_draft(data, sources)['pages']] == ['artwork']
    data['notes'][0].update(heading='Rectangular rhythm', focus=[0.1, 0.1, 0.7, 0.7])
    draft = build_editorial_draft(data, sources)
    assert [page['role'] for page in draft['pages']] == ['artwork', 'detail']
    assert draft['publication_ready'] is False and draft['manual_review_required'] is True
    assert draft['pages'][1]['note_id'] == 'marks'


def test_editorial_gallery_preserves_sources_and_builds_three_readable_variants(tmp_path):
    from src.editorial_draft import create_editorial_gallery
    sources = make_sources(tmp_path, 1)
    before = hashlib.sha256((tmp_path / 'art-0.jpg').read_bytes()).hexdigest()
    data = brief_data()
    data['notes'][0].update(heading='Rectangular rhythm', focus=[0.1, 0.1, 0.7, 0.7])
    output = tmp_path / 'gallery'
    report = create_editorial_gallery(output, sources, data)
    assert set(report['variants']) == {'artwork_led', 'journal', 'detail_study'}
    assert (output / 'caption.txt').read_text().startswith(data['title'])
    assert 'publication_ready' in (output / 'draft.json').read_text()
    for variant in report['variants'].values():
        for page in variant['pages']:
            with Image.open(output / page['path']) as image:
                assert image.size == (1080, 1350) and image.format == 'JPEG'
    assert report['variants']['detail_study']['pages'][0]['role'] == 'detail'
    assert report['variants']['detail_study']['pages'][1]['role'] == 'artwork'
    assert before == hashlib.sha256((tmp_path / 'art-0.jpg').read_bytes()).hexdigest()
    assert not (output / '.story-review.json').exists()
    assert '<script>' not in (output / 'index.html').read_text()
    with pytest.raises(FileExistsError):
        create_editorial_gallery(output, sources, data)


def test_editorial_cli_does_not_leave_partial_output_on_bad_brief(tmp_path):
    from scripts.preview_editorial_story import run
    sources = make_sources(tmp_path, 1)
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'artworks': [{'artwork': sources[0].artwork.model_dump(mode='json'),
                                                'image_path': 'art-0.jpg'}]}))
    brief = tmp_path / 'brief.json'
    data = brief_data()
    data['title'] = 'A closer look'
    brief.write_text(json.dumps(data))
    output = tmp_path / 'review'
    assert run(['--manifest', str(manifest), '--brief', str(brief), '--output', str(output)]) == 1
    assert not output.exists()


def test_gallery_requires_a_real_detail_and_escapes_editorial_text(tmp_path):
    from src.editorial_draft import create_editorial_gallery
    data = brief_data()
    data['title'] = '<script>alert(1)</script>'
    output = tmp_path / 'escaped'
    report = create_editorial_gallery(output, make_sources(tmp_path, 1), data)
    assert set(report['variants']) == {'artwork_led', 'journal'}
    assert '<script>' not in (output / 'index.html').read_text()
    assert '&lt;script&gt;' in (output / 'index.html').read_text()


def test_gallery_rejects_unreadable_copy_without_partial_output(tmp_path):
    from src.editorial_draft import create_editorial_gallery
    sources = make_sources(tmp_path, 1)
    data = brief_data()
    data['lead'] = 'Long observation about painted shapes. ' * 12
    output = tmp_path / 'too-long'
    with pytest.raises(ValueError, match='readable layout capacity'):
        create_editorial_gallery(output, sources, data)
    assert not output.exists()


def test_comparison_gallery_shows_full_sources_and_declared_difference(tmp_path):
    from src.editorial_draft import create_editorial_gallery
    from tests.test_editorial_comparison import comparison_data
    sources = make_sources(tmp_path, 2)
    for index, medium in enumerate(('Oil on canvas', 'Watercolour on paper')):
        sources[index] = sources[index].model_copy(update={'artwork': sources[index].artwork.model_copy(
            update={'medium': medium})})
    data = comparison_data()
    data.update(comparison_aspect='medium',
                comparison_rationale='The different recorded materials provide the basis for this comparison.')
    output = tmp_path / 'comparison'
    report = create_editorial_gallery(output, sources, data)
    assert [page['role'] for page in report['pages']] == ['artwork', 'artwork', 'comparison']
    assert data['comparison_rationale'] in (output / 'index.html').read_text()
    assert len(report['sources']) == 2
