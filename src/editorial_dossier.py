"""A small, explicit reading guide; never a publishing instruction."""
import html

from pydantic import Field, model_validator

from src.editorial_v2 import StrictModel


class DossierContribution(StrictModel):
    project_id: str = Field(pattern=r'^p-[0-9]{2}$')
    contribution: str = Field(min_length=12, max_length=350)


class EditorialDossier(StrictModel):
    title: str = Field(min_length=3, max_length=120)
    question: str = Field(min_length=12, max_length=400)
    contributions: tuple[DossierContribution, ...] = Field(min_length=3, max_length=5)

    @model_validator(mode='after')
    def distinct_contributions(self):
        identities = [entry.project_id for entry in self.contributions]
        texts = [' '.join(entry.contribution.casefold().split()) for entry in self.contributions]
        if len(set(identities)) != len(identities) or len(set(texts)) != len(texts):
            raise ValueError('Each dossier post needs its own distinct contribution')
        return self


def render_dossier(value, rows, *, project_id=None):
    if value is None:
        return ''
    dossier = EditorialDossier.model_validate(value)
    titles = {row['id']: row['title'] for row in rows}
    if any(entry.project_id not in titles for entry in dossier.contributions):
        raise ValueError('Dossier refers to a project outside this workspace')
    entries = [entry for entry in dossier.contributions if project_id is None or entry.project_id == project_id]
    if not entries:
        return ''
    items = []
    for entry in entries:
        title = html.escape(titles[entry.project_id])
        if project_id is None:
            title = f'<a href="projects/{entry.project_id}/index.html">{title}</a>'
        items.append(f'<li><strong>{title}</strong><p>{html.escape(entry.contribution)}</p></li>')
    return ('<section id="editorial-dossier"><h2>' + html.escape(dossier.title)
            + '</h2><p>' + html.escape(dossier.question) + '</p><ol>' + ''.join(items)
            + '</ol><p>Önerilen okuma sırası. Her paylaşım ayrı incelenir ve ayrı onaylanır.</p></section>')
