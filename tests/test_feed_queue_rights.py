import importlib
from types import SimpleNamespace

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
