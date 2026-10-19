import pytest

from tests.test_story_suggestions import make_sources


def test_source_panel_distinguishes_actual_quote_and_shows_exact_context(tmp_path):
    from src.editorial_brief import EditorialNote
    from src.editorial_sources import source_note_panel
    source = make_sources(tmp_path, 1)[0]
    quote = 'Broad marks remain visible near the edge.'
    source = source.model_copy(update={'artwork': source.artwork.model_copy(update={
        'description': f'Before the observation. {quote} After the observation.',
        'artwork_url': 'https://www.clevelandart.org/art/101'})})
    note = EditorialNote(id='edge', artwork_id='cleveland_0', kind='museum_quote', text=quote)
    panel = source_note_panel(note, source)
    assert 'Doğrudan müze alıntısı' in panel and quote in panel
    assert 'Before the observation.' in panel and 'After the observation.' in panel
    assert 'https://www.clevelandart.org/art/101' in panel and 'CC0' in panel
    with pytest.raises(ValueError):
        source_note_panel(note.model_copy(update={'text': 'An invented historical claim about the painter.'}), source)


def test_source_panel_explains_visual_notes_without_inventing_museum_context(tmp_path):
    from src.editorial_brief import EditorialNote
    from src.editorial_sources import source_note_panel
    note = EditorialNote(id='edge', artwork_id='cleveland_0', kind='visual_note', text='Blue marks overlap near the edge.')
    panel = source_note_panel(note, make_sources(tmp_path, 1)[0])
    assert 'Editoryal gözlem' in panel and 'müze kaydından alıntı değildir' in panel
    assert 'Müze metnindeki bağlam' not in panel


def test_source_panel_escapes_text_and_refuses_unsafe_links(tmp_path):
    from src.editorial_brief import EditorialNote
    from src.editorial_sources import source_note_panel
    source = make_sources(tmp_path, 1)[0]
    source = source.model_copy(update={'artwork': source.artwork.model_copy(update={
        'title': '<script>alert(1)</script>', 'artwork_url': 'javascript:alert(1)'})})
    note = EditorialNote(id='edge', artwork_id='cleveland_0', kind='visual_note', text='Blue marks overlap near the edge.')
    panel = source_note_panel(note, source)
    assert '<script>' not in panel and '&lt;script&gt;' in panel
    assert 'href=' not in panel and 'javascript:' not in panel
