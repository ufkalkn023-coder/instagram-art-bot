"""Offline editorial ideas; not prepared inventory or production authorization."""
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from src.carousel_themes import ThemeEvidenceMode, get_default_theme_registry, plan_carousel_theme
from src.feed_analytics import build_feed_analytics_report
from src.insights_storage import parse_aware_timestamp


def build_editorial_horizon(history, *, now: datetime, weeks: int = 2, seed: str = 'editorial-v1') -> dict:
    if isinstance(weeks, bool) or not isinstance(weeks, int) or weeks not in (2, 4):
        raise ValueError('Editorial horizon must be two or four weeks')
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(seed, str) or not 1 <= len(seed) <= 200):
        raise ValueError('Use an aware time and a bounded seed')
    now = now.astimezone(timezone.utc)
    analytics = build_feed_analytics_report(history, [], now=now)
    if any(analytics['diagnostics'].get(key, 0) for key in
           ('invalid_publications', 'conflicting_publications', 'future_publications')):
        raise ValueError('Resolve ambiguous publication history before planning')
    parents = {row['publication_id']: row for row in analytics['windows']}
    latest = max(parents.values(), key=lambda row: row['posted_at'], default=None)
    candidate = max(now, parse_aware_timestamp(latest['posted_at']) + timedelta(hours=48)) if latest else now
    next_format = 'single' if latest and latest['publication_format'] == 'carousel' else 'carousel'
    registry = get_default_theme_registry()
    editorial_context = []
    for pub in history.get('publications', []):
        if isinstance(pub, dict) and pub.get('id') in parents and pub.get('type') == 'carousel':
            editorial_context.append({'theme_id': pub.get('carousel_theme'),
                                      'theme_family': pub.get('theme_family'),
                                      'carousel_format': pub.get('carousel_format')})
    slots = []
    current_theme = None
    end = now + timedelta(weeks=weeks)
    while candidate < end:
        if next_format == 'carousel':
            current_theme = plan_carousel_theme(
                registry, editorial_context, run_seed=f'{seed}:{len(slots)}',
                current_month=candidate.month, eligible_evidence_modes=(ThemeEvidenceMode.METADATA,)).theme
            editorial_context.append({'theme_id': current_theme.id, 'theme_family': current_theme.family.value,
                                      'carousel_format': current_theme.format.value})
        slots.append({'earliest_at': candidate.isoformat(), 'publication_format': next_format,
                      'theme_id': current_theme.id if current_theme else None,
                      'title': current_theme.title if current_theme else 'Tek eser incelemesi',
                      'editorial_format': current_theme.format.value if current_theme and next_format == 'carousel' else None,
                      'provisional': True, 'publication_ready': False})
        next_format = 'single' if next_format == 'carousel' else 'carousel'
        candidate += timedelta(hours=48)
    return {'schema_version': 'artfolio-editorial-horizon-v1', 'generated_at': now.isoformat(),
            'weeks': weeks, 'seed': seed, 'prepared_packages': 0, 'publication_ready': False,
            'slots': slots, 'limitations': 'Provisional ideas only; not queue order, acquired artworks or authorization.'}


def render_editorial_horizon(plan: dict) -> str:
    rows = []
    for slot in plan['slots']:
        local = parse_aware_timestamp(slot['earliest_at']).astimezone(ZoneInfo('Europe/Istanbul'))
        rows.append(f'<tr><td>{local:%Y-%m-%d %H:%M} · Türkiye</td>'
                    f'<td>{escape(slot["publication_format"])}</td><td>{escape(slot["title"])}</td>'
                    f'<td>{escape(slot["editorial_format"] or "Tek eser")}</td></tr>')
    return ('<!doctype html><html lang="tr"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Editoryal plan</title>'
            '<style>body{font:16px/1.6 system-ui;margin:2rem auto;padding:0 1rem;max-width:1100px;'
            'background:#f9f6ee;color:#24271f}table{border-collapse:collapse;width:100%}'
            'td,th{text-align:left;padding:1rem;border-bottom:1px solid #ccc}.scroll{overflow:auto}</style>'
            f'<h1>{plan["weeks"]} haftalık Editoryal plan</h1>'
            '<p>Bu tarihler önceki yayınların başarısına bağlı olası alt sınırlardır; yayın garantisi değildir. '
            'Bu fikir listesi hazır kuyruk değildir ve mevcut kuyruk önceliğini hesaplamaz. '
            'Eser, hak, görsel ve editoryal onay kontrolleri henüz yapılmamıştır.</p>'
            '<div class="scroll" tabindex="0" role="region" aria-label="Editoryal fikir takvimi">'
            '<table><thead><tr><th>En erken olası</th><th>Yayın türü</th><th>Tema fikri</th>'
            f'<th>Editoryal biçim</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></html>')
