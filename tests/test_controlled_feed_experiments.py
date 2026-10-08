import importlib

import pytest

from src.carousel_themes import get_default_theme_registry
from src.models import PublicationRecord
from tests.test_feed_analytics import NOW, publication, snapshot


def module():
    return importlib.import_module('src.editorial_experiments')


def test_hook_assignment_is_deterministic_and_changes_one_existing_variable():
    theme = get_default_theme_registry().themes[0]
    assert hasattr(module(), 'assign_caption_experiment')
    first = module().assign_caption_experiment(theme, run_seed='seed')
    assert first == module().assign_caption_experiment(theme, run_seed='seed')
    assert first.variable == 'caption_hook_type'
    assert first.variant in {'question', 'visual_detail'}
    assert first.cover_variant == 'editorial'


def test_delivered_hook_is_grounded_deterministic_and_retains_fixed_body():
    theme = get_default_theme_registry().themes[0]
    assert hasattr(module(), 'assign_caption_experiment')
    assignment = module().assign_caption_experiment(theme, run_seed='seed')
    intro = module().controlled_caption_intro(assignment, featured_count=6, body='Fixed factual body.')
    assert intro.endswith('Fixed factual body.')
    assert '6 works' in intro
    with pytest.raises(ValueError):
        module().controlled_caption_intro(assignment, featured_count=0, body='Body')


def test_experiment_assignment_survives_publication_model_roundtrip():
    assignment = {'experiment_id': 'caption_hook_v1', 'variable': 'caption_hook_type', 'variant': 'question',
                  'theme_id': 'flowers', 'cover_variant': 'editorial'}
    pub = PublicationRecord.model_validate({**publication(format_name='carousel'), 'controlled_experiment': assignment,
                                           'carousel_theme': 'flowers', 'caption_hook_type': 'question', 'cover_variant': 'editorial'})
    assert pub.model_dump()['controlled_experiment'] == assignment


def test_comparison_keeps_theme_format_age_and_missing_metric_separate():
    report_module = importlib.import_module('src.feed_experiment_report')
    pubs, attempts = [], []
    for index, (variant, format_name, theme) in enumerate([
        ('question', 'carousel', 'flowers'), ('visual_detail', 'carousel', 'flowers'),
        ('question', 'carousel', 'sea'), ('question', 'single', 'flowers')]):
        pub = {**publication(index, format_name), 'carousel_theme': theme, 'caption_hook_type': variant,
               'cover_variant': 'editorial', 'controlled_experiment': {
            'experiment_id': 'caption_hook_v1', 'variable': 'caption_hook_type',
            'variant': variant, 'theme_id': theme, 'cover_variant': 'editorial'}}
        pubs.append(pub)
        attempts.append(snapshot(pub, metrics={'reach': 100, 'saved': 0}))
    report = report_module.build_experiment_report({'publications': pubs}, attempts, now=NOW)
    cohort = next(c for c in report['comparisons'] if c['theme_id'] == 'flowers' and c['target_age_hours'] == 72)
    assert cohort['status'] == 'insufficient_evidence'
    assert cohort['arms']['question']['save_rate']['observations'] == 1
    assert cohort['arms']['question']['like_rate']['observations'] == 0
    assert cohort['arms']['question']['save_rate']['mean_rate'] == 0
    assert cohort['winner'] is None


def test_mature_cohorts_disclose_uncertainty_without_promoting_winner():
    report_module = importlib.import_module('src.feed_experiment_report')
    pubs, attempts = [], []
    for index in range(10):
        pub = {**publication(index, 'carousel'), 'carousel_theme': 'flowers',
               'caption_hook_type': 'question' if index < 5 else 'visual_detail', 'cover_variant': 'editorial', 'controlled_experiment': {
            'experiment_id': 'caption_hook_v1', 'variable': 'caption_hook_type',
            'variant': 'question' if index < 5 else 'visual_detail', 'theme_id': 'flowers', 'cover_variant': 'editorial'}}
        pubs.append(pub)
        attempts.append(snapshot(pub, metrics={'reach': 100, 'saved': index}))
    report = report_module.build_experiment_report({'publications': pubs}, attempts, now=NOW)
    row = next(c for c in report['comparisons'] if c['target_age_hours'] == 72)
    assert row['status'] == 'descriptive_comparison'
    assert row['arms']['question']['save_rate']['standard_error'] > 0
    assert row['winner'] is None and report['auto_promote'] is False
