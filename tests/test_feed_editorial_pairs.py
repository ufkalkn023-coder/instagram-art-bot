import importlib
from dataclasses import replace

from src.carousel_themes import get_default_theme_registry
from tests.test_feed_queue import content


def planner():
    return importlib.import_module('src.feed_editorial').FeedPairPlanner()


def test_pair_uses_actual_carousel_theme_and_excludes_previous_artworks(tmp_path):
    plan = planner()
    theme = get_default_theme_registry().themes[0]
    seen = []
    def prepare(format_name, directory, excluded, theme_definition=None):
        seen.append((format_name, excluded, theme_definition))
        result = content(directory, format_name, len(seen))
        return replace(result, theme_id=theme.id, theme_family=theme.family.value) if format_name == 'carousel' else result
    anchor = plan.prepare('carousel', tmp_path, set(), prepare=prepare)
    follow = plan.prepare('single', tmp_path, set(anchor.publication_ids), prepare=prepare)
    pair = follow.publication_metadata['editorial_pair']
    assert pair['pair_id'] == anchor.publication_metadata['editorial_pair']['pair_id']
    assert pair['role'] == 'followup' and pair['status'] == 'matched'
    assert seen[1][2].id == theme.id
    assert seen[1][1] == set(anchor.publication_ids)


def test_unknown_theme_and_single_first_do_not_invent_relationship(tmp_path):
    plan = planner()
    def prepare(format_name, directory, excluded, theme_definition=None):
        return replace(content(directory, format_name, 1), theme_id='missing')
    assert not plan.prepare('single', tmp_path, set(), prepare=prepare).publication_metadata.get('editorial_pair')
    anchor = plan.prepare('carousel', tmp_path, set(), prepare=prepare)
    follow = plan.prepare('single', tmp_path, set(anchor.publication_ids), prepare=prepare)
    assert not follow.publication_metadata.get('editorial_pair')


def test_unavailable_themed_single_falls_back_without_claiming_matched_pair(tmp_path):
    plan = planner()
    theme = get_default_theme_registry().themes[0]
    module = importlib.import_module('src.feed_editorial')
    seen = []
    def prepare(format_name, directory, excluded, theme_definition=None):
        seen.append(theme_definition)
        if theme_definition is not None:
            raise module.ThematicSingleUnavailable('no compatible candidate')
        return replace(content(directory, format_name, len(seen)), theme_id=theme.id)
    plan.prepare('carousel', tmp_path, set(), prepare=prepare)
    follow = plan.prepare('single', tmp_path, set(), prepare=prepare)
    assert follow.publication_metadata['editorial_pair']['status'] == 'unmatched'
    assert follow.publication_metadata['editorial_pair']['role'] == 'followup'
    assert seen[-1] is None


def test_single_editorial_pair_survives_reservation_and_reconciliation(monkeypatch):
    from tests.test_feed_schedule import setup_runtime, auth
    from tests.test_single_feed_reservation import artwork
    from src import history_tracker
    setup_runtime(monkeypatch)
    pair = {'pair_id': 'a' * 32, 'theme_id': 'flowers', 'role': 'followup', 'status': 'matched'}
    identifier = history_tracker.reserve_single_publication(
        artwork(), authorization=auth(), publication_metadata={'editorial_pair': pair})
    history_tracker.start_publication_attempt(['cleveland_990001'], 'parent', expected_publication_id=identifier,
                                             authorization=auth())
    pub = history_tracker.confirm_artworks_and_record_publication(
        ['cleveland_990001'], 'media', 'single', publication_id=identifier)
    assert pub['editorial_pair'] == pair


def test_later_carousel_excludes_the_actual_earlier_theme(tmp_path):
    plan = planner()
    themes = get_default_theme_registry().enabled_themes
    seen = []
    def prepare(format_name, directory, excluded, theme_definition=None, excluded_theme_ids=None):
        seen.append(excluded_theme_ids or set())
        theme = next(theme for theme in themes if theme.id not in (excluded_theme_ids or set()))
        return replace(content(directory, format_name, len(seen)), theme_id=theme.id)
    first = plan.prepare('carousel', tmp_path, set(), prepare=prepare)
    plan.prepare('single', tmp_path, set(), prepare=prepare)
    last = plan.prepare('carousel', tmp_path, set(), prepare=prepare)
    assert first.theme_id in seen[-1]
    assert first.theme_id != last.theme_id
