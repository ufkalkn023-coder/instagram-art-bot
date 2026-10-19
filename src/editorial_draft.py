"""Source-bound editorial previews, deliberately separate from publication packages."""
import hashlib
import html
import json
import shutil
import tempfile
from pathlib import Path

from PIL import Image

from src.editorial_art_direction import render_editorial_page
from src.editorial_brief import validate_editorial_brief
from src.editorial_comparison import validate_editorial_comparison
from src.editorial_copy import compose_editorial_caption
from src.editorial_details import validate_editorial_details
from src.editorial_v2 import SourceArtwork
from src.quality_filter import validate_local_image_file


def build_editorial_draft(value, sources, *, headline_history=()):
    brief = validate_editorial_brief(value, sources)
    validate_editorial_comparison(brief, sources)
    caption = compose_editorial_caption(brief, sources, headline_history=headline_history)
    details = validate_editorial_details(brief, sources)
    registry = {source.artwork.canonical_id: source for source in sources}
    pages = []
    for index, identity in enumerate(brief.source_ids):
        notes = [note for note in brief.notes if note.artwork_id == identity]
        pages.append(dict(role='artwork', artwork_id=identity,
                          title=brief.title if index == 0 else registry[identity].artwork.title,
                          text=brief.lead if index == 0 and brief.lead.strip() else notes[0].text))
        pages.extend(dict(role='detail', artwork_id=identity, note_id=note.id)
                     for note in details if note.artwork_id == identity)
    if brief.narrative == 'comparison':
        pages.append(dict(role='comparison'))
    if len(pages) > 10:
        raise ValueError('Editorial draft exceeds ten meaningful pages; split the brief')
    return dict(schema_version='artfolio-editorial-draft-v1', manual_review_required=True,
                publication_ready=False, brief=brief.model_dump(mode='json'), pages=pages, caption=caption)


def _gallery_html(report):
    sections = []
    evidence = ''.join(f'<li><strong>{html.escape(note["kind"])}</strong> · '
                       f'{html.escape(note["artwork_id"])}: {html.escape(note["text"])}</li>'
                       for note in report['brief']['notes'])
    rationale = report['brief']['comparison_rationale']
    labels = {'artwork_led': 'Artwork led', 'journal': 'Journal', 'detail_study': 'Detail study'}
    for name, variant in report['variants'].items():
        figures = ''.join(f'<figure><img src="{html.escape(page["path"], quote=True)}" '
                          f'alt="{html.escape(page.get("title", page["role"]), quote=True)}" '
                          f'width="1080" height="1350" loading="lazy">'
                          f'<figcaption>{index + 1:02d} · {html.escape(page["role"])}</figcaption></figure>'
                          for index, page in enumerate(variant['pages']))
        sections.append(f'<section><h2>{labels[name]}</h2><div class="pages">{figures}</div></section>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Artfolio editorial review</title><style>'
            '*{box-sizing:border-box}body{margin:0;padding:40px clamp(16px,4vw,64px);background:#f8f7f3;'
            'color:#252522;font:17px/1.6 Georgia,serif}h1{font-size:clamp(32px,5vw,60px);line-height:1.1;'
            'max-width:900px}h2{font-size:26px;font-weight:normal}.meta,figcaption{font:13px/1.5 sans-serif;'
            'color:#555}section{border-top:1px solid #ccc;margin-top:40px;padding-top:12px}'
            '.pages{display:flex;gap:24px;flex-wrap:wrap}figure{margin:0;width:min(100%,360px)}'
            'img{width:100%;height:auto;display:block}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
            'font:17px/1.6 Georgia,serif;max-width:800px}</style><main>'
            f'<p class="meta">ARTFOLIO / LOCAL EDITORIAL DRAFT / MANUAL REVIEW REQUIRED</p>'
            f'<h1>{html.escape(report["brief"]["title"])}</h1>'
            f'<p>{html.escape(report["brief"]["angle"])}</p>'
            '<p class="meta">Visual notes are editorial observations, not verified historical claims. '
            'These previews are not approved publication packages.</p>'
            + ''.join(sections) + '<section><h2>Editorial evidence</h2>'
            + (f'<p>{html.escape(rationale)}</p>' if rationale else '')
            + f'<ul>{evidence}</ul></section><section><h2>Caption</h2><pre>{html.escape(report["caption"])}</pre>'
            '</section></main></html>')


def create_editorial_gallery(output, sources, value, *, headline_history=()):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Output must be a new directory')
    sources = [SourceArtwork.model_validate(source.model_dump()) for source in sources]
    report = build_editorial_draft(value, sources, headline_history=headline_history)
    brief = validate_editorial_brief(value, sources)
    selected = [source for identity in brief.source_ids for source in sources
                if source.artwork.canonical_id == identity]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.editorial-', dir=output.parent) as temporary:
        staging = Path(temporary)
        (staging / 'sources').mkdir()
        provenance = []
        copied = []
        for index, source in enumerate(selected):
            path = Path(source.image_path)
            if path.stat().st_size > 30_000_000 or not validate_local_image_file(str(path)).valid:
                raise ValueError('Source is not a bounded validated local image')
            with Image.open(path) as image:
                if image.width * image.height > 40_000_000 or min(image.size) < 300:
                    raise ValueError('Source dimensions outside editorial preview limits')
            destination = staging / 'sources' / f'work-{index + 1:02d}{path.suffix.lower()}'
            shutil.copyfile(path, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            copied.append(SourceArtwork(artwork=source.artwork, image_path=str(destination)))
            provenance.append(dict(artwork=source.artwork.model_dump(mode='json'),
                                   image_path=destination.relative_to(staging).as_posix(), sha256=digest))
        report['sources'] = provenance
        report['variants'] = {}
        styles = ['artwork_led', 'journal']
        if any(page['role'] == 'detail' for page in report['pages']):
            styles.append('detail_study')
        for style in styles:
            pages = list(report['pages'])
            if style == 'detail_study':
                first = next(index for index, page in enumerate(pages) if page['role'] == 'detail')
                pages.insert(0, pages.pop(first))
            directory = staging / style
            directory.mkdir()
            rendered = []
            for index, page in enumerate(pages):
                destination = directory / f'page-{index + 1:02d}.jpg'
                render_editorial_page(page, brief, copied, style).save(destination, quality=94, subsampling=0)
                rendered.append({**page, 'path': destination.relative_to(staging).as_posix()})
            report['variants'][style] = dict(pages=rendered)
        (staging / 'caption.txt').write_text(report['caption'], encoding='utf-8')
        (staging / 'draft.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (staging / 'index.html').write_text(_gallery_html(report), encoding='utf-8')
        # Reserve exclusively before moving completed files: concurrent users' outputs are never replaced.
        output.mkdir()
        try:
            for child in staging.iterdir():
                shutil.move(str(child), str(output / child.name))
        except OSError:
            shutil.rmtree(output)
            raise
    return report
