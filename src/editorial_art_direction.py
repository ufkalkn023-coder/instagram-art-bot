"""Quiet artwork-led layouts for local editorial drafts."""
from PIL import Image, ImageDraw

from src.editorial_copy import source_credit
from src.story_design import _draw_text, _fit_image

STYLES = ('artwork_led', 'journal', 'detail_study')
PAPER = (248, 247, 243)


def _text(canvas, text, y, *, height, size=32, minimum=26, serif=False, x=64, width=952):
    return _draw_text(ImageDraw.Draw(canvas), text, (x, y), width=width,
                      height=height, size=size, minimum=minimum, serif=serif)


def _art(canvas, source, box, y, *, focus=None, x=0, width=1080):
    image = _fit_image(source, box, focus)
    canvas.paste(image, (x + (width - image.width) // 2, y + (box[1] - image.height) // 2))


def _credit(source):
    return source_credit(source).replace('\n', ' · ')


def render_editorial_page(page, brief, sources, style):
    if style not in STYLES:
        raise ValueError('Unknown editorial art direction')
    canvas = Image.new('RGB', (1080, 1350), PAPER)
    registry = {source.artwork.canonical_id: source for source in sources}
    if page['role'] == 'comparison':
        _text(canvas, brief.title, 54, height=170, size=58, minimum=40, serif=True)
        for index, identity in enumerate(brief.source_ids):
            source = registry[identity]
            _art(canvas, source, (448, 650), 250, x=64 + index * 504, width=448)
            _text(canvas, _credit(source), 930, height=130, size=22, minimum=20,
                  x=64 + index * 504, width=448)
        _text(canvas, brief.comparison_rationale, 1110, height=180, size=30)
        return canvas
    source = registry[page['artwork_id']]
    if page['role'] == 'detail':
        note = next(note for note in brief.notes if note.id == page['note_id'])
        _text(canvas, note.heading, 54, height=155, size=58, minimum=40, serif=True)
        _art(canvas, source, (952, 780), 240, focus=note.focus)
        _text(canvas, note.text, 1055, height=170, size=30)
        _text(canvas, 'Detail · ' + _credit(source), 1240, height=85, size=20, minimum=18)
    elif style == 'journal':
        _text(canvas, page['title'], 54, height=200, size=64, minimum=40, serif=True)
        _art(canvas, source, (952, 740), 290)
        _text(canvas, page['text'], 1070, height=155, size=30)
        _text(canvas, _credit(source), 1250, height=75, size=20, minimum=18)
    else:
        _art(canvas, source, (1008, 1090), 36)
        _text(canvas, page['title'], 1160, height=80, size=40, minimum=30, serif=True)
        _text(canvas, _credit(source), 1250, height=75, size=20, minimum=18)
    return canvas
