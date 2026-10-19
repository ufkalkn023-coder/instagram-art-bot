from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_regular_feed_pins_editorial_stock_and_keeps_authoritative_flow():
    workflow = yaml.safe_load((ROOT / '.github/workflows/instagram_bot.yml').read_text())
    publish = next(step for step in workflow['jobs']['post-to-instagram']['steps']
                   if step['name'] == 'Fetch artwork, process image, and post to Instagram')
    assert publish['env']['ARTFOLIO_FEED_QUEUE_ENABLED'] == 'true'
    assert publish['env']['ARTFOLIO_FEED_CONTENT_MODE'] == 'editorial'
    assert publish['run'] == 'python main.py --mode auto'


def test_editorial_stock_workflow_is_read_only_and_never_assumes_local_paths_exist_on_runner():
    workflow = yaml.safe_load((ROOT / '.github/workflows/prepare_feed_queue.yml').read_text())
    assert workflow['on']['schedule']
    job = workflow['jobs']['prepare']
    assert 'CHECK_EDITORIAL_FEED' in job['if']
    assert 'ARTFOLIO_FEED_QUEUE_REFILL_ENABLED' in job['if']
    assert 'ARTFOLIO_FEED_QUEUE_ENABLED' in job['if']
    assert 'ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED' in job['if']
    assert workflow['concurrency']['cancel-in-progress'] is False
    check = next(item for item in job['steps'] if '--check-refill' in item.get('run', ''))
    assert check['id'] == 'refill'
    assert '--github-output' in check['run']
    assert check['env']['ARTFOLIO_FEED_CONTENT_MODE'] == 'editorial'
    assert 'if' not in check  # Manual and scheduled calls both check stock only.
    assert 'editorial_workspace' not in workflow['on']['workflow_dispatch']['inputs']
    for item in job['steps']:
        assert not any('INSTAGRAM' in name for name in item.get('env', {}))
        assert '--editorial-workspace' not in item.get('run', '')
        assert '--refill ' not in item.get('run', '')
        assert not any(name in item.get('env', {}) for name in ('GOOGLE_GEMINI_API_KEY', 'SMITHSONIAN_API_KEY', 'EUROPEANA_API_KEY'))
