"""Provisional48h slots assigned to real editorial projects, never to the queue."""
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.editorial_horizon import build_editorial_horizon
from src.editorial_story import EditorialStoryPlan
from src.insights_storage import parse_aware_timestamp
from src.story_project import _render_project, load_project


def plan_editorial_projects(projects, history, *, now, weeks=2):
    if not isinstance(projects, (list, tuple)) or not 1 <= len(projects) <= 12:
        raise ValueError('Use one to twelve real editorial projects')
    available, seen = [], set()
    for index, directory in enumerate(projects, 1):
        directory = Path(directory).resolve()
        project = load_project(directory)
        if not isinstance(project.plan, EditorialStoryPlan):
            raise ValueError('Editorial rhythm requires editorial project versions')
        if seen.intersection(project.plan.artwork_ids):
            raise ValueError('Do not repeat a source across the editorial series')
        seen.update(project.plan.artwork_ids)
        _render_project(directory, project, verify_only=True)
        status = 'unapproved'
        review_path = directory / '.story-review.json'
        if review_path.is_file() and not review_path.is_symlink() and review_path.stat().st_size <= 10_000:
            from src.story_feed import review_snapshot
            review = json.loads(review_path.read_text())
            snapshot = review_snapshot(directory, project)
            status = ('approved' if isinstance(review, dict) and review.get('schema_version') == 'artfolio-story-review-v1'
                      and review.get('revision') == project.revision and review.get('digest') == snapshot['digest'] else 'stale')
        available.append(dict(project_number=index, revision=project.revision, title=project.plan.public_title,
                              source_ids=list(project.plan.artwork_ids), style=project.plan.style,
                              narrative=project.plan.narrative, page_count=len(project.plan.slides), review_status=status,
                              publication_format='single' if len(project.plan.slides) == 1 else 'carousel'))
    horizon = build_editorial_horizon(history, now=now, weeks=weeks, seed='editorial-project-rhythm-v1')
    slots, unfilled = [], []
    last_narrative = None
    for slot in horizon['slots']:
        if not available:
            break
        candidates = [row for row in available if row['publication_format'] == slot['publication_format']]
        if not candidates:
            unfilled.append(dict(earliest_at=slot['earliest_at'], publication_format=slot['publication_format'],
                                 reason='no_matching_project'))
            continue
        chosen = next((row for row in candidates if row['narrative'] != last_narrative), candidates[0])
        available.remove(chosen)
        slots.append(dict(**chosen, earliest_at=slot['earliest_at'], provisional=True, publication_ready=False))
        last_narrative = chosen['narrative']
    return dict(schema_version='artfolio-editorial-rhythm-v1', generated_at=horizon['generated_at'], weeks=weeks,
                slots=slots, unfilled_slots=unfilled, unassigned_projects=available,
                queue_changed=False, publication_ready=False,
                limitations='Provisional project order; source/history eligibility and existing queue priority are not authorization.')


def render_editorial_rhythm(report):
    rows = []
    for row in report['slots']:
        local = parse_aware_timestamp(row['earliest_at']).astimezone(ZoneInfo('Europe/Istanbul'))
        rows.append(f'<tr><td>{local:%Y-%m-%d %H:%M}</td><td>{html.escape(row["title"])}</td>'
                    f'<td>{row["publication_format"]}</td><td>{row["page_count"]}</td>'
                    f'<td>{row["style"]}</td><td>{row["review_status"]}</td></tr>')
    return ('<!doctype html><html lang="tr"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Editoryal yayın ritmi</title>'
            '<style>body{font:17px/1.6 system-ui;max-width:1200px;margin:2rem auto;padding:0 20px}'
            'h1{font:40px Georgia}table{border-collapse:collapse;width:100%}td,th{padding:14px;text-align:left;'
            'border-bottom:1px solid #ccc}.scroll{overflow:auto}</style><main><h1>Editoryal yayın ritmi</h1>'
            '<p>Türkiye saati. Tarihler olası alt sınırlardır; gerçek yayın başarısına ve mevcut kuyruk önceliğine bağlıdır. '
            'Bu plan kuyruğu değiştirmez ve yayın onayı vermez.</p>'
            '<div class="scroll" tabindex="0" role="region" aria-label="Olası editoryal sıra"><table>'
            '<thead><tr><th>En erken olası</th><th>İçerik</th><th>Gönderi türü</th><th>Sayfa</th><th>Düzen</th><th>İnceleme</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            f'<p>Eşleşen taslak olmayan slot: {len(report["unfilled_slots"])}. '
            f'Bu döneme yerleşmeyen proje: {len(report["unassigned_projects"])}.</p></main></html>')
