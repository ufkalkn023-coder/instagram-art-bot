"""Render freshly acquired museum works with the existing editorial art direction.

Delivery v3 records automated validation, never a human project approval. The
legacy-named reviewed_* fields bind the validated generation and its exact bytes.
"""
import hashlib
import json
from pathlib import Path

from src.carousel_themes import get_default_theme_registry
from src.automatic_editorial_copy import PROTOCOL, generate_automatic_brief
from src.editorial_copy import source_credit
from src import gemini_ai
from src.editorial_art_direction import render_editorial_page
from src.editorial_draft import build_editorial_draft
from src.editorial_v2 import SourceArtwork
from src.feed_content import PreparedFeedContent, PreparedStoryContent, artwork_digest
from src.feed_queue import ARTWORK_FIELDS
from src.instagram_image import inspect_instagram_image_publishability
from src.models import NormalizedArtwork, require_canonical_artwork_id
from src.quality_filter import validate_local_image_file
from src.story_delivery import StoryDelivery, StoryPage


def _source(artwork: dict, snapshot: Path) -> SourceArtwork:
    identity = require_canonical_artwork_id(artwork['id'])
    source, source_id = identity.split('_', 1)
    if artwork.get('source', source) != source:
        raise ValueError('Acquired artwork identity and source disagree')
    values = {name: artwork[name] for name in NormalizedArtwork.model_fields if name in artwork}
    values.update(source=source, source_id=source_id, title=artwork['title'],
                  artist_name=artwork.get('artist') or artwork.get('artist_name') or 'Unknown Artist',
                  creation_date=artwork.get('date') or artwork.get('creation_date'),
                  museum_name=artwork.get('museum') or artwork.get('museum_name'))
    normalized = NormalizedArtwork.model_validate(values)
    result = SourceArtwork(artwork=normalized, image_path=str(snapshot))
    # Snapshot once: concurrent acquisition cannot change render/hash provenance.
    with Path(artwork['local_image_path']).open('rb') as stream:
        data = stream.read(30_000_001)
    if len(data) > 30_000_000:
        raise ValueError('Automatic editorial source exceeds its byte bound')
    with snapshot.open('xb') as stream:
        stream.write(data)
    if not validate_local_image_file(str(snapshot)).valid:
        raise ValueError('Automatic editorial requires a bounded validated source image')
    return result


def prepare_automatic_editorial(acquired: PreparedFeedContent, output: Path) -> PreparedStoryContent:
    # A legacy carousel's separate cover is presentation, not another featured work.
    artworks = acquired.artworks if acquired.publication_format == 'single' else acquired.artworks[1:]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    source_directory = output / 'sources'
    source_directory.mkdir()
    sources = [_source(art, source_directory / f'source-{index + 1:02d}.jpg')
               for index, art in enumerate(artworks)]
    identities = tuple(source.artwork.canonical_id for source in sources)
    if len(set(identities)) != len(identities):
        raise ValueError('Automatic editorial sources must be unique')
    single = acquired.publication_format == 'single'
    theme = next((theme for theme in get_default_theme_registry().enabled_themes
                  if theme.id == acquired.theme_id), None)
    brief = generate_automatic_brief(sources, theme_title=theme.title if theme else ('Single artwork' if single else 'Museum collection'))
    draft = build_editorial_draft(brief, sources)
    for index, page in enumerate(draft['pages']):
        note = next(note for note in brief.notes if note.artwork_id == page['artwork_id'])
        page.update(title=brief.title if index == 0 else note.heading,
                    text=brief.lead if index == 0 else note.text)
    # Bind the durable metadata projection, excluding acquisition-only local paths.
    # Queue persistence uses this same projection before validating delivery hashes.
    artworks = tuple({key: value for key, value in art.items() if key in ARTWORK_FIELDS}
                     for art in artworks)
    paths, pages = [], []
    for index, page in enumerate(draft['pages']):
        path = output / f'editorial-{index + 1:02d}.jpg'
        # Callers own a new staging directory; never overwrite an existing render.
        with path.open('xb') as stream:
            render_editorial_page(page, brief, sources, 'journal').save(stream, format='JPEG', quality=94, subsampling=0)
        if not inspect_instagram_image_publishability(str(path)).publishable:
            raise ValueError('Automatic editorial render is not Instagram compatible')
        paths.append(str(path))
        pages.append(StoryPage(id=f'artwork-{index + 1}', role='artwork',
                               artwork_ids=(page['artwork_id'],), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    sections = [brief.title, brief.lead]
    if single:
        sections.append(brief.notes[0].text)
    sections.extend(source_credit(source) for source in sources)
    caption = '\n\n'.join(sections)
    if len(caption) > 2200:
        raise ValueError('Automatic editorial caption exceeds Instagram capacity')
    identity = dict(brief=brief.model_dump(mode='json'),
                    generation=dict(model=gemini_ai.config.GEMINI_MODEL, protocol=PROTOCOL),
                    sources={source.artwork.canonical_id: hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
                             for source in sources}, pages=[page.model_dump(mode='json') for page in pages],
                    caption_sha256=hashlib.sha256(caption.encode()).hexdigest())
    delivery = StoryDelivery(schema_version='artfolio-story-delivery-v3', reviewed_revision=1,
                             reviewed_digest=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
                             source_ids=identities, source_sha256=identity['sources'],
                             artwork_sha256={art['id']: artwork_digest(art) for art in artworks},
                             caption_sha256=identity['caption_sha256'], public_title=brief.title,
                             theme_id=acquired.theme_id or 'single_artwork',
                             theme_title=theme.title if theme else brief.title, narrative=brief.narrative,
                             cover_variant='museum_journal', headline_kind='ai_proposal',
                             pages=tuple(pages))
    return PreparedStoryContent(acquired.publication_format, artworks, tuple(paths), caption,
                                alt_text=acquired.alt_text, publication_metadata={'story_delivery': delivery.model_dump(mode='json')},
                                theme_id=delivery.theme_id, theme_family=acquired.theme_family,
                                carousel_format=acquired.carousel_format, story_delivery=delivery)
