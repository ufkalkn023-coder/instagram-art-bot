"""Require a declared comparison with different, source-bound observations."""
from src.editorial_brief import EditorialBrief, validate_editorial_brief
from src.editorial_copy import check_copy
from src.editorial_v2 import _normalized


def validate_editorial_comparison(value, sources) -> EditorialBrief:
    brief = validate_editorial_brief(value, sources)
    if brief.narrative != 'comparison':
        if brief.comparison_aspect is not None or brief.comparison_rationale.strip():
            raise ValueError('Comparison rationale belongs to a comparison narrative')
        return brief
    if brief.comparison_aspect is None or len(brief.comparison_rationale.strip()) < 20:
        raise ValueError('A comparison requires a specific aspect and rationale')
    check_copy(brief.comparison_rationale)
    evidence = {}
    for identity in brief.source_ids:
        if brief.comparison_aspect in {'medium', 'date'}:
            notes = [note for note in brief.notes if note.artwork_id == identity
                     and note.kind == 'catalogue_fact' and note.field == brief.comparison_aspect]
        else:
            notes = [note for note in brief.notes if note.artwork_id == identity
                     and note.kind in {'visual_note', 'museum_quote'}]
        if not notes:
            raise ValueError('Both sources need evidence for the declared comparison aspect')
        evidence[identity] = {_normalized(note.text) for note in notes}
    unknown = {'unknown', 'unknown date', 'undated', 'n a', 'none'}
    if any(not (values - unknown) for values in evidence.values()):
        raise ValueError('Unknown metadata is not comparison evidence')
    left, right = (evidence[identity] for identity in brief.source_ids)
    if left == right:
        raise ValueError('Shared metadata alone does not explain a comparison')
    return brief
