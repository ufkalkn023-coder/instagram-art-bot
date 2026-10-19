"""Editorial controls inside the same guarded local story editor."""
import html
import json

from src.story_review import source_preview


def review_editorial_page(project, report, *, review_context=None):
    escape = html.escape
    plan = project.plan
    from src.editorial_revision import render_revision_comparison
    comparison_panel = render_revision_comparison((review_context or {}).get('comparison'))
    from src.editorial_repetition import render_repetition_panel
    repetition_panel = render_repetition_panel((review_context or {}).get('repetition',
                                               dict(compared_projects=0, warnings=[])))
    registry = {source.artwork.canonical_id: source for source in project.sources}
    controls = []
    from src.editorial_sources import NOTE_LABELS, source_note_panel
    for note in plan.brief.notes:
        fields = ''.join(f'<label>{label}<input class="coordinate" type="number" min="0" max="1" '
                         f'step="0.0001" value="{value}"></label>'
                         for label, value in zip(('Sol', 'Üst', 'Sağ', 'Alt'), note.focus or (0, 0, 1, 1)))
        checked = 'checked' if note.focus else ''
        readonly = 'readonly' if note.kind != 'visual_note' else ''
        preview = source_preview(registry[note.artwork_id].image_path)
        controls.append(f'''<section class="note-editor" data-note="{escape(note.id)}">
<h2>{escape(registry[note.artwork_id].artwork.title)}</h2><p>{NOTE_LABELS[note.kind]}</p>
{source_note_panel(note, registry[note.artwork_id])}
<label>Detay başlığı<input class="note-heading" maxlength="80" value="{escape(note.heading, quote=True)}"></label>
<label>Kaynak notu veya görsel gözlem<textarea class="note-text" maxlength="350" rows="3" {readonly}>{escape(note.text)}</textarea></label>
<label class="check"><input class="has-focus" type="checkbox" {checked}> Bu gözlem için detay sayfası</label>
<details {'open' if note.focus else ''}><summary>Özgün eser ve detay alanı</summary>
<div class="crop-surface"><img class="crop-original" src="{preview}" alt="{escape(registry[note.artwork_id].artwork.title)} — özgün eser" draggable="false">
<div class="crop-selection" aria-hidden="true"></div></div>
<fieldset><legend>Detay koordinatları</legend>{fields}</fieldset>
<p class="crop-feedback" role="status" aria-live="polite"></p></details></section>''')
    images = ''.join(f'<figure><img src="{escape(page["path"], quote=True)}" '
                     f'alt="{escape(page["role"])}: {escape(plan.public_title)}" width="1080" height="1350">'
                     f'<figcaption>{n+1}. sayfa · {escape(page["role"])}</figcaption></figure>'
                     for n, page in enumerate(report['package']['slides']))
    styles = ''.join(f'<option value="{value}" {"selected" if plan.style == value else ""}>{label}</option>'
                     for value, label in (('journal', 'Journal'), ('artwork_led', 'Eser odaklı'), ('detail_study', 'Detay incelemesi')))
    comparison = ''
    if plan.narrative == 'comparison':
        aspects = ''.join(f'<option value="{name}" {"selected" if name == plan.brief.comparison_aspect else ""}>{label}</option>'
                          for name, label in (('composition', 'Kompozisyon'), ('gesture', 'Jest'), ('light', 'Işık'),
                                              ('medium', 'Malzeme'), ('date', 'Tarih')))
        comparison = f'<label>Karşılaştırma yönü<select id="comparison-aspect">{aspects}</select></label>'
        comparison += f'<label>Karşılaştırma gerekçesi<textarea id="comparison-rationale" maxlength="350">{escape(plan.brief.comparison_rationale)}</textarea></label>'
    payload = json.dumps(dict(revision=project.revision, plan=plan.model_dump(mode='json')), ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    return ('''<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artfolio editoryal masa</title><style>
*{box-sizing:border-box}body{margin:0;color:#252522;background:#fff;font:17px/1.6 system-ui,sans-serif}
header,main{max-width:1200px;margin:auto;padding:24px}h1,h2{font-family:Georgia,serif;font-weight:normal;line-height:1.2}
h1{font-size:clamp(30px,4vw,48px)}h2{font-size:26px}label{display:block;margin:16px 0;font-size:15px}
input,textarea,select{display:block;width:100%;font:16px/1.5 system-ui;color:inherit;padding:10px;border:1px solid #aaa;margin-top:6px}
textarea{resize:vertical}button{padding:12px 20px;background:#252522;color:white;border:0;font:16px system-ui;cursor:pointer}
button:disabled{opacity:.5;cursor:default}:focus-visible{outline:3px solid #315f96;outline-offset:3px}
.toolbar{position:sticky;top:0;background:white;border-block:1px solid #ccc;z-index:2;padding:12px 24px;display:flex;gap:20px;align-items:center}
#status{margin:0;font-size:14px}.workbench{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:36px}
.note-editor{border-top:1px solid #ccc;margin-top:28px;padding-top:8px}figure{margin:0 0 28px}figure img{display:block;width:100%;height:auto}
figcaption,.note-editor p,.help{font-size:14px;color:#555;overflow-wrap:anywhere}fieldset{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;border:1px solid #ccc}
fieldset label{margin:0}fieldset input{min-width:0;padding:6px}.check{display:flex;align-items:center;gap:10px}.check input{width:auto;margin:0}
.crop-surface{position:relative;line-height:0;touch-action:none;user-select:none;cursor:crosshair}.crop-original{display:block;width:100%;height:auto}
.crop-selection{position:absolute;border:2px solid white;outline:1px solid #315f96;background:#315f9620;pointer-events:none}
summary{cursor:pointer}#caption-preview{background:#f5f5f5}a{color:inherit} @media(max-width:700px){header,main{padding:18px}.workbench{grid-template-columns:1fr}.toolbar{padding:12px 18px;gap:12px}}
.revision-images{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px}.revision-images img{width:100%;height:auto;display:block;margin-bottom:18px}#revision-comparison{border-top:1px solid #ccc;margin-top:32px;padding-top:16px;overflow-wrap:anywhere}#review-approval{border-top:1px solid #ccc;margin:32px 0;padding-top:16px}#review-approval button{margin-top:8px}
.source-panel{margin:14px 0;padding:12px 0;border-block:1px solid #ddd}.source-panel dl{display:grid;grid-template-columns:minmax(80px,1fr) minmax(0,3fr);gap:6px 14px;font-size:14px}.source-panel dd{margin:0;overflow-wrap:anywhere}.source-panel dt{color:#555}.source-panel blockquote{margin:12px 0;padding-left:14px;border-left:2px solid #aaa;white-space:pre-wrap;overflow-wrap:anywhere}
</style><header><p>Artfolio / Editoryal masa</p>'''
            + f'<h1>{escape(plan.public_title)}</h1><p>{project.revision}. sürüm · {len(project.sources)} eser · {len(plan.slides)} sayfa</p></header>'
            + '<div class="toolbar"><button id="save" type="button" disabled>Değişiklikleri kaydet</button>'
            '<p id="status" role="status" aria-live="polite">Düzenlemek için projeyi yerel sunucuyla açın.</p></div>'
            '<main><p class="help">Kaydetmek yayın onayı vermez. Görsel gözlemleri ve kaynak bilgilerini inceleyin. Her değişiklik önceki onayı geçersiz kılar.</p>'
            '<div class="workbench"><div><label>Başlık<textarea id="public-title" rows="2" maxlength="90">'
            + escape(plan.brief.title) + '</textarea></label><label>Anlatım açısı<textarea id="editorial-angle" rows="3" maxlength="400">'
            + escape(plan.brief.angle) + '</textarea></label><label>Kısa giriş<textarea id="editorial-lead" rows="4" maxlength="500">'
            + escape(plan.brief.lead) + f'</textarea></label><label>Görsel düzen<select id="editorial-style">{styles}</select></label>'
            + comparison + ''.join(controls) + '<label>Teslim edilecek açıklama<textarea id="caption-preview" rows="10" readonly>'
            + escape(report['caption']) + '</textarea></label></div><div><h2>Kaydedilen sayfalar</h2>' + images
            + '</div></div>' + (review_context or {}).get('dossier', '') + comparison_panel + repetition_panel
            + '<section id="review-approval"><h2>Bu sürümü onayla</h2>'
            '<p>Onay yalnız görünen sürümün görselleri, kaynakları ve açıklaması için geçerlidir. Canlı yayın başlatmaz.</p>'
            '<label class="check"><input id="review-confirm" type="checkbox" disabled>Görselleri, açıklamayı ve kaynak notlarını inceledim.</label>'
            '<button id="approve-review" type="button" disabled>İncelenen sürümü onayla</button>'
            '<p id="approval-status" role="status" aria-live="polite">Onay için yerel editörü açın.</p></section>'
            '</main><script type="application/json" id="story-data">' + payload + '</script>'
            + '''<script>
window.ARTFOLIO_EDITOR=null;
const original=JSON.parse(document.getElementById('story-data').textContent);
const button=document.getElementById('save'),status=document.getElementById('status');
if(window.ARTFOLIO_EDITOR){button.disabled=false;status.textContent='Düzenleyin ve kaydedin; önizleme güncellenecek.';}
const approve=document.getElementById('approve-review'),confirmReview=document.getElementById('review-confirm'),approvalStatus=document.getElementById('approval-status');
let dirty=false,approving=false;
function approvalState(){const context=window.ARTFOLIO_EDITOR;confirmReview.disabled=!context||dirty||approving||context.approved;approve.disabled=!context||dirty||approving||context.approved||!confirmReview.checked;if(context){approvalStatus.textContent=dirty?'Kaydedilmemiş değişiklikler var. Kaydedip yeni sürümü inceleyin.':context.approved?'Bu sürüm onaylı. Düzenleme yaparsanız yeniden onay gerekir.':'Bu sürüm henüz onaylanmadı.';}}
for(const field of document.querySelectorAll('main input, main textarea, main select')){if(field.id==='review-confirm'||field.readOnly)continue;field.addEventListener('input',()=>{dirty=true;confirmReview.checked=false;approvalState();});field.addEventListener('change',()=>{dirty=true;confirmReview.checked=false;approvalState();});}
confirmReview.addEventListener('change',approvalState);approvalState();
approve.addEventListener('click',async()=>{if(approve.disabled)return;approving=true;button.disabled=true;approvalState();approvalStatus.textContent='Görünen sürüm doğrulanıyor…';try{const context=window.ARTFOLIO_EDITOR;const response=await fetch('api/approve',{method:'POST',headers:{'Content-Type':'application/json','X-Artfolio-Editor':context.token},body:JSON.stringify({expected_revision:original.revision,expected_digest:context.digest,reviewed:true})});const result=await response.json();if(!response.ok)throw new Error(result.error||'Onay kaydedilemedi.');context.approved=true;approving=false;approvalState();button.disabled=false;}catch(error){approving=false;confirmReview.checked=false;approvalState();approvalStatus.textContent=error.message;button.disabled=false;}});
function focusValues(section){
const values=[...section.querySelectorAll('.coordinate')].map(input=>{
if(!input.value.trim())throw new Error('Dört detay koordinatını doldurun.');return Number(input.value);});
if(values.some(value=>!Number.isFinite(value)||value<0||value>1)||values[0]>=values[2]||values[1]>=values[3])throw new Error('Geçerli bir detay alanı seçin.');return values;
}
for(const section of document.querySelectorAll('.note-editor')){
const fields=[...section.querySelectorAll('.coordinate')],surface=section.querySelector('.crop-surface'),selection=section.querySelector('.crop-selection'),feedback=section.querySelector('.crop-feedback');
let drag=null;
function refresh(){try{const [l,t,r,b]=focusValues(section);selection.hidden=!section.querySelector('.has-focus').checked;Object.assign(selection.style,{left:l*100+'%',top:t*100+'%',width:(r-l)*100+'%',height:(b-t)*100+'%'});feedback.textContent='Detay, kaydettiğinizde tam çözünürlüklü eserden hazırlanır.';}catch(error){selection.hidden=true;feedback.textContent=error.message;}}
function point(event){const box=surface.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))];}
function move(event){if(!drag||event.pointerId!==drag.id)return;const end=point(event),start=drag.start;[Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])].forEach((value,n)=>{fields[n].value=String(Math.round(value*10000)/10000);});refresh();}
function finish(event,cancelled=false){if(!drag||event.pointerId!==drag.id)return;if(!cancelled)move(event);let valid=true;try{focusValues(section);}catch{valid=false;}if(cancelled||!valid)fields.forEach((field,n)=>{field.value=drag.before[n];});const id=drag.id;drag=null;if(surface.hasPointerCapture(id))surface.releasePointerCapture(id);refresh();}
surface.addEventListener('pointerdown',event=>{if(drag||event.button!==0||event.isPrimary===false)return;event.preventDefault();drag={id:event.pointerId,start:point(event),before:fields.map(f=>f.value)};surface.setPointerCapture(event.pointerId);section.querySelector('.has-focus').checked=true;});
surface.addEventListener('pointermove',move);surface.addEventListener('pointerup',event=>finish(event));surface.addEventListener('pointercancel',event=>finish(event,true));surface.addEventListener('lostpointercapture',event=>finish(event,true));
surface.addEventListener('pointerdown',()=>{dirty=true;confirmReview.checked=false;approvalState();});
fields.forEach(field=>field.addEventListener('input',refresh));section.querySelector('.has-focus').addEventListener('change',refresh);refresh();
}
button.addEventListener('click',async()=>{button.disabled=true;approve.disabled=true;confirmReview.disabled=true;status.textContent='Taslak kontrol ediliyor…';try{
const plan=structuredClone(original.plan),brief=plan.brief;
brief.title=document.getElementById('public-title').value;brief.angle=document.getElementById('editorial-angle').value;brief.lead=document.getElementById('editorial-lead').value;plan.style=document.getElementById('editorial-style').value;
if(brief.narrative==='comparison'){brief.comparison_aspect=document.getElementById('comparison-aspect').value;brief.comparison_rationale=document.getElementById('comparison-rationale').value;}
for(const section of document.querySelectorAll('.note-editor')){const note=brief.notes.find(n=>n.id===section.dataset.note);note.heading=section.querySelector('.note-heading').value;note.text=section.querySelector('.note-text').value;note.focus=section.querySelector('.has-focus').checked?focusValues(section):null;}
const response=await fetch('api/plan',{method:'POST',headers:{'Content-Type':'application/json','X-Artfolio-Editor':window.ARTFOLIO_EDITOR.token},body:JSON.stringify({expected_revision:original.revision,plan})});const result=await response.json();if(!response.ok)throw new Error(result.error||'Taslak kaydedilemedi.');location.reload();
}catch(error){status.textContent=error.message;button.disabled=false;approvalState();}});
</script></html>''')
