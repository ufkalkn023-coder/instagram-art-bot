"""Bounded metadata-based alternatives for human story review; no model calls."""
from itertools import combinations

from src.artwork_metadata import normalize_artist_identity
from src.editorial_v2 import SourceArtwork
from src.story_plan import build_story_plan
from src.story_quality import assess_story


def suggest_stories(sources, *, maximum: int = 3, headline_history=None) -> list[dict]:
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 6:
        raise ValueError('Suggest between one and six stories')
    if (not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= 50
            or any(not isinstance(source, SourceArtwork) for source in sources)):
        raise ValueError('Use one to fifty validated local sources')
    sources = sorted((SourceArtwork.model_validate(s.model_dump()) for s in sources),
                     key=lambda s: s.artwork.canonical_id)
    if len({s.artwork.canonical_id for s in sources}) != len(sources):
        raise ValueError('Source identities must be unique')
    if headline_history is not None and not isinstance(headline_history, (list, tuple)):
        raise ValueError('Headline history must be an array')
    titles = [] if headline_history is None else list(headline_history)
    if len(titles) > 100 or any(not isinstance(title, str) for title in titles):
        raise ValueError('Headline history must contain at most one hundred strings')
    candidates = [('single_study', sources[:1], 'Recorded source title')]
    if len(sources) >= 2:
        def pair_key(pair):
            left, right = pair
            artist = normalize_artist_identity(left.artwork.artist_name)
            shared_artist = artist is not None and artist == normalize_artist_identity(right.artwork.artist_name)
            museum = (left.artwork.museum_name or '').strip().casefold()
            shared_museum = bool(museum) and museum == (right.artwork.museum_name or '').strip().casefold()
            return (-int(shared_artist), -int(shared_museum), left.artwork.canonical_id, right.artwork.canonical_id)

        pair = min(combinations(sources, 2), key=pair_key)
        reason = 'Shared recorded artist' if pair_key(pair)[0] == -1 else 'Two distinct rights-cleared sources'
        candidates.append(('comparison', list(pair), reason))
    if len(sources) >= 3:
        candidates.append(('thematic_selection', sources[:5], 'Source metadata collection; no shared subject claim'))
    candidates.extend(('single_study', [source], 'Recorded source title') for source in sources[1:7])
    proposals = []
    for narrative, selected, basis in candidates:
        plan = build_story_plan(selected, narrative)
        quality = assess_story(plan, selected, headline_history=titles[-100:])
        if not quality.can_render or len(plan.slides) > 10:
            continue
        proposals.append({'id': f'story-{len(proposals) + 1:02d}', 'basis': basis,
                          'manual_review_required': True, 'publication_ready': False,
                          'plan': plan.model_dump(mode='json'), 'quality': quality.model_dump(mode='json')})
        titles.append(plan.public_title)
        if len(proposals) == maximum:
            break
    return proposals
