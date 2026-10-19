"""Summaries of retained metadata observations; never inferred download yields."""
import json
from collections import Counter, defaultdict
from datetime import datetime
from html import escape

from src.insights_storage import parse_aware_timestamp, utc_timestamp
from src.source_health import normalize_source_failure_category
from src.theme_feasibility import ThemeFeasibilityStorageError, validate_state

SOURCE_LABELS = {'aic': 'Art Institute of Chicago', 'met': 'Metropolitan Museum',
                 'cleveland': 'Cleveland Museum', 'rijksmuseum': 'Rijksmuseum',
                 'smithsonian': 'Smithsonian', 'europeana': 'Europeana'}


def build_source_report(state, *, now: datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Source report time requires a timezone')
    try:
        validate_state(state)
    except ThemeFeasibilityStorageError as error:
        raise ValueError('Invalid source evidence') from error
    observations = defaultdict(dict)
    for rows in state['themes'].values():
        for row in rows:
            at = parse_aware_timestamp(row['attempted_at'])
            if at > now:
                continue
            normalized = {**row, 'attempted_at': utc_timestamp(at)}
            observations[(row['theme_id'], at)][json.dumps(normalized, sort_keys=True)] = normalized
    retained = [next(iter(versions.values())) for versions in observations.values() if len(versions) == 1]
    funnel = {name: sum(row[field] for row in retained) for name, field in (
        ('raw', 'raw_count'), ('unique', 'unique_count'), ('rights', 'rights_qualified_count'),
        ('quality', 'quality_qualified_count'), ('relevance', 'relevance_qualified_count'))}
    sources = []
    for source in SOURCE_LABELS:
        active = unavailable = disabled = failed = 0
        categories = Counter()
        for row in retained:
            active += source in row['active_sources']
            unavailable += source in row['unavailable_sources']
            disabled += source in row['disabled_sources']
            failures = {normalize_source_failure_category(f.get('category'))
                        for f in row['adapter_failures'] if f.get('source') == source}
            failed += bool(failures)
            categories.update(failures)
        if active or unavailable or disabled or failed:
            sources.append({'source': source, 'active_attempts': active,
                            'unavailable_attempts': unavailable, 'disabled_attempts': disabled,
                            'failed_attempts': failed, 'failure_categories': dict(sorted(categories.items())),
                            'downloads': None})
    return {'observations': len(retained), 'funnel': funnel, 'sources': sources,
            'last_observation_at': max((row['attempted_at'] for row in retained), default=None),
            'basis': 'Retained theme attempts; candidates can recur across attempts. Metadata gates only.'}


def render_source_report(report: dict | None) -> str:
    if report is None:
        content = '<p>Kaynak gözlemi belirtilmedi; isteğe bağlı --feasibility dosyası kullanılabilir.</p>'
    else:
        funnel = report['funnel']
        content = (f'<p>{report["observations"]} kayıtlı tema denemesi · son gözlem: '
                   f'{escape(str(report["last_observation_at"] or "kullanılamıyor"))}</p>'
                   f'<p>Toplam metadata hunisi: {funnel["raw"]} ham → {funnel["unique"]} benzersiz '
                   f'→ {funnel["rights"]} hak uygun → {funnel["quality"]} kalite uygun '
                   f'→ {funnel["relevance"]} tema uygun.</p>')
        rows = ''.join(
            f'<tr><th>{SOURCE_LABELS[row["source"]]}</th><td>{row["active_attempts"]}</td>'
            f'<td>{row["unavailable_attempts"]}</td><td>{row["disabled_attempts"]}</td>'
            f'<td>{row["failed_attempts"]} · {escape(str(row["failure_categories"]))}</td></tr>'
            for row in report['sources'])
        content += ('<div class="table-scroll" tabindex="0" role="region" aria-label="Müze kaynak gözlemleri">'
                    '<table><thead><tr><th>Kaynak</th><th>Aktif deneme</th><th>Erişilemeyen</th>'
                    '<th>Devre dışı</th><th>Hata gözlenen deneme</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table></div>')
    return ('<section><h2>Müze kaynak gözlemleri</h2>' + content
            + '<p class="muted">Sayılar saklanan tema denemeleridir; aynı eser farklı denemelerde yer alabilir. '
            'Eleme sayıları tüm kaynakların toplamıdır. Müze başına indirme verimi ölçülmemiştir.</p></section>')
