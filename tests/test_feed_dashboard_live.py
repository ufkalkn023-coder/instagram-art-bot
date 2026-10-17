import base64
import hashlib
import http.client
import io
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from src.feed_dashboard import render_feed_dashboard

NOW = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)


def package(identifier, format_name='single', **changes):
    return {'id': identifier, 'title': identifier, 'state': 'READY',
            'publication_format': format_name, 'expires_at': '2026-10-28T10:00:00Z',
            'source_count': 1, 'page_count': 1, **changes}


def schedule(**changes):
    return {'status': 'WAITING_COOLDOWN', 'next_format': 'single',
            'next_eligible_at': '2026-10-18T07:14:29Z', **changes}


def test_calendar_preserves_order_seconds_and_conditional_48h_spacing():
    from src.feed_calendar import build_feed_calendar
    rows = build_feed_calendar(schedule(), [package('one'), package('two', 'carousel')], now=NOW)
    assert [r['id'] for r in rows] == ['one', 'two']
    assert rows[0]['earliest_at'] == datetime(2026, 10, 18, 7, 14, 29, tzinfo=timezone.utc)
    assert rows[1]['earliest_at'] == rows[0]['earliest_at'] + timedelta(hours=48)
    assert all(r['provisional'] for r in rows)


@pytest.mark.parametrize('state', ['CLAIMED', 'QUARANTINED'])
def test_uncertain_ownership_prevents_calendar_dates(state):
    from src.feed_calendar import build_feed_calendar
    rows = build_feed_calendar(schedule(), [package('one', state=state), package('two', 'carousel')], now=NOW)
    assert all(r['earliest_at'] is None for r in rows)


def test_calendar_does_not_date_expired_mismatched_or_paused_packages():
    from src.feed_calendar import build_feed_calendar
    rows = build_feed_calendar(schedule(), [package('old', expires_at='2026-10-10T00:00:00Z'),
        package('wrong', 'carousel'), package('one'), package('used', state='CONSUMED')], now=NOW)
    assert [r['id'] for r in rows] == ['old', 'wrong', 'one']
    assert [r['earliest_at'] is None for r in rows] == [True, True, False]
    paused = build_feed_calendar(schedule(status='SHA_MISMATCH'), [package('one')], now=NOW)
    assert paused[0]['earliest_at'] is None


def test_calendar_renderer_shows_cards_safe_images_and_provisional_copy():
    page = render_feed_dashboard(schedule(), [package('one', thumbnail='https://evil.example/track')],
                                 {'publications': []}, [], now=NOW)
    assert 'İçerik takvimi' in page
    assert '2026-10-18 10:14:29 +03' in page
    assert 'Önizleme yok' in page
    assert 'https://evil.example/track' not in page
    assert 'önceki yayınların başarılı tamamlanmasına bağlıdır' in page
    assert 'id="refresh"' in page


def test_thumbnail_checks_bytes_and_preserves_source():
    import scripts.report_feed_dashboard as cli
    original = io.BytesIO()
    Image.new('RGB', (1080, 1350), 'green').save(original, 'JPEG')
    data = original.getvalue()
    uri = cli._thumbnail(data, hashlib.sha256(data).hexdigest())
    assert uri.startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1]))) as preview:
        assert max(preview.size) <= 320
    assert cli._thumbnail(data, '0' * 64) is None
    assert original.getvalue() == data



def test_oversized_image_header_keeps_queue_with_placeholder(monkeypatch):
    import scripts.report_feed_dashboard as cli
    original = io.BytesIO()
    Image.new('RGB', (20, 20), 'green').save(original, 'JPEG')
    data = original.getvalue()
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)
    rows = cli._queue_details([package('one')],
        thumbnail_loader=lambda item: cli._thumbnail(data, hashlib.sha256(data).hexdigest()))
    assert len(rows) == 1
    assert rows[0]['id'] == 'one'
    assert rows[0]['thumbnail'] is None


def test_story_comparison_uses_one_parent_and_real_cohort_rates():
    delivery = {'schema_version': 'artfolio-story-delivery-v1', 'reviewed_revision': 1,
        'reviewed_digest': 'a'*64, 'source_ids': ['aic_1'], 'source_sha256': {'aic_1':'b'*64},
        'artwork_sha256': {'aic_1':'c'*64}, 'caption_sha256':'d'*64,
        'narrative':'single_study','cover_variant':'artwork_first','headline_kind':'source_title',
        'public_title':'Study','theme_id':'study','theme_title':'Study',
        'pages':[{'id':name,'role':role,'artwork_ids':['aic_1'],'sha256':'e'*64}
                 for name,role in [('cover','cover'),('art','artwork'),('detail','detail'),('close','closing')]]}
    posted = NOW - timedelta(hours=80)
    history = {'publications':[{'id':'p','media_id':'m','type':'carousel','artwork_ids':['aic_1'],
                               'posted_at':posted.isoformat(),'story_delivery':delivery}]}
    snapshots=[{'publication_id':'p','media_id':'m','target_age_hours':72,
                'captured_at':(posted+timedelta(hours=72)).isoformat(),'metrics':{'reach':10,'saved':2}}]
    page=render_feed_dashboard({}, [], history, snapshots, now=NOW)
    assert 'Hikâye karşılaştırmaları' in page
    assert 'Tek eser incelemesi' in page
    assert '20.0% · 1 gözlem' in page
    assert '1 kullanılabilir / 1 uygun yayın' in page
    assert 'Örneklem yetersiz' in page


@contextmanager
def running(loader, clock=lambda: 0):
    from src.feed_dashboard_server import dashboard_server
    server=dashboard_server(loader, port=0, clock=clock)
    thread=threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def request(server, method='GET', path='/', headers=None, body=None):
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
    connection.request(method,path,body=body,headers=headers or {})
    response=connection.getresponse()
    result=(response.status,dict(response.getheaders()),response.read().decode())
    connection.close()
    return result


def page_loader(label='snapshot'):
    return f'<html><h1>{label}</h1><script id="dashboard-config" type="application/json">null</script></html>'


def headers(server, page):
    config=json.loads(re.search(r'<script id="dashboard-config" type="application/json">(.*?)</script>',page).group(1))
    return {'Origin':f'http://127.0.0.1:{server.server_port}','X-Artfolio-Dashboard':config['token'],
            'Content-Type':'application/json'}


def test_server_refresh_reloads_data_and_preserves_read_only_routes():
    calls=[]
    def load():
        calls.append(1)
        return page_loader(f'snapshot-{len(calls)}')
    with running(load) as server:
        assert server.server_address[0]=='127.0.0.1'
        status, response_headers, page=request(server)
        assert status==200 and 'snapshot-1' in page
        assert response_headers['Cache-Control']=='no-store'
        auth=headers(server,page)
        assert request(server,'POST','/api/refresh',auth,'{}')[0]==200
        assert 'snapshot-2' in request(server)[2]
        assert request(server,path='/../../etc/passwd')[0]==404
        assert request(server,'POST','/api/publish',auth,'{}')[0]==404
        assert len(calls)==2


@pytest.mark.parametrize('change', [
    {'Host':'evil.example'}, {'Origin':'https://evil.example'}, {'Origin':''},
    {'X-Artfolio-Dashboard':''}, {'X-Artfolio-Dashboard':'wrong'},
])
def test_refresh_rejects_cross_origin_or_invalid_session(change):
    calls=[]
    def load():
        calls.append(1)
        return page_loader()
    with running(load) as server:
        page=request(server)[2]
        auth=headers(server,page)
        auth.update(change)
        assert request(server,'POST','/api/refresh',auth,'{}')[0]==403
        assert len(calls)==1
        assert request(server,headers={'Host':'evil.example'})[0]==403


def test_failed_refresh_keeps_last_good_page_and_hides_internal_errors():
    calls=[]
    def load():
        calls.append(1)
        if len(calls)>1:
            raise RuntimeError('DO-NOT-LEAK-private-config')
        return page_loader('last-good')
    with running(load) as server:
        page=request(server)[2]
        response=request(server,'POST','/api/refresh',headers(server,page),'{}')
        assert response[0]==503
        assert 'DO-NOT-LEAK' not in response[2]
        assert 'last-good' in request(server)[2]


def test_refresh_rate_limit_prevents_repeated_loader_calls():
    clock=[0]
    calls=[]
    def load():
        calls.append(1)
        return page_loader()
    with running(load,clock=lambda:clock[0]) as server:
        auth=headers(server,request(server)[2])
        assert request(server,'POST','/api/refresh',auth,'{}')[0]==200
        assert request(server,'POST','/api/refresh',auth,'{}')[0]==429
        assert len(calls)==2
        clock[0]=5
        assert request(server,'POST','/api/refresh',auth,'{}')[0]==200


@pytest.mark.parametrize('body, changes', [
    ('{}', {'Content-Type':'text/plain'}),
    ('{}', {'Content-Length':'65'}),
    ('[]', {}),
    ('{"path":"/etc/passwd"}', {}),
    ('not-json', {}),
])
def test_refresh_rejects_nonempty_invalid_or_oversized_requests(body, changes):
    calls=[]
    def load():
        calls.append(1)
        return page_loader()
    with running(load) as server:
        auth=headers(server,request(server)[2])
        auth.update(changes)
        assert request(server,'POST','/api/refresh',auth,body)[0]==400
        assert len(calls)==1


def test_thumbnail_cannot_read_a_path_outside_package(tmp_path):
    import scripts.report_feed_dashboard as cli
    from src.feed_queue import PreparedFeedQueue
    store=PreparedFeedQueue(tmp_path)
    item={'id':'package','content':{'assets':[{'path':'../private.jpg','sha256':'a'*64}]}}
    with pytest.raises(ValueError,match='ownership'):
        cli._package_thumbnail(store,item)
