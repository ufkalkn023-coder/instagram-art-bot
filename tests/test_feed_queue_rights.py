import importlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from src.museums import aic, cleveland, rijksmuseum
from tests.test_rijksmuseum import _install_router, _resource_set, _search_page

from tests.test_feed_queue import content


def test_fresh_rights_require_exact_identity_not_matching_title(tmp_path):
    prepared = content(tmp_path, 'single', 1)
    module = importlib.import_module('src.feed_queue_rights')
    def adapter(identifier, public=True):
        candidate = SimpleNamespace(canonical_id=identifier, is_public_domain=public,
                                    rights_status='CONFIRMED_PUBLIC_DOMAIN')
        return SimpleNamespace(source_id='aic', fetch_candidates=lambda **kwargs: [candidate])
    assert not module.revalidate_source_rights(prepared, adapters=[adapter('aic_other')])
    assert not module.revalidate_source_rights(prepared, adapters=[adapter('aic_1-0', False)])
    assert module.revalidate_source_rights(prepared, adapters=[adapter('aic_1-0')])


def test_unavailable_source_does_not_reconfirm_stored_rights(tmp_path):
    prepared = content(tmp_path, 'single', 1)
    module = importlib.import_module('src.feed_queue_rights')
    assert not module.revalidate_source_rights(prepared, adapters=[])


def test_rights_search_is_bounded_and_source_exception_rejects(tmp_path):
    prepared = content(tmp_path, 'single', 1)
    module = importlib.import_module('src.feed_queue_rights')
    seen = []
    def fetch(**kwargs):
        seen.append(kwargs)
        raise TimeoutError('network')
    adapter = SimpleNamespace(source_id='aic', fetch_candidates=fetch)
    assert not module.revalidate_source_rights(prepared, adapters=[adapter])
    assert len(seen) == 1 and seen[0]['limit'] == 20
    assert seen[0]['verification_id'] == '1-0'


@pytest.mark.parametrize('source', ['aic', 'cleveland'])
def test_narrow_title_rights_search_keeps_the_first_matching_result(tmp_path, monkeypatch, source):
    prepared = content(tmp_path, 'single', 1)
    prepared.artworks[0].update(id=f'{source}_123', title='Unique painting')
    module = aic if source == 'aic' else cleveland
    artwork = {'id': 123, 'title': 'Unique painting', 'image_id': 'image',
               'classification_title': 'Painting', 'is_public_domain': True,
               'type': 'Painting', 'share_license_status': 'CC0',
               'images': {'web': {'url': 'https://images.example/123.jpg'}}}
    requests = []

    def get(url, **kwargs):
        params = parse_qs(urlparse(url).query)
        requests.append(params)
        first_page = (params.get('page') == ['1'] if source == 'aic'
                      else params.get('skip') == ['0'])
        return SimpleNamespace(status_code=200, headers={},
                               json=lambda: {'data': [artwork] if first_page else []})

    monkeypatch.setattr(module.requests, 'get', get)
    adapter = module.AICAdapter() if source == 'aic' else module.ClevelandAdapter()
    rights = importlib.import_module('src.feed_queue_rights')
    assert rights.revalidate_source_rights(prepared, adapters=[adapter])
    assert len(requests) == 1
    assert requests[0]['limit'] == ['20']


def test_rijksmuseum_rights_search_uses_object_number_and_fresh_resolved_rights(tmp_path, monkeypatch):
    prepared = content(tmp_path, 'single', 1)
    prepared.artworks[0].update(id='rijksmuseum_SK-A-1', title='English Title')
    resources = _resource_set('1001', rights='http://creativecommons.org/publicdomain/mark/1.0/')
    calls = _install_router(monkeypatch, [_search_page('1001')], resources)
    rights = importlib.import_module('src.feed_queue_rights')
    assert rights.revalidate_source_rights(prepared, adapters=[rijksmuseum.RijksmuseumAdapter()])
    assert calls[0][1]['params']['objectNumber'] == 'SK-A-1'
    assert 'description' not in calls[0][1]['params']
