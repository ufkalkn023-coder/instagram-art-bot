import logging
import random

import pytest

from src.carousel_themes import (
    MAX_SEASONAL_BOOST,
    CarouselFormat,
    CarouselThemeDefinition,
    CarouselThemeRegistry,
    ThemeFamily,
    ThemeHistorySlot,
    ThemeRegistryError,
    get_default_theme_registry,
    parse_theme_registry,
    plan_carousel_theme,
    primary_search_query,
    rank_carousel_themes,
    score_theme,
)


def _theme(
    theme_id: str,
    *,
    family: ThemeFamily = ThemeFamily.SUBJECT,
    seasonal_months=(),
    enabled: bool = True,
    editorial_priority: float = 0,
) -> CarouselThemeDefinition:
    return CarouselThemeDefinition(
        id=theme_id,
        title=theme_id.replace("_", " ").title(),
        family=family,
        format=CarouselFormat.THEMATIC_COLLECTION,
        description=f"A focused editorial definition for {theme_id.replace('_', ' ')}.",
        primary_queries=[f"{theme_id} primary", f"{theme_id} alternate"],
        secondary_queries=[f"{theme_id} secondary"],
        required_terms=[theme_id],
        preferred_terms=["preferred"],
        seasonal_months=list(seasonal_months),
        enabled=enabled,
        editorial_priority=editorial_priority,
        tags=["test"],
    )


def test_default_registry_is_large_unique_typed_and_fully_categorized():
    registry = get_default_theme_registry()

    assert len(registry.enabled_themes) >= 120
    assert len({theme.id for theme in registry.themes}) == len(registry.themes)
    assert [theme.id for theme in registry.themes] == sorted(theme.id for theme in registry.themes)
    assert {theme.family for theme in registry.themes} == set(ThemeFamily)
    assert {theme.format for theme in registry.themes} == set(CarouselFormat)
    assert all(theme.title and theme.title.isascii() for theme in registry.themes)
    assert all(theme.primary_queries for theme in registry.themes)
    assert all(len(theme.primary_queries) >= 2 for theme in registry.themes)
    assert all(theme.secondary_queries for theme in registry.themes)
    assert all(1 <= month <= 12 for theme in registry.themes for month in theme.seasonal_months)
    assert all(theme.minimum_candidate_target == 12 for theme in registry.themes)


def test_registry_rejects_duplicate_ids():
    definition = _theme("duplicate_theme").model_dump(mode="json")

    with pytest.raises(ThemeRegistryError, match="duplicate theme IDs"):
        parse_theme_registry({"themes": [definition, definition]})


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("primary_queries", "not-an-array", "array"),
        ("primary_queries", [], "primary query"),
        ("secondary_queries", ["valid", ""], "non-empty"),
        ("seasonal_months", [0, 13], "1..12"),
    ],
)
def test_registry_rejects_malformed_query_and_seasonal_arrays(field, value, match):
    definition = _theme("malformed_theme").model_dump(mode="json")
    definition[field] = value

    with pytest.raises(ThemeRegistryError, match=match):
        parse_theme_registry({"themes": [definition]})


def test_primary_search_query_is_an_explicit_registry_query():
    theme = get_default_theme_registry().by_id("women_reading")

    assert primary_search_query(theme) == "woman reading"
    assert primary_search_query(theme) in theme.primary_queries
    assert primary_search_query(theme) != theme.title


def test_planner_is_deterministic_and_does_not_mutate_global_random_state():
    registry = CarouselThemeRegistry(themes=(_theme("theme_alpha"), _theme("theme_beta")))
    random.seed(20260825)
    expected = random.random()
    random.seed(20260825)

    first = plan_carousel_theme(registry, [], run_seed="fixed", current_month=8)
    second = plan_carousel_theme(registry, [], run_seed="fixed", current_month=8)

    assert first.theme.id == second.theme.id
    assert first.score == second.score
    assert random.random() == expected


def test_rank_api_exposes_the_same_deterministic_order_used_by_selection():
    registry = CarouselThemeRegistry(
        themes=(_theme("theme_alpha"), _theme("theme_beta"), _theme("theme_gamma"))
    )

    ranked = rank_carousel_themes(registry, [], run_seed="fixed", current_month=8)
    selected = plan_carousel_theme(registry, [], run_seed="fixed", current_month=8)

    assert selected.ranked_scores == ranked
    assert selected.ranked_themes(registry) == tuple(
        registry.by_id(score.theme_id) for score in ranked
    )
    assert selected.theme == selected.ranked_themes(registry)[0]


def test_different_seeds_can_change_equal_score_serendipity_tie_breaks():
    registry = CarouselThemeRegistry(themes=(_theme("theme_alpha"), _theme("theme_beta")))

    choices = {
        plan_carousel_theme(registry, [], run_seed=f"seed-{index}", current_month=8).theme.id
        for index in range(40)
    }

    assert choices == {"theme_alpha", "theme_beta"}


def test_same_theme_repetition_has_moderate_then_strong_soft_penalties():
    theme = _theme("repeatable")
    once = score_theme(
        theme,
        [ThemeHistorySlot("repeatable", ThemeFamily.SUBJECT)],
        run_seed="fixed",
        current_month=8,
    )
    twice = score_theme(
        theme,
        [ThemeHistorySlot("repeatable", ThemeFamily.SUBJECT)] * 2,
        run_seed="fixed",
        current_month=8,
    )

    assert once.theme_fatigue < 0
    assert twice.theme_fatigue < once.theme_fatigue
    assert twice.total < once.total


def test_family_fatigue_crosses_theme_ids_without_creating_theme_fatigue():
    candidate = _theme("subject_three", family=ThemeFamily.SUBJECT)
    history = [
        ThemeHistorySlot("subject_one", ThemeFamily.SUBJECT),
        ThemeHistorySlot("subject_two", ThemeFamily.SUBJECT),
    ]

    score = score_theme(candidate, history, run_seed="fixed", current_month=8)

    assert score.theme_fatigue == 0
    assert score.family_fatigue < 0


def test_family_fatigue_strengthens_but_remains_soft():
    theme = _theme("new_maritime", family=ThemeFamily.MARITIME)
    two = score_theme(
        theme,
        [ThemeHistorySlot(f"sea_{index}", ThemeFamily.MARITIME) for index in range(2)],
        run_seed="fixed",
        current_month=8,
    )
    five = score_theme(
        theme,
        [ThemeHistorySlot(f"sea_{index}", ThemeFamily.MARITIME) for index in range(5)],
        run_seed="fixed",
        current_month=8,
    )

    assert five.family_fatigue < two.family_fatigue < 0
    assert five.total > 0


def test_seasonal_boost_is_bounded_and_never_an_eligibility_requirement():
    winter = _theme("winter_theme", seasonal_months=(12, 1, 2))
    nonseasonal = _theme("always_theme")

    winter_score = score_theme(winter, [], run_seed="fixed", current_month=1)
    summer_score = score_theme(winter, [], run_seed="fixed", current_month=7)
    only_nonseasonal = plan_carousel_theme(
        CarouselThemeRegistry(themes=(nonseasonal,)),
        [],
        run_seed="fixed",
        current_month=1,
    )

    assert winter_score.seasonal == MAX_SEASONAL_BOOST
    assert summer_score.seasonal == 0
    assert winter_score.total - summer_score.total == MAX_SEASONAL_BOOST
    assert only_nonseasonal.theme.id == "always_theme"


def test_disabled_theme_is_never_selected_even_with_high_priority():
    registry = CarouselThemeRegistry(
        themes=(
            _theme("disabled_theme", enabled=False, editorial_priority=5),
            _theme("enabled_theme", editorial_priority=-5),
        )
    )

    selection = plan_carousel_theme(registry, [], run_seed="fixed", current_month=8)

    assert selection.theme.id == "enabled_theme"


def test_planner_logs_explainable_selected_score_and_eligibility(caplog):
    caplog.set_level(logging.INFO, logger="src.carousel_themes")
    registry = CarouselThemeRegistry(themes=(_theme("logged_theme"),))

    plan_carousel_theme(registry, [], run_seed="fixed", current_month=8)

    assert "theme_eligibility enabled=1 disabled=0 recent_publication_slots=0" in caplog.text
    assert "theme_selected id=logged_theme family=subject" in caplog.text
    assert "fatigue=" in caplog.text
    assert "serendipity=" in caplog.text
