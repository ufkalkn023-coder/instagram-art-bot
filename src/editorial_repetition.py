"""Transparent, bounded editorial repetition hints; not semantic certification."""
import html

from src.editorial_brief import EditorialBrief
from src.editorial_v2 import _normalized

STOP_WORDS = set('the and this that with from into across around behind between while above below through '
                 'one two work works painting painted forms small large bir ve bu ile için gibi olan resim eser'.split())
MOTIFS = {
    'Mesafe ve ölçek': {'distance', 'distant', 'recede', 'recedes', 'foreground', 'background', 'horizon',
                        'scale', 'uzak', 'uzaklık', 'ufuk', 'ölçek', 'mesafe', 'derinlik'},
    'Işık ve kontrast': {'light', 'shadow', 'shadows', 'dark', 'pale', 'contrast', 'bright',
                         'ışık', 'gölge', 'karanlık', 'aydınlık', 'kontrast'},
    'Kumaş ve yüzey': {'lace', 'fabric', 'fold', 'folds', 'texture', 'surface', 'brushwork',
                       'dantel', 'kumaş', 'kıvrım', 'kıvrımlar', 'doku', 'yüzey'},
    'Bakış ve jest': {'gaze', 'eyes', 'hand', 'hands', 'gesture', 'face', 'head',
                      'bakış', 'göz', 'eller', 'jest', 'yüz', 'baş'},
}


def _words(brief):
    parts = [brief.angle, brief.lead, brief.comparison_rationale]
    parts.extend(note.text for note in brief.notes if note.kind == 'visual_note')
    return {word for word in _normalized(' '.join(parts)).split() if len(word) >= 3 and word not in STOP_WORDS}


def repetition_warnings(value, history):
    brief = EditorialBrief.model_validate(value.model_dump() if isinstance(value, EditorialBrief) else value)
    if not isinstance(history, (list, tuple)) or len(history) > 100:
        raise ValueError('Repetition context is bounded to one hundred projects')
    words = _words(brief)
    warnings, seen = [], set()
    for record in history:
        if (not isinstance(record, dict) or set(record) != {'id', 'brief'} or not isinstance(record['id'], str)
                or not 1 <= len(record['id']) <= 80 or record['id'] in seen):
            raise ValueError('History requires unique bounded project identities and briefs')
        seen.add(record['id'])
        previous = record['brief']
        previous = EditorialBrief.model_validate(previous.model_dump() if isinstance(previous, EditorialBrief) else previous)
        old = _words(previous)
        shared = words & old
        similarity = len(shared) / len(words | old) if words | old else 0
        if len(shared) >= 4 and similarity >= 0.45:
            warnings.append(dict(kind='similar_angle', project_id=record['id'], title=previous.title,
                                 matched_terms=sorted(shared), similarity=round(similarity, 3)))
        for motif, terms in MOTIFS.items():
            current_terms, previous_terms = words & terms, old & terms
            if len(current_terms) >= 2 and len(previous_terms) >= 2:
                warnings.append(dict(kind='recurring_motif', project_id=record['id'], title=previous.title,
                                     motif=motif, matched_terms=sorted(current_terms), previous_terms=sorted(previous_terms)))
    return dict(advisory_only=True, compared_projects=len(history), warnings=warnings,
                limitations='Token/motif hints over retained draft text; not a semantic verdict or publication gate.')


def render_repetition_panel(report):
    rows = []
    for warning in report['warnings']:
        reason = 'Benzer anlatım sözcükleri' if warning['kind'] == 'similar_angle' else warning['motif']
        terms = ', '.join(warning['matched_terms'])
        rows.append(f'<li><strong>{html.escape(reason)}</strong> — {html.escape(warning["title"])}'
                    f'<p>Bu taslaktaki işaretler: {html.escape(terms)}</p></li>')
    empty = ('Karşılaştırılacak başka proje seçilmedi.' if not report['compared_projects'] else
             'Bu sınırlı sözcük ve motif kontrolünde belirgin tekrar bulunmadı.')
    return ('<section id="repetition-review"><h2>Anlatım ritmi</h2>'
            f'<p>{report["compared_projects"]} diğer taslakla karşılaştırıldı. Uyarılar yoruma açıktır; onayı veya yayını otomatik engellemez. '
            'Yalnız kaydedilen metinler değerlendirilir.</p>'
            + (f'<ul>{"".join(rows)}</ul>' if rows else f'<p>{empty}</p>') + '</section>')
