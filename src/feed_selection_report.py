"""Explain recorded selection evidence without reconstructing absent scores."""
from collections.abc import Mapping
from datetime import datetime
from html import escape
from math import isfinite

from pydantic import ValidationError

from src.feed_analytics import build_feed_analytics_report
from src.insights_storage import parse_aware_timestamp
from src.models import PublicationRecord

COMPONENT_LABELS = {
    'quality_component': 'Kalite / editoryal katkı',
    'engagement_component': 'Öğrenilmiş etkileşim katkısı',
    'engagement_confidence': 'Öğrenme güveni',
    'diversity_component': 'Çeşitlilik katkısı',
    'exploration_component': 'Keşif katkısı',
    'learned_score': 'Öğrenilmiş puan',
}
MAX_SELECTION_ROWS = 24


def selection_metadata(value) -> dict:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key in COMPONENT_LABELS:
        number = value.get(key)
        lower, upper = (-100, 100) if key == 'diversity_component' else (0, 100)
        if key == 'engagement_confidence':
            upper = 1
        if (not isinstance(number, bool) and isinstance(number, (int, float))
                and isfinite(number) and lower <= number <= upper):
            result[key] = number
    for key in ('carousel_theme', 'cover_variant', 'caption_hook_type'):
        text = value.get(key)
        if isinstance(text, str) and 0 < len(text) <= 150:
            result[key] = text
    return result


def build_selection_report(history, queue, *, now: datetime) -> list[dict]:
    validated = build_feed_analytics_report(history, [], now=now)
    identities = {row['publication_id']: row for row in validated['windows']}
    rows = []
    for pub in history.get('publications', []):
        try:
            parsed = PublicationRecord.model_validate(pub)
        except ValidationError:
            continue
        identity = identities.get(parsed.id)
        if (identity is not None and parsed.media_id == identity['media_id']
                and parse_aware_timestamp(parsed.posted_at) == parse_aware_timestamp(identity['posted_at'])):
            rows.append({'id': parsed.id, 'title': parsed.carousel_theme or parsed.id,
                         'origin': 'published', 'components': selection_metadata(pub),
                         'posted_at': identity['posted_at']})
    rows.sort(key=lambda row: (row['posted_at'], row['id']))
    rows = rows[-MAX_SELECTION_ROWS:]
    for package in queue[:MAX_SELECTION_ROWS]:
        expiry = parse_aware_timestamp(package.get('expires_at'))
        if package.get('state') != 'READY' or expiry is None or expiry <= now:
            continue
        rows.append({'id': package.get('id'), 'title': package.get('title') or package.get('id'),
                     'origin': 'prepared', 'components': selection_metadata(package.get('selection_metadata'))})
    return rows[-MAX_SELECTION_ROWS:]


def render_selection_report(rows: list[dict]) -> str:
    cards = []
    for row in rows:
        components = row['components']
        scores = []
        for key, label in COMPONENT_LABELS.items():
            value = components.get(key)
            shown = 'kullanılamıyor' if value is None else (
                f'{value:.0%}' if key == 'engagement_confidence' else f'{value:g}')
            scores.append(f'<div><dt>{label}</dt><dd>{shown}</dd></div>')
        context = ' · '.join(str(components[key]) for key in
                             ('carousel_theme', 'cover_variant', 'caption_hook_type') if key in components)
        origin = 'Hazır paket' if row['origin'] == 'prepared' else 'Yayın kaydı'
        cards.append(f'<article><h3>{escape(str(row["title"]))}</h3><p>{origin} · {escape(context)}</p>'
                     f'<dl class="metrics">{"".join(scores)}</dl></article>')
    return ('<section><h2>Bu içerik neden seçildi?</h2>'
            '<p class="muted">Yalnız kayıtlı puan katkıları gösterilir; eksik puanlar hesaplanmaz. '
            'Hazır içerik yayın başarısı değildir; puanlar performans garantisi vermez.</p>'
            + (''.join(cards) or '<p>Henüz seçim kaydı yok.</p>') + '</section>')
