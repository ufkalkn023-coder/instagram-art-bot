"""Actual project previews grouped into a local, non-publishing editorial workspace."""
import hashlib
import html
import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from src.editorial_dossier import EditorialDossier, render_dossier
from src.editorial_story import EditorialStoryPlan
from src.editorial_v2 import StrictModel
from src.story_feed import review_snapshot
from src.story_project import _project_lock, load_project


class WorkspaceProject(StrictModel):
    id: str = Field(pattern=r'^p-[0-9]{2}$')
    directory: str = Field(min_length=1, max_length=4096)
    title: str = Field(min_length=1, max_length=90)
    revision: int = Field(ge=1, strict=True)
    preview_path: str = Field(pattern=r'^projects/p-[0-9]{2}/pages/[a-z][a-z0-9_-]{0,63}-[0-9a-f]{20}\.jpg$')
    preview_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    page_count: int = Field(ge=1, le=10, strict=True)


class EditorialWorkspace(StrictModel):
    schema_version: Literal['artfolio-editorial-workspace-v1'] = 'artfolio-editorial-workspace-v1'
    projects: tuple[WorkspaceProject, ...] = Field(min_length=1, max_length=12)
    history_projects: tuple[str, ...] = Field(default=(), max_length=100)
    dossier: EditorialDossier | None = None
    publication_ready: Literal[False] = False

    @model_validator(mode='after')
    def bounded_context(self):
        if len(self.projects) + len(self.history_projects) > 100:
            raise ValueError('Workspace and history together are bounded to one hundred projects')
        paths = [Path(entry.directory).resolve() for entry in self.projects]
        paths.extend(Path(path).resolve() for path in self.history_projects)
        if len(set(paths)) != len(paths):
            raise ValueError('Do not repeat a project directory in the workspace context')
        if self.dossier and any(entry.project_id not in {row.id for row in self.projects}
                                for entry in self.dossier.contributions):
            raise ValueError('Dossier refers to a project outside this workspace')
        return self


def workspace_rows(value):
    workspace = EditorialWorkspace.model_validate(value)
    if (len({entry.id for entry in workspace.projects}) != len(workspace.projects)
            or len({Path(entry.directory).resolve() for entry in workspace.projects}) != len(workspace.projects)):
        raise ValueError('Workspace project identities and directories must be unique')
    rows = []
    for entry in workspace.projects:
        directory = Path(entry.directory).resolve()
        with _project_lock(directory):
            project = load_project(directory)
            if not isinstance(project.plan, EditorialStoryPlan):
                raise ValueError('Workspace requires explicit editorial project versions')
            snapshot = review_snapshot(directory, project)
            page = Path(snapshot['paths'][0])
            relative = page.relative_to(directory).as_posix()
            rows.append(dict(id=entry.id, directory=str(directory), title=project.plan.public_title,
                             revision=project.revision, preview_path=f'projects/{entry.id}/{relative}',
                             preview_sha256=snapshot['pages'][0]['sha256'], page_count=len(project.plan.slides)))
    return rows


def load_workspace(directory):
    path = Path(directory).resolve() / 'workspace.json'
    if path.stat().st_size > 128_000 or path.is_symlink():
        raise ValueError('Workspace manifest is unsafe or oversized')
    workspace = EditorialWorkspace.model_validate_json(path.read_text(encoding='utf-8'))
    value = workspace.model_dump(mode='json')
    workspace_rows(value)
    return value


def workspace_repetition(value, *, current_directory=None, current_brief=None):
    from src.editorial_repetition import repetition_warnings
    workspace = EditorialWorkspace.model_validate(value)
    records = []
    entries = [(entry.id, Path(entry.directory).resolve()) for entry in workspace.projects]
    entries.extend((f'history-{n}', Path(path).resolve()) for n, path in enumerate(workspace.history_projects, 1))
    for identity, directory in entries:
        if current_directory is not None and directory == current_directory:
            brief = current_brief
        else:
            project = load_project(directory)
            if not isinstance(project.plan, EditorialStoryPlan):
                raise ValueError('Repetition context requires editorial project versions')
            brief = project.plan.brief
        records.append((directory, dict(id=identity, brief=brief)))
    if current_directory is not None:
        return repetition_warnings(current_brief, [record for directory, record in records if directory != current_directory])
    return {entry.id: repetition_warnings(record['brief'], [other for directory, other in records
                                                          if directory != Path(entry.directory).resolve()])
            for entry in workspace.projects for directory, record in records if directory == Path(entry.directory).resolve()}


def render_workspace(rows, *, extra=''):
    tiles = []
    for row in rows:
        identity, title = row['id'], html.escape(row['title'])
        tiles.append(f'<article data-project="{identity}"><a href="projects/{identity}/index.html">'
                     f'<img src="{row["preview_path"]}" alt="{html.escape(row["title"], quote=True)}" width="1080" height="1350">'
                     f'<h2>{title}</h2></a><p>{row["revision"]}. sürüm / {row["page_count"]} sayfa</p>'
                     f'<div class="order-controls"><button type="button" data-move="-1" aria-label="Öne al: {html.escape(row["title"], quote=True)}">Öne al</button>'
                     f'<button type="button" data-move="1" aria-label="Arkaya al: {html.escape(row["title"], quote=True)}">Arkaya al</button></div></article>')
    return ('''<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artfolio / Akış masası</title><style>
*{box-sizing:border-box}body{margin:0;padding:28px clamp(12px,4vw,64px);color:#252522;background:white;font:16px/1.6 system-ui,sans-serif}
main{max-width:1180px;margin:auto}h1,h2{font-family:Georgia,serif;font-weight:normal;line-height:1.2}h1{font-size:clamp(32px,5vw,58px);max-width:800px}
header p{max-width:760px}.feed-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px 10px;margin-top:32px}
article{min-width:0}article img{display:block;width:100%;height:auto;aspect-ratio:4/5;object-fit:contain;background:#f8f8f8}
a{color:inherit;text-decoration-thickness:1px;text-underline-offset:4px}article a{text-decoration:none}h2{font-size:clamp(16px,2vw,25px);margin:14px 0 8px;overflow-wrap:anywhere}
article p{font-size:13px;color:#555}.order-controls{display:flex;flex-wrap:wrap;gap:6px}button{padding:7px 10px;border:1px solid #aaa;background:white;color:inherit;cursor:pointer;font:14px system-ui}
button:disabled{opacity:.4;cursor:default}:focus-visible{outline:3px solid #315f96;outline-offset:3px}#order-status{min-height:26px;font-size:14px}
@media(max-width:480px){.feed-grid{gap:22px 7px}article p{font-size:12px}.order-controls button{font-size:12px;padding:6px}}
</style><main><header><p>Artfolio / Akış masası</p><h1>Eserler birlikte nasıl görünüyor?</h1>
<p>Yalnız önizleme. Gösterilen görseller kaydedilmiş taslaklardır; bu görünüm canlı profil veya yayın kuyruğu değildir.
Sıra değişiklikleri bu sayfada kalır. Renk, ölçek ve boşlukların akışına bakın.</p></header>
<p id="order-status" role="status" aria-live="polite"></p><div class="feed-grid" id="feed-grid">'''
            + ''.join(tiles) + '</div>' + extra + '''</main><script>
const grid=document.getElementById('feed-grid'),status=document.getElementById('order-status');
function refreshOrder(){const items=[...grid.children];items.forEach((item,n)=>{item.querySelector('[data-move="-1"]').disabled=n===0;item.querySelector('[data-move="1"]').disabled=n===items.length-1;});}
grid.addEventListener('click',event=>{const button=event.target.closest('button[data-move]');if(!button||button.disabled)return;const item=button.closest('article'),direction=Number(button.dataset.move),other=direction<0?item.previousElementSibling:item.nextElementSibling;if(!other)return;if(direction<0)grid.insertBefore(item,other);else grid.insertBefore(other,item);refreshOrder();const focusButton=item.querySelector(direction<0?'[data-move="1"]':'[data-move="-1"]');focusButton.focus();status.textContent='Önizleme sırası değişti. Yayın kuyruğu değişmedi.';});
refreshOrder();
</script></html>''')


def create_editorial_workspace(output, projects, *, history_projects=(), dossier=None):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Workspace output must be a new directory')
    if not isinstance(projects, (list, tuple)) or not 1 <= len(projects) <= 12:
        raise ValueError('Select one to twelve explicit editorial projects')
    # Initial placeholders are replaced by verified render paths before persistence.
    initial = dict(dossier=dossier, history_projects=[str(Path(path).resolve()) for path in history_projects],
                   projects=[dict(id=f'p-{n:02d}', directory=str(Path(path).resolve()), title='Preview', revision=1,
                                  preview_path=f'projects/p-{n:02d}/pages/artwork-1-{"0"*20}.jpg',
                                  preview_sha256='0'*64, page_count=1) for n, path in enumerate(projects, 1)])
    rows = workspace_rows(initial)
    report = EditorialWorkspace(projects=tuple(WorkspaceProject(**row) for row in rows),
                                 history_projects=tuple(initial['history_projects']), dossier=dossier).model_dump(mode='json')
    warnings = workspace_repetition(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.workspace-') as temporary:
        stage = Path(temporary)
        for row in rows:
            directory = Path(row['directory'])
            with _project_lock(directory):
                project = load_project(directory)
                snapshot = review_snapshot(directory, project)
                if snapshot['pages'][0]['sha256'] != row['preview_sha256'] or project.revision != row['revision']:
                    raise ValueError('Project changed while building workspace')
                report_path = (directory / 'current').resolve() / 'report.json'
                rendered = json.loads(report_path.read_text())
                from src.editorial_review import review_editorial_page
                destination = stage / 'projects' / row['id']
                (destination / 'pages').mkdir(parents=True)
                context = {'repetition': warnings[row['id']],
                           'dossier': render_dossier(report['dossier'], rows, project_id=row['id'])}
                (destination / 'index.html').write_text(review_editorial_page(project, rendered, review_context=context), encoding='utf-8')
                for path, page in zip(snapshot['paths'], snapshot['pages']):
                    target = destination / 'pages' / Path(path).name
                    shutil.copyfile(path, target)
                    if hashlib.sha256(target.read_bytes()).hexdigest() != page['sha256']:
                        raise ValueError('Project media changed while building workspace')
        (stage / 'workspace.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (stage / 'index.html').write_text(render_workspace(rows, extra=render_dossier(report['dossier'], rows)), encoding='utf-8')
        if output.exists() or output.is_symlink():
            raise FileExistsError('Workspace output changed during creation')
        stage.rename(output)
    return report
