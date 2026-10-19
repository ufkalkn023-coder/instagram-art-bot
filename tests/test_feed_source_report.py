import copy
from datetime import timedelta

import pytest

from tests.test_theme_feasibility import NOW, _observation


def test_source_report_separates_global_funnel_from_source_failure_evidence():
    from src.feed_source_report import build_source_report, render_source_report
    observation = _observation('flowers', success=True, qualified=6)
    observation.update(raw_count=20, unique_count=12, rights_qualified_count=9,
                       quality_qualified_count=8, relevance_qualified_count=6)
    observation['adapter_failures'] = [{'source': 'met', 'category': 'RATE_LIMITED'},
                                       {'source': 'met', 'category': 'RATE_LIMITED'}]
    state = {'schema_version': 1, 'themes': {'flowers': [observation]}}
    before = copy.deepcopy(state)
    report = build_source_report(state, now=NOW)
    assert report['funnel'] == {'raw': 20, 'unique': 12, 'rights': 9, 'quality': 8, 'relevance': 6}
    met = next(row for row in report['sources'] if row['source'] == 'met')
    assert met['active_attempts'] == 1 and met['failed_attempts'] == 1
    assert met['failure_categories'] == {'RATE_LIMITED': 1}
    assert met['downloads'] is None
    assert 'indirme verimi ölçülmemiştir' in render_source_report(report)
    assert state == before


def test_source_report_does_not_double_count_replayed_or_future_evidence():
    from src.feed_source_report import build_source_report
    first = _observation('flowers', qualified=6)
    future = _observation('flowers', qualified=20)
    future['attempted_at'] = (NOW + timedelta(hours=1)).isoformat()
    report = build_source_report({'schema_version': 1, 'themes': {'flowers': [first, first, future]}}, now=NOW)
    assert report['observations'] == 1 and report['funnel']['raw'] == 6
    conflicting = {**first, 'raw_count': 30}
    report = build_source_report({'schema_version': 1, 'themes': {'flowers': [first, conflicting]}}, now=NOW)
    assert report['observations'] == 0


def test_source_report_rejects_malformed_state_and_defaults_to_unavailable():
    from src.feed_source_report import build_source_report, render_source_report
    with pytest.raises(ValueError):
        build_source_report({'schema_version': 1, 'themes': []}, now=NOW)
    assert 'Kaynak gözlemi belirtilmedi' in render_source_report(None)
