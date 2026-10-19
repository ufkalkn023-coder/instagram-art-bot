"""Explicit, source-bound editorial briefs for unapproved local art stories."""
from typing import Literal

from pydantic import Field, model_validator

from src.editorial_design import _validate_focus
from src.editorial_v2 import SourceArtwork, StrictModel

CATALOGUE_FIELDS = {'title': 'title', 'artist': 'artist_name', 'date': 'creation_date', 'medium': 'medium'}


class EditorialNote(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    artwork_id: str = Field(min_length=1, max_length=150)
    kind: Literal['museum_quote', 'catalogue_fact', 'visual_note']
    text: str = Field(min_length=1, max_length=350)
    heading: str = Field(default='', max_length=80)
    field: Literal['title', 'artist', 'date', 'medium'] | None = None
    focus: tuple[float, float, float, float] | None = None

    @model_validator(mode='after')
    def note_contract(self):
        if (self.kind == 'catalogue_fact') != (self.field is not None):
            raise ValueError('Only catalogue facts require a catalogue field')
        if self.kind != 'catalogue_fact' and len(self.text.strip()) < 8:
            raise ValueError('Quotes and visual notes must contain a specific observation')
        if self.focus is not None:
            _validate_focus(self.focus)
        return self


class EditorialBrief(StrictModel):
    schema_version: Literal['artfolio-editorial-brief-v1'] = 'artfolio-editorial-brief-v1'
    narrative: Literal['single_study', 'comparison', 'thematic_selection']
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    title: str = Field(min_length=3, max_length=90)
    angle: str = Field(min_length=12, max_length=400)
    lead: str = Field(default='', max_length=500)
    notes: tuple[EditorialNote, ...] = Field(min_length=1, max_length=12)
    comparison_aspect: Literal['composition', 'gesture', 'light', 'medium', 'date'] | None = None
    comparison_rationale: str = Field(default='', max_length=350)
    manual_review_required: Literal[True] = True
    publication_ready: Literal[False] = False

    @model_validator(mode='after')
    def membership(self):
        if len(self.title.strip()) < 3 or len(self.angle.strip()) < 12:
            raise ValueError('A title and specific editorial angle cannot be blank')
        count = len(self.source_ids)
        if len(set(self.source_ids)) != count or len({n.id for n in self.notes}) != len(self.notes):
            raise ValueError('Source and note identities must be unique')
        if ((self.narrative == 'single_study' and count != 1)
                or (self.narrative == 'comparison' and count != 2)
                or (self.narrative == 'thematic_selection' and count < 3)):
            raise ValueError('Source count must match the editorial narrative')
        if {note.artwork_id for note in self.notes} != set(self.source_ids):
            raise ValueError('Each source requires its own explicit editorial evidence')
        return self


def validate_editorial_brief(value, sources: list[SourceArtwork]) -> EditorialBrief:
    brief = EditorialBrief.model_validate(value.model_dump() if isinstance(value, EditorialBrief) else value)
    registry = {}
    for source in sources:
        validated = SourceArtwork.model_validate(source.model_dump())
        identity = validated.artwork.canonical_id
        if identity in registry:
            raise ValueError('Duplicate source identity')
        registry[identity] = validated.artwork
    if not set(brief.source_ids) <= set(registry):
        raise ValueError('Editorial brief refers to an unknown source')
    for note in brief.notes:
        art = registry[note.artwork_id]
        if note.kind == 'museum_quote' and note.text not in (art.description or ''):
            raise ValueError('Museum quotations must be exact source excerpts')
        if note.kind == 'catalogue_fact':
            factual = getattr(art, CATALOGUE_FIELDS[note.field]) or ''
            if note.text != factual:
                raise ValueError('Catalogue facts must match the recorded field exactly')
    return brief
