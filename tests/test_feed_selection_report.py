from datetime import timedelta

import pytest

from tests.test_feed_analytics import NOW, publication


def test_selection_report_uses_recorded_components_and_rejects_conflicting_parents():
    from src.feed_selection_report import build_selection_report
    pub = {**publication(), 'quality_component': 70, 'engagement_component': 0,
           'engagement_confidence': 0, 'carousel_theme': 'flowers'}
    history = {'publications': [pub, {**publication(1), 'media_id': pub['media_id']}]}
    assert build_selection_report(history, [], now=NOW) == []
    rows = build_selection_report({'publications': [pub]}, [], now=NOW)
    assert rows[0]['components']['quality_component'] == 70
    assert rows[0]['components']['engagement_component'] == 0
    assert rows[0]['origin'] == 'published'
    assert 'final_score' not in rows[0]['components']


def test_selection_report_preserves_missingness_and_limits_prepared_evidence():
    from src.feed_selection_report import build_selection_report, render_selection_report
    queue = [{'id': 'package', 'state': 'READY', 'title': '<script>bad</script>',
              'publication_format': 'carousel', 'expires_at': (NOW + timedelta(days=2)).isoformat(),
              'selection_metadata': {'quality_component': True, 'learned_score': float('nan'),
                                     'engagement_confidence': 0.5, 'access_token': 'do-not-display'}}]
    rows = build_selection_report({}, queue, now=NOW)
    assert rows[0]['components'] == {'engagement_confidence': 0.5}
    assert rows[0]['origin'] == 'prepared'
    page = render_selection_report(rows)
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert 'do-not-display' not in page and 'kullanılamıyor' in page
    assert 'yayın başarısı değildir' in page


def test_queue_loader_keeps_only_allowlisted_selection_metadata():
    from scripts.report_feed_dashboard import _queue_details
    result = _queue_details([{'id': 'p', 'state': 'READY', 'content': {
        'publication_format': 'single', 'artworks': [], 'assets': [],
        'publication_metadata': {'quality_component': 80, 'secret': 'do-not-display'}}}])
    assert result[0]['selection_metadata'] == {'quality_component': 80}


@pytest.mark.parametrize('raw_value', [True, '70'])
def test_selection_report_never_coerces_recorded_score_values(raw_value):
    from src.feed_selection_report import build_selection_report
    pub = {**publication(), 'quality_component': raw_value, 'engagement_confidence': 0}
    rows = build_selection_report({'publications': [pub]}, [], now=NOW)
    assert len(rows) == 1
    assert 'quality_component' not in rows[0]['components']
    assert rows[0]['components']['engagement_confidence'] == 0
