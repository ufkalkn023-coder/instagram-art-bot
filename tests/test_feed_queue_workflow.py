from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_queue_consumption_requires_explicit_opt_in_and_keeps_authoritative_flow():
    workflow = yaml.safe_load((ROOT / '.github/workflows/instagram_bot.yml').read_text())
    publish = next(step for step in workflow['jobs']['post-to-instagram']['steps']
                   if step['name'] == 'Fetch artwork, process image, and post to Instagram')
    assert publish['env']['ARTFOLIO_FEED_QUEUE_ENABLED'] == '${{ vars.ARTFOLIO_FEED_QUEUE_ENABLED }}'
    assert publish['run'] == 'python main.py --mode auto'


def test_queue_preparation_is_manual_opt_in_without_instagram_publication_credentials():
    workflow = yaml.safe_load((ROOT / '.github/workflows/prepare_feed_queue.yml').read_text())
    assert 'schedule' not in workflow['on']
    job = workflow['jobs']['prepare']
    assert 'PREPARE_FEED_QUEUE' in job['if']
    assert workflow['concurrency']['cancel-in-progress'] is False
    step = next(item for item in job['steps'] if '--r2' in item.get('run', ''))
    assert '--skip-keychain' in step['run']
    assert step['env']['ARTFOLIO_RIGHTS_POLICY'] == 'strict_public_domain'
    assert 'INSTAGRAM_ACCESS_TOKEN' not in step['env']
    assert 'CLOUDFLARE_STATE_R2_ACCESS_KEY_ID' in step['env']
