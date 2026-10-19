"""Explicit editorial plan version hosted by the existing revisioned story project."""
import hashlib
import json
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import Field, model_validator

from src.carousel_cover import FONT_DIRECTORY
from src.editorial_brief import EditorialBrief
from src.editorial_design import CoverStyle
from src.editorial_draft import build_editorial_draft
from src.editorial_v2 import StrictModel
from src.quality_filter import validate_local_image_file
from src.story_plan import StorySlide
from src.story_quality import StoryIssue, StoryQuality


class EditorialStoryPlan(StrictModel):
    schema_version: Literal['artfolio-editorial-story-v2'] = 'artfolio-editorial-story-v2'
    theme_id: str = Field(min_length=1, max_length=100)
    theme_title: str = Field(min_length=1, max_length=180)
    brief: EditorialBrief
    style: Literal['journal', 'artwork_led', 'detail_study'] = 'journal'

    @model_validator(mode='after')
    def capacity(self):
        if len(self.slides) > 10:
            raise ValueError('Editorial story exceeds ten meaningful pages')
        if self.style == 'detail_study' and not any(note.focus and note.artwork_id == self.artwork_ids[0]
                                                   for note in self.brief.notes):
            raise ValueError('Detail study requires an explained focus on the first source')
        return self

    @property
    def public_title(self):
        return self.brief.title

    @property
    def editorial_angle(self):
        return self.brief.angle

    @property
    def narrative(self):
        return self.brief.narrative

    @property
    def artwork_ids(self):
        return self.brief.source_ids

    @property
    def headline_kind(self):
        return 'user_edit'

    @property
    def cover_style(self):
        return {'journal': CoverStyle.MUSEUM_JOURNAL, 'artwork_led': CoverStyle.ARTWORK_FIRST,
                'detail_study': CoverStyle.DETAIL_STUDY}[self.style]

    @property
    def slides(self):
        slides = []
        for index, identity in enumerate(self.artwork_ids):
            slides.append(StorySlide(id=f'artwork-{index + 1}', role='artwork', artwork_ids=(identity,)))
            slides.extend(StorySlide(id=f'detail-{note_index}', role='detail', artwork_ids=(identity,),
                                     title=note.heading, body=note.text, focus=note.focus, focus_basis='preview_focus')
                          for note_index, note in enumerate(self.brief.notes, 1) if note.artwork_id == identity and note.focus)
        if self.narrative == 'comparison':
            slides.append(StorySlide(id='comparison', role='comparison', artwork_ids=self.artwork_ids,
                                     title=self.public_title, body=self.brief.comparison_rationale))
        if self.style == 'detail_study':
            first = next((n for n, slide in enumerate(slides) if slide.role == 'detail'), None)
            if first is not None:
                slides.insert(0, slides.pop(first))
        return tuple(slides)


def assess_editorial_story(plan, sources, *, headline_history=None):
    try:
        plan = EditorialStoryPlan.model_validate(plan.model_dump())
        if set(plan.artwork_ids) != {source.artwork.canonical_id for source in sources}:
            raise ValueError('Source registry mismatch')
        build_editorial_draft(plan.brief, sources, headline_history=headline_history or ())
        for source in sources:
            path = Path(source.image_path)
            if path.stat().st_size > 30_000_000 or not validate_local_image_file(str(path)).valid:
                raise ValueError('Invalid source image')
            with Image.open(path) as image:
                if min(image.size) < 300:
                    raise ValueError('Insufficient source resolution')
    except (ValueError, OSError):
        return StoryQuality(can_render=False, issues=(StoryIssue(severity='critical', code='editorial_brief'),))
    return StoryQuality(can_render=True, issues=(StoryIssue(severity='warning', code='manual_review_required'),))


def render_editorial_project(directory, project, *, verify_only=False):
    from src import editorial_art_direction, editorial_brief, editorial_copy, editorial_draft, editorial_design
    from src import editorial_comparison, editorial_details, story_design, editorial_story, carousel_cover
    from src.editorial_art_direction import render_editorial_page
    from src.story_project import _atomic, _fingerprint

    plan = project.plan
    draft = build_editorial_draft(plan.brief, list(project.sources), headline_history=project.headline_history)
    sequence = list(draft['pages'])
    if plan.style == 'detail_study':
        first = next(n for n, page in enumerate(sequence) if page['role'] == 'detail')
        sequence.insert(0, sequence.pop(first))
    identity = dict(plan=plan.model_dump(mode='json'),
                    sources=[dict(artwork=source.artwork.model_dump(mode='json'),
                                  sha256=_fingerprint(Path(source.image_path))) for source in project.sources],
                    code=[_fingerprint(Path(module.__file__)) for module in
                          (editorial_art_direction, editorial_brief, editorial_copy, editorial_draft, editorial_design,
                           editorial_comparison, editorial_details, story_design, editorial_story, carousel_cover)],
                    fonts=[_fingerprint(FONT_DIRECTORY / name) for name in
                           ('source-serif/SourceSerif4-Regular.otf', 'source-sans/SourceSans3-Regular.otf')])
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    (directory / 'pages').mkdir(exist_ok=True)
    slides, rendered = [], 0
    for slide, page in zip(plan.slides, sequence):
        relative = f'pages/{slide.id}-{key[:20]}.jpg'
        path = directory / relative
        metadata_path = path.with_suffix('.json')
        metadata = None
        if path.is_file() and metadata_path.is_file():
            try:
                candidate = json.loads(metadata_path.read_text())
                with Image.open(path) as image:
                    image.verify()
                    if (image.size == (1080, 1350) and image.format == 'JPEG'
                            and candidate.get('sha256') == _fingerprint(path)):
                        metadata = candidate
            except (ValueError, OSError, AttributeError):
                pass  # Invalid cached raster is rebuilt, never accepted during review.
        if metadata is None:
            if verify_only:
                raise ValueError('Editorial review requires a current valid render')
            temporary = path.with_suffix('.render.jpg')
            try:
                render_editorial_page(page, plan.brief, project.sources, plan.style).save(temporary, quality=94, subsampling=0)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            metadata = dict(actual_style=plan.cover_style.value, renderer='artfolio-editorial-story-v2',
                            sha256=_fingerprint(path))
            _atomic(metadata_path, json.dumps(metadata))
            rendered += 1
        slides.append(dict(slide_id=slide.id, role=slide.role, artwork_ids=list(slide.artwork_ids),
                           path=relative, focus=list(slide.focus) if slide.focus else None,
                           focus_basis=slide.focus_basis, content_hash=key, render_metadata=metadata))
    return dict(revision=project.revision, rendered_count=rendered, reused_count=len(slides)-rendered,
                caption=draft['caption'], quality=assess_editorial_story(plan, project.sources).model_dump(mode='json'),
                package=dict(schema_version='artfolio-story-package-v2', artwork_ids=list(plan.artwork_ids),
                             slides=slides, publication_enabled=False,
                             metadata=dict(narrative=plan.narrative, cover_variant=plan.cover_style.value,
                                           headline_kind=plan.headline_kind, theme_id=plan.theme_id,
                                           story_schema=plan.schema_version, renderer_version='artfolio-editorial-story-v2')))
