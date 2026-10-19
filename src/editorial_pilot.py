"""A small, non-publishing pilot with distinct source-bound editorial sequences."""
import html
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from src.editorial_brief import EditorialBrief
from src.editorial_draft import create_editorial_gallery
from src.editorial_v2 import StrictModel
from src.editorial_story import EditorialStoryPlan
from src.story_project import create_project


class PilotEntry(StrictModel):
    theme_id: str = Field(min_length=1, max_length=100)
    theme_title: str = Field(min_length=1, max_length=180)
    brief: EditorialBrief


class PilotSeries(StrictModel):
    title: str = Field(min_length=3, max_length=120)
    entries: tuple[PilotEntry, ...] = Field(min_length=4, max_length=4)


def build_editorial_pilot(output, sources, value, *, history=None, now=None):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Pilot output must be a new directory')
    series = PilotSeries.model_validate(value)
    used = [identity for entry in series.entries for identity in entry.brief.source_ids]
    available = [source.artwork.canonical_id for source in sources]
    if len(available) != 5 or len(set(available)) != 5 or sorted(used) != sorted(available):
        raise ValueError('Pilot must use each of five unique sources exactly once')
    report = dict(title=series.title, schema_version='artfolio-editorial-pilot-v1',
                  source_count=5, publication_ready=False, entries=[])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.pilot-') as temporary:
        stage = Path(temporary)
        headline_history = []
        for index, entry in enumerate(series.entries):
            name = f'story-{index + 1:02d}'
            selected = [source for source in sources if source.artwork.canonical_id in entry.brief.source_ids]
            gallery = create_editorial_gallery(stage / name, selected, entry.brief, headline_history=headline_history)
            style = ('detail_study' if 'detail_study' in gallery['variants'] else
                     'artwork_led' if index % 3 == 2 else 'journal')
            pages = gallery['variants'][style]['pages']
            plan = EditorialStoryPlan(theme_id=entry.theme_id, theme_title=entry.theme_title,
                                      brief=entry.brief, style=style)
            create_project(stage / name / 'project', selected, plan, headline_history=headline_history)
            report['entries'].append(dict(title=entry.brief.title, path=f'{name}/index.html',
                                          project_path=f'{name}/project',
                                          source_ids=list(entry.brief.source_ids), style=style,
                                          publication_format='single' if len(pages) == 1 else 'carousel',
                                          page_count=len(pages), publication_ready=False))
            headline_history.append(entry.brief.title)
        from src.editorial_rhythm import plan_editorial_projects, render_editorial_rhythm
        rhythm = plan_editorial_projects([stage / row['project_path'] for row in report['entries']],
                                         history if history is not None else {'publications': []},
                                         now=now if now is not None else datetime.now(timezone.utc))
        report['rhythm_history_basis'] = 'provided_snapshot' if history is not None else 'empty_demo_history'
        (stage / 'rhythm.json').write_text(json.dumps(rhythm, ensure_ascii=False, indent=2), encoding='utf-8')
        (stage / 'rhythm.html').write_text(render_editorial_rhythm(rhythm), encoding='utf-8')
        rows = ''.join(f'<li><a href="{row["project_path"]}/index.html">{html.escape(row["title"])}</a>'
                       f' — {row["style"]}, {row["page_count"]} sayfa · '
                       f'<a href="{row["path"]}">Tasarım alternatifleri</a></li>' for row in report['entries'])
        (stage / 'index.html').write_text('<!doctype html><html lang="tr"><meta charset="utf-8">'
                                         '<meta name="viewport" content="width=device-width,initial-scale=1">'
                                         '<title>Editoryal pilot</title><style>body{font:18px/1.6 Georgia;'
                                         'max-width:900px;margin:3rem auto;padding:0 20px}li{margin:20px 0}'
                                         'a{color:inherit}h1{line-height:1.2}</style>'
                                         f'<main><h1>{html.escape(series.title)}</h1>'
                                         '<p>Beş eser. Yerel inceleme taslakları; yayın onayı yok.</p>'
                                         f'<ul>{rows}</ul><p><a href="rhythm.html">Olası yayın ritmi</a></p>'
                                         '<p>Yayın geçmişi verilmediyse ritim boş örnek geçmişe dayanır.</p></main></html>', encoding='utf-8')
        (stage / 'pilot.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if output.exists() or output.is_symlink():
            raise FileExistsError('Pilot output changed during creation')
        stage.rename(output)
    return report
