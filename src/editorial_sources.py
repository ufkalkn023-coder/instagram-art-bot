"""Readable source provenance, exact quotations and clearly labeled observations."""
import html
from urllib.parse import urlsplit

from src.editorial_brief import CATALOGUE_FIELDS, EditorialNote
from src.editorial_v2 import SourceArtwork

NOTE_LABELS = {'museum_quote': 'Doğrudan müze alıntısı', 'catalogue_fact': 'Katalog bilgisi',
               'visual_note': 'Editoryal gözlem'}


def _source_link(art):
    for url in (art.artwork_url, art.museum_url):
        if not url:
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                continue
            parsed.port  # Reject malformed ports before offering the link.
        except ValueError:
            continue
        return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">Müzedeki eser kaydı</a>'
    return '<span>Doğrudan kaynak bağlantısı bu kayıtta bulunmuyor.</span>'


def source_note_panel(note, source):
    note = EditorialNote.model_validate(note.model_dump())
    source = SourceArtwork.model_validate(source.model_dump())
    art = source.artwork
    if note.artwork_id != art.canonical_id:
        raise ValueError('Source panel note must match the displayed artwork')
    context = ''
    if note.kind == 'museum_quote':
        description = art.description or ''
        position = description.find(note.text)
        if position < 0:
            raise ValueError('Museum quotation must match retained source text')
        excerpt = description[max(0, position - 120):position + len(note.text) + 120]
        context = '<h4>Müze metnindeki bağlam</h4><blockquote>' + html.escape(excerpt) + '</blockquote>'
        explanation = 'Alıntı, kaydedilen kaynak metninden değiştirilmeden alınmıştır.'
    elif note.kind == 'catalogue_fact':
        if note.text != (getattr(art, CATALOGUE_FIELDS[note.field]) or ''):
            raise ValueError('Catalogue fact must match the recorded field')
        explanation = 'Bu değer kaydedilen katalog alanıyla birebir eşleşir.'
    else:
        explanation = 'Bu yorum müze kaydından alıntı değildir; görüntü üzerinden editoryal olarak kontrol edin.'
    facts = (('Eser', art.title), ('Sanatçı', art.artist_display_name or art.artist_name),
             ('Tarih', art.creation_date_display or art.creation_date), ('Malzeme', art.medium),
             ('Müze', art.museum_name), ('Kullanım hakkı kaydı', art.license), ('Kaynak kredisi', art.credit_line))
    values = ''.join(f'<dt>{label}</dt><dd>{html.escape(str(value))}</dd>' for label, value in facts if value)
    return (f'<details class="source-panel"><summary>{NOTE_LABELS[note.kind]} ve kaynak bilgisi</summary>'
            f'<p>{explanation}</p><dl>{values}</dl><p>{_source_link(art)}</p>{context}</details>')
