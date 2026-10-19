import copy
from datetime import timedelta

import pytest

from tests.test_feed_analytics import NOW, publication


def test_horizon_preserves_actual_alternation_cooldown_and_deterministic_registry_themes():
    from src.editorial_horizon import build_editorial_horizon
    history = {'publications': [publication(age=1)]}
    before = copy.deepcopy(history)
    plan = build_editorial_horizon(history, now=NOW, weeks=2, seed='fixed')
    assert plan == build_editorial_horizon(history, now=NOW, weeks=2, seed='fixed')
    assert plan['slots'][0]['earliest_at'] == (NOW + timedelta(hours=47)).isoformat()
    assert [row['publication_format'] for row in plan['slots'][:4]] == ['carousel', 'single', 'carousel', 'single']
    assert all(row['provisional'] and row['publication_ready'] is False for row in plan['slots'])
    assert plan['slots'][0]['theme_id'] == plan['slots'][1]['theme_id']
    assert plan['slots'][0]['editorial_format'] is not None
    assert plan['slots'][1]['editorial_format'] is None
    assert history == before


@pytest.mark.parametrize('weeks', [0, 1, 3, True, 2.0])
def test_horizon_rejects_unsupported_horizons(weeks):
    from src.editorial_horizon import build_editorial_horizon
    with pytest.raises(ValueError):
        build_editorial_horizon({}, now=NOW, weeks=weeks, seed='fixed')


def test_horizon_blocks_ambiguous_history_and_labels_cold_start_as_unprepared():
    from src.editorial_horizon import build_editorial_horizon
    pub = publication()
    with pytest.raises(ValueError):
        build_editorial_horizon({'publications': [pub, pub]}, now=NOW, weeks=2, seed='fixed')
    plan = build_editorial_horizon({}, now=NOW, weeks=4, seed='fixed')
    assert len(plan['slots']) == 14
    assert plan['prepared_packages'] == 0
    assert plan['slots'][0]['publication_format'] == 'carousel'


def test_horizon_cli_produces_exclusive_offline_review_artifacts(tmp_path):
    from scripts.plan_editorial import run
    source = tmp_path / 'history.json'
    source.write_text('{"publications": []}')
    output = tmp_path / 'plan'
    args = ['--history', str(source), '--now', NOW.isoformat(), '--output', str(output)]
    assert run(args) == 0
    assert (output / 'plan.json').is_file() and 'Editoryal plan' in (output / 'index.html').read_text()
    assert run(args) == 1
    assert source.read_text() == '{"publications": []}'
