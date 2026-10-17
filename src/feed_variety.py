"""Bounded selection pressure from proven history and explicitly planned inventory."""
import os
from collections.abc import Mapping

from src.artwork_metadata import normalize_artist_identity
from src.artwork_visual_features import DominantColorFamily
from src.content_diversity import _extract_century
from src.insights_storage import parse_aware_timestamp

RECENT_GROUPS = 12
MAX_ADJUSTMENT = 12.0
WEIGHTS = {'artist': 4.0, 'museum': 2.0, 'period': 1.0, 'color': 1.0}
UNKNOWN = {'unknown','unknown artist','unknown museum','unknown period','anonymous','other','n/a','none'}


def variety_enabled() -> bool:
    return os.environ.get('ARTFOLIO_FEED_VARIETY_ENABLED','').strip().lower() == 'true'


def _value(candidate, *names):
    for name in names:
        value = candidate.get(name) if isinstance(candidate, Mapping) else getattr(candidate, name, None)
        if isinstance(value,str) and value.strip():
            return value
    return None


def _known(value):
    if not isinstance(value,str):
        return None
    value = ' '.join(value.casefold().split())
    return value if value and value not in UNKNOWN else None


def _features(candidate, visual_features=None):
    visual = visual_features or (candidate.get('visual_features') if isinstance(candidate, Mapping) else None)
    color = getattr(getattr(visual,'dominant_color_family',None),'value',None)
    color = color or _value(candidate,'visual_color_family','dominant_color','color')
    if color not in {item.value for item in DominantColorFamily if item is not DominantColorFamily.UNKNOWN}:
        color = None
    period = _known(_value(candidate,'period','style_or_period','period_or_style'))
    if period is None:
        period = _known(_extract_century(_value(candidate,'date','creation_date')))
    return {'artist':normalize_artist_identity(_value(candidate,'artist','artist_name')),
            'museum':_known(_value(candidate,'museum','museum_name')),
            'period':period,'color':color}


def variety_adjustment(candidate, context, *, visual_features=None) -> float:
    features = _features(candidate, visual_features)
    groups = {}
    for index,row in enumerate(context):
        if not isinstance(row,Mapping):
            continue
        group = row.get('inventory_group') or row.get('publication_id') or f'legacy:{index}'
        groups.setdefault(group,[]).append(_features(row))
    pressure = 0.0
    for rows in list(groups.values())[-RECENT_GROUPS:]:
        for field,weight in WEIGHTS.items():
            if features[field] is not None and any(row[field] == features[field] for row in rows):
                pressure += weight
    return -min(MAX_ADJUSTMENT,pressure)


def apply_variety(artwork: dict, context) -> dict:
    adjustment = variety_adjustment(artwork,context)
    return {**artwork,'selection_score':float(artwork.get('selection_score') or 0)+adjustment,
            'feed_variety_adjustment':adjustment}


def context_from_content(content, group_id: str) -> tuple[dict,...]:
    return tuple({'id':art['id'], **_features(art), 'inventory_group':group_id}
                 for art in content.artworks)


def context_from_inventory(packages, *, now) -> tuple[dict,...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Inventory context time requires a timezone')
    rows = []
    for package in packages:
        expiry = parse_aware_timestamp(package.get('expires_at'))
        if package.get('state') != 'READY' or expiry is None or expiry <= now:
            continue
        rows.extend({'id':art['id'], **_features(art), 'inventory_group':'active:'+package['id']}
                    for art in package['content']['artworks'])
    return tuple(rows)
