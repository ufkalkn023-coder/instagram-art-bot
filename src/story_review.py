"""Escaped, portable review pages with optional localhost editing."""

from __future__ import annotations

import html
import json
import base64
import io
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from PIL import Image, ImageOps

from src.story_quality import ISSUE_MESSAGES

if TYPE_CHECKING:
    from src.story_project import StoryProject


def source_preview(path: str) -> str:
    """Embed a portable, oriented raster without modifying the reviewed source."""
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((960, 960), Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        image.save(stream, "JPEG", quality=80, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode(
        "ascii"
    )


def review_page(project: StoryProject, report: dict) -> str:
    from src.editorial_story import EditorialStoryPlan
    if isinstance(project.plan, EditorialStoryPlan):
        from src.editorial_review import review_editorial_page
        return review_editorial_page(project, report)
    escape = html.escape
    roles = dict(
        cover="Kapak",
        artwork="Eser",
        detail="Detay",
        comparison="Karşılaştırma",
        context="Müze notu",
        closing="Kapanış",
    )
    narratives = dict(
        single_study="Tek eser incelemesi",
        comparison="İki eser karşılaştırması",
        thematic_selection="Eser seçkisi",
    )
    registry = {s.artwork.canonical_id: s.artwork for s in project.sources}
    detail_ids = {slide.artwork_ids[0] for slide in project.plan.slides if slide.focus}
    previews = {
        source.artwork.canonical_id: source_preview(source.image_path)
        for source in project.sources
        if source.artwork.canonical_id in detail_ids
    }
    cards = []
    for position, (slide, media) in enumerate(
        zip(project.plan.slides, report["package"]["slides"]), 1
    ):
        locked = "disabled" if slide.role == "cover" else ""
        focus = ""
        if slide.focus:
            focus = (
                '<details class="crop-editor" open><summary>Detay alanını seç</summary>'
                '<p class="note">Eser üzerinde sürükleyin veya aşağıdaki koordinatları klavyeyle düzenleyin.</p>'
                '<div class="crop-surface">'
                f'<img class="crop-original" src="{previews[slide.artwork_ids[0]]}" alt="{escape(registry[slide.artwork_ids[0]].title)} — özgün eser" draggable="false">'
                '<div class="crop-selection" aria-hidden="true" hidden></div></div>'
                "<fieldset><legend>Detay alanı · sol / üst / sağ / alt</legend>"
                + "".join(
                    f'<label>{label}<input aria-label="{label}" class="coordinate" type="number" min="0" max="1" step="0.0001" value="{value}"></label>'
                    for label, value in zip(("Sol", "Üst", "Sağ", "Alt"), slide.focus)
                )
                + '</fieldset><p class="crop-feedback note" role="status" aria-live="polite"></p>'
                '<canvas class="crop-preview" role="img" aria-label="Seçilen detayın önizlemesi" hidden></canvas></details>'
            )
        cover_note = (
            '<p class="note">Kapak başlığını sayfanın üstünden düzenleyin.</p>'
            if slide.role == "cover"
            else ""
        )
        credits = []
        for identity in slide.artwork_ids:
            art = registry[identity]
            label = escape(
                f"{art.title} · {art.artist_display_name or art.artist_name} · {art.museum_name}"
            )
            url = urlsplit(art.museum_url or "")
            if url.scheme == "https" and url.hostname and not url.username:
                label = f'<a href="{escape(art.museum_url, quote=True)}" target="_blank" rel="noreferrer">{label}</a>'
            credits.append(label)
        cards.append(f'''<article data-slide="{escape(slide.id)}"><div class="image"><img src="{escape(media["path"])}" alt="{escape(roles[slide.role])}: {escape(slide.title or project.plan.public_title)}" loading="lazy"></div>
        <div class="controls"><div class="eyebrow">{position:02d} / {len(project.plan.slides):02d} · {roles[slide.role]}</div>
        <label>Sayfa sırası<input class="order" type="number" min="1" max="{len(project.plan.slides)}" value="{position}"></label>
        <label>Başlık<textarea class="title" rows="2" maxlength="180" {locked}>{escape(slide.title)}</textarea></label>
        <label>Metin<textarea class="body" rows="3" maxlength="600" {locked}>{escape(slide.body)}</textarea></label>{focus}{cover_note}
        <p class="source">{"<br>".join(credits)}</p></div></article>''')
    payload = (
        json.dumps(
            dict(revision=project.revision, plan=project.plan.model_dump(mode="json")),
            ensure_ascii=False,
        )
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
    )
    choices = "".join(
        f'<option value="{value}" {"selected" if project.plan.cover_style.value == value else ""}>{label}</option>'
        for value, label in (
            ("museum_journal", "Museum Journal"),
            ("artwork_first", "Artwork First"),
            ("detail_study", "Detail Study"),
        )
    )
    warnings = " · ".join(
        dict.fromkeys(
            ISSUE_MESSAGES.get(i["code"], "Taslağı kontrol edin")
            for i in report["quality"]["issues"]
        )
    )
    return (
        """<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ARTFOLIO · Hikâye masası</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#eeeae1;color:#24271f;font:16px/1.5 system-ui,sans-serif}header{padding:24px 5vw 18px;background:#f9f6ee;border-bottom:1px solid #ccc6b7;display:flex;justify-content:space-between;align-items:center;gap:20px}header strong{font:24px Georgia,serif;letter-spacing:3px}.badge{font-size:12px;text-transform:uppercase;letter-spacing:1px}main{max-width:1220px;margin:auto;padding:30px 24px}h1{font:clamp(28px,4vw,44px)/1.15 Georgia,serif;margin:12px 0 24px}.intro{display:grid;grid-template-columns:1fr 260px;gap:20px;margin-bottom:28px}label{display:block;font-size:13px;font-weight:600;margin:12px 0}input,textarea,select{width:100%;display:block;margin-top:6px;border:1px solid #b9b5a8;background:#fffdf6;border-radius:5px;padding:10px;color:inherit;font:16px/1.4 system-ui}textarea{resize:vertical}input:focus,textarea:focus,select:focus,button:focus{outline:2px solid #526c53;outline-offset:3px}button{border:0;border-radius:5px;background:#294735;color:#fff;padding:12px 20px;font:600 15px system-ui;cursor:pointer}button:disabled{opacity:.5;cursor:default}.toolbar{position:sticky;top:0;z-index:1;background:#f9f6ee;padding:12px 5vw;border-bottom:1px solid #ccc6b7;display:flex;align-items:center;gap:20px}#status{margin:0;font-size:14px}article{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,.72fr);gap:28px;margin:24px 0 48px;padding:22px;background:#f9f6ee;border:1px solid #d3ccbd;border-radius:10px}.image{background:#e8e3d8;line-height:0}.image img{width:100%;height:auto;display:block}.eyebrow{font-size:12px;letter-spacing:1px;text-transform:uppercase;border-bottom:1px solid #d3ccbd;padding-bottom:12px}.order{max-width:85px}fieldset{border:1px solid #c7c1b3;display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:12px}legend{font-size:12px}.coordinate{padding:7px;min-width:0}.source a{color:inherit;text-underline-offset:3px}.source{font-size:12px;color:#62695c;overflow-wrap:anywhere}.note{font-size:13px;color:#62695c}.footer{border-top:1px solid #c7c1b3;padding:18px 0;font-size:13px;color:#62695c}@media(max-width:650px){header{padding:20px;align-items:flex-start}header strong{font-size:20px}.toolbar{padding:12px 20px;gap:12px}main{padding:22px 16px}.intro,article{grid-template-columns:1fr}article{padding:14px;gap:18px}#status{font-size:12px}button{padding:11px 14px;white-space:nowrap}}
.crop-editor{margin:18px 0}.crop-editor summary{cursor:pointer;font-weight:600}.crop-surface{position:relative;line-height:0;touch-action:none;cursor:crosshair;user-select:none}.crop-original{display:block;width:100%;height:auto}.crop-selection{position:absolute;border:2px solid #fff;outline:1px solid #294735;background:#29473520;pointer-events:none}.crop-preview{display:block;max-width:100%;height:auto;background:#e8e3d8}.crop-preview[hidden],.crop-selection[hidden]{display:none}fieldset label{margin:0;font-size:12px}fieldset .coordinate{margin-top:4px}.crop-editor summary:focus-visible{outline:2px solid #526c53;outline-offset:3px}
</style><header><strong>ARTFOLIO</strong><span class="badge">Hikâye masası · yerel taslak</span></header>
<div class="toolbar"><button id="save" type="button" disabled>Değişiklikleri kaydet</button><p id="status" role="status" aria-live="polite">Önizleme hazır. Düzenleme sunucusu açıldığında kaydetme etkinleşir.</p></div><main>"""
        + f"""
<div class="badge">{escape(narratives[project.plan.narrative])} · {len(project.sources)} eser · {len(project.plan.slides)} sayfa · sürüm {project.revision}</div><h1>{escape(project.plan.public_title)}</h1>
<div class="intro"><label>Kapak başlığı<textarea id="public-title" rows="2" maxlength="180">{escape(project.plan.public_title)}</textarea></label><label>Kapak stili<select id="cover-style">{choices}</select></label></div>
<p class="note">Yayın öncesi görsel ve içerik kontrolü gerekir. Detay alanları görüntünün 0–1 aralığındaki koordinatlarıdır. Kapak ilk, kapanış son sayfada kalır.</p>
{"".join(cards)}<div class="footer">Kontrol: {escape(warnings)}<br>Bu taslak Instagram'a gönderilmez. Müze kaynakları her sayfanın yanında yer alır.</div>
</main><script type="application/json" id="story-data">{payload}</script>"""
        + """<script>
window.ARTFOLIO_EDITOR=null;
const original=JSON.parse(document.getElementById('story-data').textContent);
const button=document.getElementById('save'),status=document.getElementById('status');
if(window.ARTFOLIO_EDITOR){button.disabled=false;status.textContent='Düzenleyin ve kaydedin. Yalnızca değişen sayfalar yeniden hazırlanır.';}
function readFocus(fields){
const values=fields.map(el=>{
const raw=el.value.trim(),value=Number(raw);
if(!raw||!Number.isFinite(value)||value<0||value>1)throw new Error('Detay alanının dört koordinatını da 0–1 arasında doldurun.');
return value;
});
if(values.length!==4||values[0]>=values[2]||values[1]>=values[3])throw new Error('Sağ sınır soldan, alt sınır üstten büyük olmalı.');
return values;
}
function setupCrop(article){
const fields=Array.from(article.querySelectorAll('.coordinate'));
if(!fields.length)return;
const surface=article.querySelector('.crop-surface'),image=article.querySelector('.crop-original');
const selection=article.querySelector('.crop-selection'),preview=article.querySelector('.crop-preview'),feedback=article.querySelector('.crop-feedback');
let drag=null;
function refresh(){
try{
const [left,top,right,bottom]=readFocus(fields);
if(!image.naturalWidth||!image.naturalHeight){feedback.textContent='Özgün eser yükleniyor…';return;}
selection.hidden=false;
Object.assign(selection.style,{left:left*100+'%',top:top*100+'%',width:(right-left)*100+'%',height:(bottom-top)*100+'%'});
const width=(right-left)*image.naturalWidth,height=(bottom-top)*image.naturalHeight;
const scale=Math.min(1,420/width,420/height);
preview.width=Math.max(1,Math.round(width*scale));preview.height=Math.max(1,Math.round(height*scale));
preview.getContext('2d').drawImage(image,left*image.naturalWidth,top*image.naturalHeight,width,height,0,0,preview.width,preview.height);
preview.hidden=false;feedback.textContent='Seçilen detay · değişiklikler kaydettiğinizde uygulanır.';
}catch(error){selection.hidden=true;preview.hidden=true;feedback.textContent=error.message;}
}
function point(event){
const box=surface.getBoundingClientRect();
return [Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))];
}
function move(event){
if(!drag||event.pointerId!==drag.id)return;
const end=point(event),start=drag.start;
const values=[Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])];
fields.forEach((field,index)=>{field.value=String(Math.round(values[index]*10000)/10000);});
refresh();
}
function finish(event,cancelled=false){
if(!drag||event.pointerId!==drag.id)return;
if(!cancelled)move(event);
let valid=true;try{readFocus(fields);}catch{valid=false;}
if(cancelled||!valid)fields.forEach((field,index)=>{field.value=drag.before[index];});
const id=drag.id;drag=null;
if(surface.hasPointerCapture(id))surface.releasePointerCapture(id);
refresh();
}
surface.addEventListener('pointerdown',event=>{
if(drag||event.button!==0||event.isPrimary===false||!image.naturalWidth)return;
event.preventDefault();drag={id:event.pointerId,start:point(event),before:fields.map(field=>field.value)};
surface.setPointerCapture(event.pointerId);
});
surface.addEventListener('pointermove',move);
surface.addEventListener('pointerup',event=>finish(event));
surface.addEventListener('pointercancel',event=>finish(event,true));
surface.addEventListener('lostpointercapture',event=>finish(event,true));
fields.forEach(field=>field.addEventListener('input',refresh));
image.addEventListener('load',refresh);
image.addEventListener('error',()=>{selection.hidden=true;preview.hidden=true;feedback.textContent='Özgün eser yüklenemedi. Projeyi yeniden açın.';});
refresh();
}
for(const article of document.querySelectorAll('article[data-slide]'))setupCrop(article);
button.addEventListener('click',async()=>{
button.disabled=true;status.textContent='Taslak kontrol ediliyor…';
try{
const plan=structuredClone(original.plan);
const title=document.getElementById('public-title').value;
if(title!==plan.public_title)plan.headline_kind='user_edit';
plan.public_title=title;plan.cover_style=document.getElementById('cover-style').value;
const order=new Map();
for(const article of document.querySelectorAll('article[data-slide]')){
const slide=plan.slides.find(s=>s.id===article.dataset.slide);
slide.title=article.querySelector('.title').value;slide.body=article.querySelector('.body').value;
order.set(slide.id,Number(article.querySelector('.order').value));
if(slide.focus){
const focus=readFocus(Array.from(article.querySelectorAll('.coordinate')));
if(JSON.stringify(focus)!==JSON.stringify(slide.focus))slide.focus_basis='preview_focus';slide.focus=focus;
}
}
if(new Set(order.values()).size!==plan.slides.length||Array.from(order.values()).some(n=>!Number.isInteger(n)||n<1||n>plan.slides.length))throw new Error('Her sayfaya benzersiz bir sıra verin.');
plan.slides.sort((a,b)=>order.get(a.id)-order.get(b.id));
const response=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json','X-Artfolio-Editor':window.ARTFOLIO_EDITOR.token},body:JSON.stringify({expected_revision:original.revision,plan})});
const result=await response.json();
if(!response.ok)throw new Error(result.error||'Taslak kaydedilemedi.');
location.reload();
}catch(error){status.textContent=error.message;button.disabled=false;}
});
</script></html>"""
    )
