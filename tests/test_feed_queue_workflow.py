from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_queue_consumption_requires_explicit_opt_in_and_keeps_authoritative_flow():
    workflow = yaml.safe_load((ROOT / '.github/workflows/instagram_bot.yml').read_text())
    publish = next(step for step in workflow['jobs']['post-to-instagram']['steps']
                   if step['name'] == 'Fetch artwork, process image, and post to Instagram')
    assert publish['env']['ARTFOLIO_FEED_QUEUE_ENABLED'] == '${{ vars.ARTFOLIO_FEED_QUEUE_ENABLED }}'
    assert publish['run'] == 'python main.py --mode auto'


def test_queue_preparation_keeps_manual_opt_in_and_explicit_scheduled_refill():
    workflow = yaml.safe_load((ROOT / '.github/workflows/prepare_feed_queue.yml').read_text())
    assert workflow['on']['schedule']
    job = workflow['jobs']['prepare']
    assert 'PREPARE_FEED_QUEUE' in job['if']
    assert 'ARTFOLIO_FEED_QUEUE_REFILL_ENABLED' in job['if']
    assert 'ARTFOLIO_FEED_QUEUE_ENABLED' in job['if']
    assert 'ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED' in job['if']
    assert workflow['concurrency']['cancel-in-progress'] is False
    step = next(item for item in job['steps'] if item.get('name') == 'Prepare and install validated batch')
    assert '--skip-keychain' in step['run']
    assert step['env']['ARTFOLIO_RIGHTS_POLICY'] == 'strict_public_domain'
    assert 'INSTAGRAM_ACCESS_TOKEN' not in step['env']
    assert 'CLOUDFLARE_STATE_R2_ACCESS_KEY_ID' in step['env']
    check = next(item for item in job['steps'] if '--check-refill' in item.get('run', ''))
    assert check['id'] == 'refill'
    assert '--github-output' in check['run']
    for item in job['steps']:
        assert not any('INSTAGRAM' in name for name in item.get('env', {}))
    validation = next(item for item in job['steps'] if item.get('name') == 'Verify queue implementation')
    assert "steps.refill.outputs.refill_needed == 'true'" in validation['if']
