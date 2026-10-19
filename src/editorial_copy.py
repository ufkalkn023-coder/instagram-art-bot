"""Specific editorial copy checks, not an automated art-history fact checker."""
import unicodedata

from src.editorial_brief import EditorialBrief, validate_editorial_brief
from src.editorial_v2 import _normalized, _repeated

STOCK_PHRASES = (
    'timeless beauty', 'a visual symphony', 'a journey through', 'discover the magic',
    'step into a world', 'delve into', 'dive into', 'what connects these works',
    'look closely at these', 'look again at the complete artwork', 'return to the whole',
    'a closer look', 'look side by side', 'two works side by side',
    'save this post', 'follow for more', 'which one is your favorite',
    'the artist intended', 'the artist wanted', 'the artist felt',
)


def check_copy(text: str) -> None:
    normalized = f' {_normalized(text)} '
    if any(f' {_normalized(phrase)} ' in normalized for phrase in STOCK_PHRASES):
        raise ValueError('Use a specific observation instead of a stock hook or unsupported intention')
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in text):
        raise ValueError('Editorial copy contains control characters')


def validate_editorial_copy(brief: EditorialBrief, *, headline_history=()) -> None:
    if (not isinstance(headline_history, (list, tuple)) or len(headline_history) > 100
            or any(not isinstance(title, str) for title in headline_history)):
        raise ValueError('Use at most one hundred recorded titles')
    for text in (brief.title, brief.angle, brief.lead, brief.comparison_rationale):
        check_copy(text)
    for note in brief.notes:
        check_copy(note.heading)
        if note.kind == 'visual_note':
            check_copy(note.text)
    if _repeated(brief.title, list(headline_history)):
        raise ValueError('Editorial title repeats an existing title')


def source_credit(source) -> str:
    art = source.artwork
    identity = art.artist_display_name or art.artist_name
    date = art.creation_date_display or art.creation_date
    return '\n'.join(str(value).strip() for value in
                     (f'{art.title} — {identity}' if identity else art.title, date, art.museum_name)
                     if value is not None and str(value).strip())


def compose_editorial_caption(brief: EditorialBrief, sources, *, headline_history=()) -> str:
    brief = validate_editorial_brief(brief, sources)
    validate_editorial_copy(brief, headline_history=headline_history)
    registry = {source.artwork.canonical_id: source for source in sources}
    sections = [brief.title]
    if brief.lead.strip():
        sections.append(brief.lead.strip())
    sections.extend(source_credit(registry[identity]) for identity in brief.source_ids)
    caption = '\n\n'.join(sections)
    if len(caption) > 2200:
        raise ValueError('Editorial copy and factual credits exceed caption capacity')
    return caption
