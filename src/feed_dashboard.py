"""Accessible static snapshot rendering for the read-only Feed dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from src.feed_analytics import build_feed_analytics_report
from src.feed_operations import build_feed_operations
from src.insights_storage import parse_aware_timestamp


def render_feed_dashboard(schedule, queue, history, snapshots, *, now: datetime,
                          stories=()) -> str:
    """Render a self-contained Turkish HTML snapshot from already loaded data."""
    local_now = now.astimezone(ZoneInfo("Europe/Istanbul"))
    operations = build_feed_operations(schedule, queue, history, snapshots, now=now)
    analytics = build_feed_analytics_report(history, snapshots, now=now)
    def esc(value):
        return html.escape(str(value), quote=True)

    def shown(value):
        return esc(value) if value not in (None, "") else "kullanılamıyor"

    def local_timestamp(value):
        parsed = parse_aware_timestamp(value)
        return parsed.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S %Z") if parsed else None

    next_at = local_timestamp(schedule.get("next_eligible_at"))
    attempt_at = local_timestamp(schedule.get("next_attempt_at") or schedule.get("next_check_at"))
    last_at = local_timestamp(schedule.get("last_successful_feed_at"))
    format_label = {"single": "Tek eser", "carousel": "Carousel", "reel": "Reel"}.get(
        schedule.get("next_format"), schedule.get("next_format")
    )
    queue_state_label = {"READY": "Hazır", "CLAIMED": "Sahiplenildi",
                         "CONSUMED": "Tüketildi", "QUARANTINED": "Karantinada"}
    queue_format_label = {"single": "Tek eser", "carousel": "Carousel"}
    cadence_label = {
        "hourly_utc17": "Her saatin 17. dakikasında kontrol edilir",
        "daily_utc1717": "Her gün Türkiye saatiyle 20:17'de kontrol edilir",
    }.get(schedule.get("cadence"), "Kontrol sıklığı belirtilmedi")
    queue_rows = []
    for row in queue:
        queue_rows.append(
            "<tr>"
            f"<td>{shown(row.get('title'))}<br><small>{shown(row.get('id') or row.get('package_id'))}</small></td>"
            f"<td>{shown(queue_state_label.get(row.get('state'), row.get('state')))}</td>"
            f"<td>{shown(queue_format_label.get(row.get('publication_format'), row.get('publication_format')))}</td>"
            f"<td>{shown(local_timestamp(row.get('expires_at')))}</td><td>{shown(row.get('source_count'))}</td>"
            f"<td>{shown(row.get('page_count'))}</td>"
            "</tr>"
        )
    if not queue_rows:
        queue_rows.append('<tr><td colspan="6">Kuyruk kaydı yok</td></tr>')

    metric_names = (
        ("feed_publications", "Yayın sayısı"), ("complete_windows", "Tamamlanan ölçüm pencereleri"),
        ("partial_windows", "Kısmi ölçüm pencereleri"), ("unavailable_windows", "Kullanılamayan pencereler"),
        ("missed_windows", "Kaçırılan pencereler"), ("due_windows", "Vadesi gelen pencereler"),
        ("pending_windows", "Bekleyen pencereler"),
    )
    summary = operations["analytics"]
    metrics = "".join(
        f"<div class=\"metric\"><dt>{esc(label)}</dt><dd>{shown(summary.get(key))}</dd></div>"
        for key, label in metric_names
    )
    def number(value, *, percent=False):
        if value is None:
            return "kullanılamıyor"
        rendered = f"{value * 100:.1f}%" if percent else f"{value:g}"
        return rendered

    cohort_rows = []
    minimum_by_target = {item["target_age_hours"]: item["minimum_cohort_size"]
                         for item in analytics["comparisons"]}
    for cohort in analytics["cohorts"]:
        if not cohort["usable_publications"] and not cohort["eligible_publications"]:
            continue
        rates = cohort["rates"]
        cohort_rows.append(
            f"<tr><th>{esc(cohort['publication_format'])} · {cohort['target_age_hours']} saat</th>"
            f"<td>{cohort['usable_publications']} kullanılabilir / {cohort['eligible_publications']} uygun yayın; "
            f"minimum örneklem {minimum_by_target[cohort['target_age_hours']]} "
            f"({'karşılandı' if cohort['usable_publications'] >= minimum_by_target[cohort['target_age_hours']] else 'karşılanmadı'})</td>"
            f"<td>{number(cohort['reach']['mean'])} · {cohort['reach']['observations']} gözlem</td>"
            f"<td>{number(rates['save_rate']['mean_rate'], percent=True)} · {rates['save_rate']['observations']} gözlem</td>"
            f"<td>{number(rates['like_rate']['mean_rate'], percent=True)} · {rates['like_rate']['observations']} gözlem</td></tr>"
        )
    cohort_html = "".join(cohort_rows) or '<tr><td colspan="5">Henüz değerlendirilebilir yayın yok.</td></tr>'
    story_html = "".join(
        f"<li>{esc(story['title'])}: yerel editoryal onay geçerli ve içerik doğrulandı, {esc(story['source_count'])} kaynak, "
        f"{esc(story['page_count'])} sayfa</li>" if story.get("approved") else
        f"<li>{esc(story['title'])}: onaylı değil — {esc(story.get('error', 'kontrol başarısız'))}</li>"
        for story in stories
    ) or "<li>Yerel hikâye projesi belirtilmedi.</li>"
    status_labels = {
        "READY": "Yayın için uygun",
        "WAITING_COOLDOWN": "Yayın aralığının dolması bekleniyor",
        "WAITING_FAILURE_BACKOFF": "Başarısız deneme sonrası bekleniyor",
        "WAITING_WINDOW": "Yayın penceresi bekleniyor",
        "PAUSED": "Yayın takibi duraklatıldı",
        "BLOCKED": "Yayın için kontrol gerekiyor",
        "SHA_MISMATCH": "Üretim sürümünün onayı gerekiyor",
    }
    status = schedule.get("status")
    schedule_text = shown(status_labels.get(status, status))
    schedule_detail = f"{shown(status)} · {shown(schedule.get('reason'))}"
    freshness = {"fresh": "Güncel", "stale": "Eski", "missing": "Ölçüm bulunamadı",
                 "cold_start": "İlk ölçüm bekleniyor"}.get(summary.get("freshness"), summary.get("freshness"))
    return f'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARTFOLIO · Feed Durumu</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eeeae1;color:#24271f;font:16px/1.55 system-ui,sans-serif}}
header{{background:#f9f6ee;border-bottom:1px solid #ccc6b7;padding:22px max(5vw,20px);display:flex;justify-content:space-between;gap:16px;align-items:center}}
header strong{{font:24px Georgia,serif;letter-spacing:3px}}main{{max-width:1100px;margin:auto;padding:28px 22px}}h1,h2{{font-family:Georgia,serif}}h1{{font-size:clamp(30px,5vw,46px);margin:10px 0}}h2{{font-size:25px;margin:0 0 12px}}section,.notice{{background:#f9f6ee;border:1px solid #d3ccbd;border-radius:9px;padding:20px;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.eyebrow,dt{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#62695c}}dd{{margin:4px 0 0;font-size:18px;font-weight:600;overflow-wrap:anywhere}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:16px}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #d3ccbd}}th{{font-size:12px;color:#62695c}}.muted{{color:#62695c}}.table-scroll{{overflow-x:auto}}.table-scroll:focus{{outline:2px solid #294735;outline-offset:4px}}a{{color:#294735}}@media(max-width:620px){{main{{padding:18px 14px}}section{{padding:16px}}.grid{{grid-template-columns:1fr}}header{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><header><strong>ARTFOLIO</strong><span>Feed · salt okunur durum anlık görüntüsü</span></header><main>
<p class="eyebrow">Rapor oluşturuldu · {esc(local_now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p><h1>Yayın akışı durumu</h1>
<p class="notice">Bu sayfa üretildiği andaki bir anlık görüntüsüdür. Sonraki uygunluk zamanı yayın garantisi değildir.</p>
<section><h2>Planlama</h2><div class="grid"><dl><dt>Durum</dt><dd title="{schedule_detail}">{schedule_text}</dd></dl><dl><dt>Sıradaki format</dt><dd>{shown(format_label)}<br><small>{esc(cadence_label)}</small></dd></dl><dl><dt>En erken uygunluk zamanı</dt><dd>{shown(next_at)} · Europe/Istanbul</dd></dl><dl><dt>Deneme için alt sınır</dt><dd>{shown(attempt_at)} · Europe/Istanbul</dd></dl><dl><dt>Gerçek son başarılı yayın</dt><dd>{shown(last_at)} · Europe/Istanbul</dd></dl></div></section>
<section><h2>Hazır içerik kuyruğu</h2><p class="muted">Sayım manifest durumuna dayanır; tüketim sırasında içerik, haklar ve geçmiş yeniden doğrulanır.</p><div class="metrics">{''.join(f'<dl class="metric"><dt>{esc({"ready":"Hazır", "next_format_ready":"Sıradaki formata hazır", "expired":"Süresi dolmuş", "invalid_age":"Yaşı geçersiz", "claimed":"Sahiplenilmiş", "consumed":"Tüketilmiş", "quarantined":"Karantinada"}.get(k, k))}</dt><dd>{esc(v)}</dd></dl>' for k,v in operations['queue'].items())}</div>
<div class="table-scroll" tabindex="0" role="region" aria-label="Kaydırılabilir veri tablosu"><table><thead><tr><th>Paket</th><th>Durum</th><th>Format</th><th>Son kullanma</th><th>Kaynak</th><th>Sayfa</th></tr></thead><tbody>{''.join(queue_rows)}</tbody></table></div></section>
<section><h2>Yerel hikâye onayı</h2><ul>{story_html}</ul></section>
<section><h2>Insights ölçüm pencereleri</h2><p class="muted">Kaçırılan ölçüm pencereleri, eksik performans verisini gösterir; kaçırılmış yayın anlamına gelmez.</p><dl class="metrics">{metrics}</dl><p class="muted">Ölçüm güncelliği: {shown(freshness)}; son karşılaştırılabilir kayıt: {shown(local_timestamp(summary.get('last_comparable_capture_at')))}.</p><h3>Format · hedef yaş grupları</h3><p class="muted">Erişim ortalaması ve kaydetme/beğeni oranları yalnızca mevcut gözlemleri özetler. Küçük örneklemler betimseldir.</p><div class="table-scroll" tabindex="0" role="region" aria-label="Kaydırılabilir veri tablosu"><table><thead><tr><th>Grup</th><th>Örneklem</th><th>Ortalama erişim</th><th>Kaydetme oranı</th><th>Beğeni oranı</th></tr></thead><tbody>{cohort_html}</tbody></table></div><p class="muted">Ölçümler mevcut yayınları özetler; hangi formatın daha iyi olduğu sonucunu çıkarmak için yeterli veri gerekir.</p></section>
</main></body></html>'''
