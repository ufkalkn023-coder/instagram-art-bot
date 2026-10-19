"""Accessible static snapshot rendering for the read-only Feed dashboard."""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from src.feed_analytics import build_feed_analytics_report
from src.feed_calendar import build_feed_calendar
from src.feed_operations import build_feed_operations
from src.feed_selection_report import build_selection_report, render_selection_report
from src.feed_source_report import build_source_report, render_source_report
from src.feed_experiment_report import build_experiment_report, render_experiment_progress
from src.insights_storage import parse_aware_timestamp


def render_feed_dashboard(schedule, queue, history, snapshots, *, now: datetime,
                          stories=(), feasibility=None) -> str:
    """Render a self-contained Turkish HTML snapshot from already loaded data."""
    local_now = now.astimezone(ZoneInfo("Europe/Istanbul"))
    operations = build_feed_operations(schedule, queue, history, snapshots, now=now)
    analytics = build_feed_analytics_report(history, snapshots, now=now)
    selection_html = render_selection_report(build_selection_report(history, queue, now=now))
    source_html = render_source_report(build_source_report(feasibility, now=now) if feasibility is not None else None)
    experiment_html = render_experiment_progress(build_experiment_report(history, snapshots, now=now))
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
    calendar_cards = []
    for row in build_feed_calendar(schedule, queue, now=now):
        thumbnail = row.get("thumbnail")
        if (isinstance(thumbnail, str) and len(thumbnail) <= 180_000
                and re.fullmatch(r"data:image/jpeg;base64,[A-Za-z0-9+/=]+", thumbnail)):
            picture = f'<img src="{esc(thumbnail)}" alt="{esc(row.get("title") or "Paket önizlemesi")}" loading="lazy">'
        else:
            picture = '<div class="preview-empty">Önizleme yok</div>'
        earliest = local_timestamp(row.get("earliest_at"))
        date = f"En erken olası · {earliest}" if earliest else "Tarih atanmadı"
        label = queue_state_label.get(row.get("state"), row.get("state"))
        calendar_cards.append(
            f'<article class="calendar-card">{picture}<div><p class="muted">{shown(label)} · '
            f'{shown(queue_format_label.get(row.get("publication_format"), row.get("publication_format")))}</p>'
            f'<h3>{shown(row.get("title"))}</h3><p>{esc(date)}</p>'
            f'<p class="muted">{esc(row["planning_note"])}</p>'
            f'<p>{shown(row.get("source_count"))} kaynak · {shown(row.get("page_count"))} sayfa</p>'
            f'<small>Son kullanma: {shown(local_timestamp(row.get("expires_at")))}</small></div></article>'
        )
    calendar_html = "".join(calendar_cards) or '<p>Planlanacak paket bulunamadı.</p>'
    story_labels = {"single_study": "Tek eser incelemesi", "comparison": "Karşılaştırma",
                    "thematic_selection": "Eser seçkisi", "museum_journal": "Museum Journal",
                    "artwork_first": "Artwork First", "detail_study": "Detail Study"}
    story_rows = []
    for cohort in analytics["story_cohorts"]:
        if cohort["dimension"] not in {"narrative", "cover_variant"} or not cohort["eligible_publications"]:
            continue
        rates = cohort["rates"]
        evidence = "Betimsel ölçümler" if cohort["minimum_cohort_met"] else "Örneklem yetersiz"
        story_rows.append(
            f'<tr><th>{esc(story_labels.get(cohort["value"], cohort["value"]))} · {cohort["target_age_hours"]} saat</th>'
            f'<td>{cohort["usable_publications"]} kullanılabilir / {cohort["eligible_publications"]} uygun yayın'
            f'<br><small>{esc(evidence)} · minimum {cohort["minimum_cohort_size"]}</small></td>'
            f'<td>{number(cohort["coverage"], percent=True)}</td>'
            f'<td>{number(rates["save_rate"]["mean_rate"], percent=True)} · {rates["save_rate"]["observations"]} gözlem</td>'
            f'<td>{number(rates["like_rate"]["mean_rate"], percent=True)} · {rates["like_rate"]["observations"]} gözlem</td></tr>'
        )
    story_comparison_html = "".join(story_rows) or '<tr><td colspan="5">Karşılaştırılabilir hikâye ölçümü bulunamadı.</td></tr>'
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
    coverage = {"complete": "Vadesi gelen pencereler tamamlandı", "incomplete": "Eksik pencereler var",
                "collecting": "Vadesi gelen ölçümler bekleniyor", "pending": "İlk ölçüm zamanı bekleniyor",
                "cold_start": "Henüz yayın yok"}[summary["coverage_status"]]
    return f'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARTFOLIO · Feed Durumu</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eeeae1;color:#24271f;font:16px/1.55 system-ui,sans-serif}}
header{{background:#f9f6ee;border-bottom:1px solid #ccc6b7;padding:22px max(5vw,20px);display:flex;justify-content:space-between;gap:16px;align-items:center}}
header strong{{font:24px Georgia,serif;letter-spacing:3px}}main{{max-width:1100px;margin:auto;padding:28px 22px}}h1,h2{{font-family:Georgia,serif}}h1{{font-size:clamp(30px,5vw,46px);margin:10px 0}}h2{{font-size:25px;margin:0 0 12px}}section,.notice{{background:#f9f6ee;border:1px solid #d3ccbd;border-radius:9px;padding:20px;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.eyebrow,dt{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#62695c}}dd{{margin:4px 0 0;font-size:18px;font-weight:600;overflow-wrap:anywhere}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:16px}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #d3ccbd}}th{{font-size:12px;color:#62695c}}.muted{{color:#62695c}}.table-scroll{{overflow-x:auto}}.table-scroll:focus{{outline:2px solid #294735;outline-offset:4px}}a{{color:#294735}}@media(max-width:620px){{main{{padding:18px 14px}}section{{padding:16px}}.grid{{grid-template-columns:1fr}}header{{align-items:flex-start;flex-direction:column}}}}
.calendar-list{{display:grid;gap:16px}}.calendar-card{{display:grid;grid-template-columns:160px minmax(0,1fr);gap:20px;padding:16px 0;border-top:1px solid #d3ccbd}}.calendar-card img,.preview-empty{{width:160px;height:200px;object-fit:contain;background:#e8e3d8}}.preview-empty{{display:grid;place-items:center;color:#62695c}}.calendar-card h3{{font:23px Georgia,serif;margin:4px 0}}.calendar-card p,.calendar-card small{{overflow-wrap:anywhere}}.refresh-bar{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}button{{background:#294735;color:#fff;border:0;border-radius:5px;padding:12px 18px;font:600 15px system-ui;cursor:pointer}}button:disabled{{opacity:.55;cursor:default}}button:focus-visible{{outline:2px solid #294735;outline-offset:4px}}@media(max-width:480px){{.calendar-card{{grid-template-columns:90px minmax(0,1fr);gap:12px}}.calendar-card img,.preview-empty{{width:90px;height:120px}}.calendar-card h3{{font-size:20px}}}}
</style></head><body><header><strong>ARTFOLIO</strong><span>Feed · salt okunur durum anlık görüntüsü</span></header><main>
<p class="eyebrow">Rapor oluşturuldu · {esc(local_now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p><h1>Yayın akışı durumu</h1>
<div class="refresh-bar"><button id="refresh" type="button" disabled>Verileri yenile</button><p id="refresh-status" role="status" aria-live="polite">Canlı yenileme için yerel panel sunucusunu açın.</p></div>
<p class="notice">Bu sayfa üretildiği andaki bir anlık görüntüsüdür. Sonraki uygunluk zamanı yayın garantisi değildir.</p>
<section><h2>Planlama</h2><div class="grid"><dl><dt>Durum</dt><dd title="{schedule_detail}">{schedule_text}</dd></dl><dl><dt>Sıradaki format</dt><dd>{shown(format_label)}<br><small>{esc(cadence_label)}</small></dd></dl><dl><dt>En erken uygunluk zamanı</dt><dd>{shown(next_at)} · Europe/Istanbul</dd></dl><dl><dt>Deneme için alt sınır</dt><dd>{shown(attempt_at)} · Europe/Istanbul</dd></dl><dl><dt>Gerçek son başarılı yayın</dt><dd>{shown(last_at)} · Europe/Istanbul</dd></dl></div></section>
<section><h2>İçerik takvimi</h2><p class="muted">Bu olası tarihler önceki yayınların başarılı tamamlanmasına bağlıdır; yayın garantisi değildir. Son kullanma ve yayın kontrolleri her tüketimde yeniden uygulanır.</p><div class="calendar-list">{calendar_html}</div></section>
<section><h2>Hazır içerik kuyruğu</h2><p class="muted">Sayım manifest durumuna dayanır; tüketim sırasında içerik, haklar ve geçmiş yeniden doğrulanır.</p><div class="metrics">{''.join(f'<dl class="metric"><dt>{esc({"ready":"Hazır", "next_format_ready":"Sıradaki formata hazır", "expired":"Süresi dolmuş", "invalid_age":"Yaşı geçersiz", "claimed":"Sahiplenilmiş", "consumed":"Tüketilmiş", "quarantined":"Karantinada"}.get(k, k))}</dt><dd>{esc(v)}</dd></dl>' for k,v in operations['queue'].items())}</div>
<div class="table-scroll" tabindex="0" role="region" aria-label="Kaydırılabilir veri tablosu"><table><thead><tr><th>Paket</th><th>Durum</th><th>Format</th><th>Son kullanma</th><th>Kaynak</th><th>Sayfa</th></tr></thead><tbody>{''.join(queue_rows)}</tbody></table></div></section>
{selection_html}
{source_html}
{experiment_html}
<section><h2>Yerel hikâye onayı</h2><ul>{story_html}</ul></section>
<section><h2>Insights ölçüm pencereleri</h2><p class="muted">Kaçırılan ölçüm pencereleri, eksik performans verisini gösterir; kaçırılmış yayın anlamına gelmez.</p><dl class="metrics">{metrics}</dl><p class="muted">Ölçüm güncelliği: {shown(freshness)}; son karşılaştırılabilir kayıt: {shown(local_timestamp(summary.get('last_comparable_capture_at')))}.</p><p class="muted">Ölçüm kapsamı: {shown(coverage)}. Kapsam tüm kayıtlı yayınların ölçüm pencerelerine dayanır; güncellik eksiksiz ölçüm anlamına gelmez.</p><h3>Format · hedef yaş grupları</h3><p class="muted">Erişim ortalaması ve kaydetme/beğeni oranları yalnızca mevcut gözlemleri özetler. Küçük örneklemler betimseldir.</p><div class="table-scroll" tabindex="0" role="region" aria-label="Kaydırılabilir veri tablosu"><table><thead><tr><th>Grup</th><th>Örneklem</th><th>Ortalama erişim</th><th>Kaydetme oranı</th><th>Beğeni oranı</th></tr></thead><tbody>{cohort_html}</tbody></table></div><p class="muted">Ölçümler mevcut yayınları özetler; hangi formatın daha iyi olduğu sonucunu çıkarmak için yeterli veri gerekir.</p></section>

<section><h2>Hikâye karşılaştırmaları</h2><p class="muted">Anlatım türü ve kapaklar aynı hedef yaştaki gerçek ölçümlerle karşılaştırılır. Her parent gönderi bir gözlemdir; detay sayfaları örnek sayısını artırmaz. Eksik ölçüm sıfır değildir ve otomatik kazanan seçilmez.</p><div class="table-scroll" tabindex="0" role="region" aria-label="Kaydırılabilir hikâye karşılaştırmaları"><table><thead><tr><th>Tür / kapak · hedef yaş</th><th>Örneklem</th><th>Ölçüm kapsamı</th><th>Kaydetme oranı</th><th>Beğeni oranı</th></tr></thead><tbody>{story_comparison_html}</tbody></table></div></section>
<script id="dashboard-config" type="application/json">null</script>
<script>
const dashboardConfig=JSON.parse(document.getElementById('dashboard-config').textContent);
const refreshButton=document.getElementById('refresh'),refreshStatus=document.getElementById('refresh-status');
if(dashboardConfig){{refreshButton.disabled=false;refreshStatus.textContent='Son başarılı rapor gösteriliyor. Yeni veriler için yenileyin.';}}
refreshButton.addEventListener('click',async()=>{{
refreshButton.disabled=true;refreshStatus.textContent='Veriler yenileniyor…';
try{{
const response=await fetch('/api/refresh',{{method:'POST',headers:{{'Content-Type':'application/json','X-Artfolio-Dashboard':dashboardConfig.token}},body:'{{}}'}});
const data=await response.json();
if(!response.ok)throw new Error(data.error||'Veriler yenilenemedi. Yeniden deneyin.');
location.reload();
}}catch(error){{refreshStatus.textContent=error.message||'Bağlantı kurulamadı. Yerel panel sunucusunu kontrol edin.';refreshButton.disabled=false;}}
}});
</script>
</main></body></html>'''
