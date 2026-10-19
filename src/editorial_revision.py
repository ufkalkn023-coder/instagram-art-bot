"""Exact predecessor comparison and review state without publication side effects."""
import json
import re
import html
import hashlib
from pathlib import Path

from src.editorial_story import EditorialStoryPlan
from src.story_feed import review_snapshot
from src.story_project import StoryProject, _project_lock, load_project


def _read_json(path, limit=2_000_000):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Revision document is unsafe or oversized')
    return json.loads(path.read_text(encoding='utf-8'))


def _fields(plan):
    values = {key: getattr(plan.brief, key) for key in
              ('title', 'angle', 'lead', 'comparison_aspect', 'comparison_rationale', 'source_ids')}
    values['style'] = plan.style
    for note in plan.brief.notes:
        values.update({f'notes.{note.id}.{key}': getattr(note, key)
                       for key in ('heading', 'text', 'focus', 'kind', 'field', 'artwork_id')})
    return values


def _pages(directory, project, report):
    slides = report.get('package', {}).get('slides', [])
    if report.get('revision') != project.revision or len(slides) != len(project.plan.slides):
        raise ValueError('Previous revision report does not match its saved plan')
    result = []
    for slide, rendered in zip(project.plan.slides, slides):
        relative = rendered.get('path', '')
        if (rendered.get('slide_id') != slide.id or rendered.get('role') != slide.role
                or rendered.get('artwork_ids') != list(slide.artwork_ids)
                or rendered.get('focus') != (list(slide.focus) if slide.focus else None)
                or rendered.get('focus_basis') != slide.focus_basis
                or not re.fullmatch(r'pages/[a-z][a-z0-9_-]{0,63}-[0-9a-f]{20}\.jpg', relative)):
            raise ValueError('Previous revision page does not match its saved plan')
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory / 'pages') or not path.is_file() or path.stat().st_size > 8_000_000:
            raise ValueError('Previous revision page escaped project')
        metadata = rendered.get('render_metadata', {})
        if not isinstance(metadata, dict) or metadata.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('Previous revision image bytes no longer match the saved report')
        result.append(dict(path=relative, role=slide.role))
    return result


def revision_comparison(directory, *, project=None, report=None):
    """Call with project/report only while holding the project's existing lock."""
    directory = Path(directory).resolve()
    if project is None:
        with _project_lock(directory):
            current = load_project(directory)
            review_snapshot(directory, current)
            rendered = _read_json((directory / 'current').resolve() / 'report.json')
            return revision_comparison(directory, project=current, report=rendered)
    pointer = (directory / 'current').resolve()
    if not pointer.is_relative_to(directory / '.revisions'):
        raise ValueError('Revision pointer escaped project')
    parent_file = pointer / '.previous.json'
    if not isinstance(project.plan, EditorialStoryPlan) or not parent_file.exists():
        return dict(available=False, reason='Bu sürüm için kayıtlı bir önceki sürüm yok.', changes=[],
                    before_pages=[], after_pages=[])
    parent = _read_json(parent_file, 10_000)
    relative = parent.get('snapshot') if isinstance(parent, dict) else None
    if not isinstance(relative, str) or not re.fullmatch(r'\.revisions/[0-9]{6}-[a-f0-9]{32}', relative):
        raise ValueError('Invalid previous revision pointer')
    previous = (directory / relative).resolve()
    if not previous.is_relative_to(directory / '.revisions') or previous == pointer:
        raise ValueError('Previous revision escaped project')
    before = StoryProject.model_validate(_read_json(previous / 'project.json'))
    if before.revision != project.revision - 1 or not isinstance(before.plan, EditorialStoryPlan):
        raise ValueError('Previous committed revision is inconsistent')
    before_report = _read_json(previous / 'report.json')
    old, new = _fields(before.plan), _fields(project.plan)
    changes = [dict(field=key, before=old.get(key), after=new.get(key)) for key in sorted(old.keys() | new.keys())
               if old.get(key) != new.get(key)]
    return dict(available=True, previous_revision=before.revision, revision=project.revision, changes=changes,
                before_pages=_pages(directory, before, before_report), after_pages=_pages(directory, project, report))


def editorial_review_state(directory, project, report):
    snapshot = review_snapshot(directory, project)
    approved = False
    review_path = directory / '.story-review.json'
    if review_path.is_file() and not review_path.is_symlink():
        review = _read_json(review_path, 10_000)
        approved = (isinstance(review, dict) and review.get('schema_version') == 'artfolio-story-review-v1'
                    and review.get('revision') == project.revision and review.get('digest') == snapshot['digest'])
    return dict(digest=snapshot['digest'], approved=approved,
                comparison=revision_comparison(directory, project=project, report=report))


def render_revision_comparison(comparison):
    if not comparison or not comparison.get('available'):
        return '<section id="revision-comparison"><h2>Sürüm karşılaştırması</h2><p>Bu sürüm için kayıtlı bir önceki sürüm yok.</p></section>'
    labels = dict(title='Başlık', angle='Anlatım açısı', lead='Kısa giriş', style='Görsel düzen',
                  comparison_aspect='Karşılaştırma yönü', comparison_rationale='Karşılaştırma gerekçesi',
                  source_ids='Eser sırası', heading='Detay başlığı', text='Not metni', focus='Detay alanı',
                  kind='Not türü', field='Katalog alanı', artwork_id='Kaynak eser')
    changes = []
    for change in comparison['changes']:
        parts = change['field'].split('.')
        label = labels.get(parts[-1], parts[-1])
        if len(parts) == 3:
            label = f'Not {parts[1]} / {label}'
        before, after = (html.escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                         for value in (change['before'], change['after']))
        changes.append(f'<li><strong>{html.escape(label)}</strong><p>Önce: {before}</p><p>Sonra: {after}</p></li>')
    columns = []
    for label, key in (('Önceki sürüm', 'before_pages'), ('Mevcut sürüm', 'after_pages')):
        pages = ''.join(f'<img src="{page["path"]}" alt="{label}: {html.escape(page["role"])}" width="1080" height="1350" loading="lazy">'
                        for page in comparison[key])
        columns.append(f'<div><h3>{label}</h3>{pages}</div>')
    return ('<section id="revision-comparison"><h2>Sürüm karşılaştırması</h2>'
            f'<p>{comparison["previous_revision"]}. sürümden {comparison["revision"]}. sürüme</p>'
            f'<ul>{"".join(changes) if changes else "<li>Metin veya detay alanı değişmedi.</li>"}</ul>'
            f'<div class="revision-images">{"".join(columns)}</div></section>')
