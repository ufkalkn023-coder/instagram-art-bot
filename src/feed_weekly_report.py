"""Weekly stored-evidence report; no collection, notification or publication."""
import html
from collections import Counter
from datetime import datetime, timedelta, timezone

from src.feed_analytics import build_feed_analytics_report
from src.feed_operations import build_feed_operations
from src.feed_experiment_report import build_experiment_report
from src.insights_storage import parse_aware_timestamp, utc_timestamp


def build_weekly_report(schedule, queue, history, snapshots, *, now: datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Weekly report time must include a timezone')
    timestamp = now.astimezone(timezone.utc)
    start = timestamp - timedelta(days=7)
    # Validate identities across all history before selecting the weekly interval.
    complete = build_feed_analytics_report(history, snapshots, now=timestamp)
    selected = {row['publication_id'] for row in complete['windows']
                if start <= parse_aware_timestamp(row['posted_at']) < timestamp}
    publications = [row for row in history['publications']
                    if isinstance(row, dict) and row.get('id') in selected]
    selected_snapshots = [row for row in snapshots if isinstance(row, dict)
                          and row.get('publication_id') in selected]
    analytics = build_feed_analytics_report({'publications': publications}, selected_snapshots, now=timestamp)
    valid_parents = {row['publication_id']: row['publication_format'] for row in analytics['windows']}
    counts = Counter(valid_parents.values())
    # Experiment arms need observations across weeks at the normal Feed cadence.
    experiments = build_experiment_report(history, snapshots, now=timestamp)
    experiments['period'] = {
        'basis': 'All validated stored publications before report generation',
        'end': utc_timestamp(timestamp),
    }
    return {'schema_version': 1, 'generated_at': utc_timestamp(timestamp),
            'period': {'start': utc_timestamp(start), 'end': utc_timestamp(timestamp),
                       'basis': 'Published in [start, end); stored measurements available at generation time'},
            'publication_counts': {name: counts[name] for name in ('single', 'carousel')},
            'analytics': analytics, 'source_diagnostics': complete['diagnostics'],
            'experiments': experiments,
            'operations': build_feed_operations(schedule, queue, history, snapshots, now=timestamp),
            'winner': None, 'auto_promote': False}


def render_weekly_markdown(report: dict) -> str:
    analytics, operations = report['analytics'], report['operations']
    summary = analytics['summary']
    lines = ['# Haftalık Feed raporu', '',
             f"Dönem (UTC): {report['period']['start']} ≤ yayın < {report['period']['end']}",
             f"Rapor zamanı: {report['generated_at']}", '',
             f"Yayın: **{summary['feed_publications']}** · Tekli: {report['publication_counts']['single']} · Carousel: {report['publication_counts']['carousel']}",
             'Her parent gönderi bir yayındır; sayfalar veya ölçüm saatleri yayın sayısını artırmaz.', '',
             '## Ölçüm kapsamı', '',
             f"Tamamlanan: {summary['complete_windows']} · Kısmi: {summary['partial_windows']} · Kaçırılan: {summary['missed_windows']} · Vadesi gelen: {summary['due_windows']} · Bekleyen: {summary['pending_windows']}",
             'Kaçırılan pencere eksik ölçümdür, kaçırılmış yayın değildir. Yeni gönderilerin ileriki ölçüm saatleri henüz beklemede olabilir.', '',
             '## Aynı hedef yaştaki ölçümler', '',
             '| Format | Hedef yaş | Kullanılabilir / uygun | Ortalama erişim | Kaydetme oranı / gözlem |',
             '| --- | --- | --- | --- | --- |']
    for cohort in analytics['cohorts']:
        if not cohort['eligible_publications']:
            continue
        rate = cohort['rates']['save_rate']
        save = 'kullanılamıyor' if rate['mean_rate'] is None else f"{rate['mean_rate']:.1%}"
        reach = 'kullanılamıyor' if cohort['reach']['mean'] is None else f"{cohort['reach']['mean']:.1f}"
        lines.append(f"| {cohort['publication_format']} | {cohort['target_age_hours']} saat | {cohort['usable_publications']} / {cohort['eligible_publications']} | {reach} | {save} / {rate['observations']} |")
    lines += ['', 'Eksik metrik sıfır değildir. Karşılaştırmalar betimseldir; minimum 5 gözlem gerekir. Otomatik kazanan seçilmez.', '',
              '## Sistem ve içerik stoğu', '',
              f"Zamanlayıcı: {operations['schedule'].get('status', 'UNKNOWN')}",
              f"Hazır: {operations['queue']['ready']} · Süresi dolmuş: {operations['queue']['expired']} · Sahiplenilmiş: {operations['queue']['claimed']} · Karantina: {operations['queue']['quarantined']}",
              f"Ölçüm güncelliği: {operations['analytics']['freshness']}",
              'Kuyruk sayıları manifest durumudur; gerçek tüketimde haklar, görseller ve geçmiş yeniden doğrulanır.', '',
              'Rapor yalnız kayıtlı verileri okur; yayın veya yeni ölçüm başlatmaz.']
    experiments = report.get('experiments', {})
    comparisons = [*experiments.get('comparisons', []), *experiments.get('cover_comparisons', [])]
    lines += ['', '## Deney gözlemleri · birikimli', '',
              'Deneyler yalnız bu haftayı değil, rapor zamanına kadar doğrulanmış tüm kayıtlı yayınları kapsar.', '',
              'Kapak ve açıklama ayrı deneylerdir. Aynı tema, hedef yaş ve sabit karşı faktör korunur; her kolda minimum 5 gözlem gerekir. Otomatik kazanan seçilmez.']
    if not comparisons:
        lines += ['', 'Henüz kayıtlı karşılaştırılabilir deney ataması yok.']
    else:
        lines += ['', '| Değişken | Tema | Hedef yaş | Kol | Kaydetme oranı / gözlem |',
                  '| --- | --- | --- | --- | --- |']
        for comparison in comparisons:
            theme = html.escape(comparison['theme_id']).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
            for arm, rates in comparison['arms'].items():
                rate = rates['save_rate']
                value = 'kullanılamıyor' if rate['mean_rate'] is None else f"{rate['mean_rate']:.1%}"
                lines.append(f"| {comparison['variable']} | {theme} | {comparison['target_age_hours']} saat | {arm} | {value} / {rate['observations']} |")
    return '\n'.join(lines) + '\n'
