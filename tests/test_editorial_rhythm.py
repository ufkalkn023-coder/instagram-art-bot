from datetime import datetime, timedelta, timezone

import pytest

from tests.test_editorial_pilot import series_data
from tests.test_story_suggestions import make_sources

NOW = datetime(2026, 10, 18, 9, tzinfo=timezone.utc)


def test_rhythm_uses_actual_projects_and_alternates_without_padding_or_reuse(tmp_path):
    from src.editorial_pilot import build_editorial_pilot
    from src.editorial_rhythm import plan_editorial_projects
    root = tmp_path / 'pilot'
    pilot = build_editorial_pilot(root, make_sources(tmp_path, 5), series_data())
    projects = [root / row['project_path'] for row in pilot['entries']]
    result = plan_editorial_projects(projects, {'publications': []}, now=NOW)
    assert [row['publication_format'] for row in result['slots']] == ['carousel', 'single', 'carousel', 'single']
    assert [row['page_count'] for row in result['slots']] == [2, 1, 3, 1]
    times = [datetime.fromisoformat(row['earliest_at']) for row in result['slots']]
    assert all(right - left >= timedelta(hours=48) for left, right in zip(times, times[1:]))
    assert len({identity for row in result['slots'] for identity in row['source_ids']}) == 5
    assert result['queue_changed'] is False and result['publication_ready'] is False
    assert all(row['review_status'] == 'unapproved' for row in result['slots'])
    with pytest.raises(ValueError, match='repeat'):
        plan_editorial_projects([projects[0], projects[0]], {'publications': []}, now=NOW)


def test_rhythm_does_not_claim_unavailable_format_has_a_ready_project(tmp_path):
    from src.editorial_rhythm import plan_editorial_projects
    from tests.test_editorial_project import make_editorial_project
    target = make_editorial_project(tmp_path)
    result = plan_editorial_projects([target], {'publications': []}, now=NOW)
    assert result['slots'][0]['publication_format'] == 'single'
    assert datetime.fromisoformat(result['slots'][0]['earliest_at']) == NOW + timedelta(hours=48)
    assert result['unfilled_slots'][0]['publication_format'] == 'carousel'
