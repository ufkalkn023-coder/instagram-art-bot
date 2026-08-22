import pytest
from src import content_diversity
from src.models import NormalizedArtwork

def test_extract_century():
    assert content_diversity._extract_century("c. 1850") == "1800s"
    assert content_diversity._extract_century("1532") == "1500s"
    assert content_diversity._extract_century("unknown") == "unknown"
    assert content_diversity._extract_century(None) == "unknown"

def test_infer_visual_category():
    assert content_diversity._infer_visual_category("Self-portrait", "Painting") == "portrait"
    assert content_diversity._infer_visual_category("View of a Valley", "Drawing") == "landscape"
    assert content_diversity._infer_visual_category("Vase with Flowers", "Painting") == "still_life"
    assert content_diversity._infer_visual_category("Madonna and Child", "Sculpture") == "religious"
    assert content_diversity._infer_visual_category("Random Title", "Object") == "other"

def test_infer_medium_category():
    assert content_diversity._infer_medium_category("Oil on canvas", "Painting") == "painting"
    assert content_diversity._infer_medium_category("Bronze", "Sculpture") == "sculpture"
    assert content_diversity._infer_medium_category("Pen and ink", "Drawing") == "drawing"
    assert content_diversity._infer_medium_category("Unknown medium", "Object") == "other"

def test_museum_diversity_three_recent_appearances_uses_current_tier():
    history = [
        {"museum_name": "Met"},
        {"museum_name": "Met"},
        {"museum_name": "Met"}
    ]
    penalty = content_diversity.analyze_museum_diversity("Met", history)
    assert penalty == -7.0

    penalty2 = content_diversity.analyze_museum_diversity("Cleveland", history)
    assert penalty2 == 0.0


@pytest.mark.parametrize(
    ("repeats", "expected_penalty"),
    [
        (0, 0.0),
        (1, -1.0),
        (2, -3.0),
        (3, -7.0),
        (4, -7.0),
        (5, -22.0),
        (10, -22.0),
    ],
)
def test_museum_diversity_penalty_tiers(repeats, expected_penalty):
    history = [{"museum_name": "Met"}] * repeats

    assert content_diversity.analyze_museum_diversity("Met", history) == expected_penalty


def test_museum_diversity_uses_only_the_last_ten_history_records():
    history = [{"museum_name": "Met"}] + [{"museum_name": "Cleveland"}] * 10

    assert content_diversity.analyze_museum_diversity("Met", history) == 0.0


@pytest.mark.parametrize("candidate_museum", ["", "unknown"])
def test_museum_diversity_ignores_empty_or_unknown_candidate_museum(candidate_museum):
    assert content_diversity.analyze_museum_diversity(candidate_museum, [{"museum_name": "Met"}]) == 0.0


def test_museum_diversity_uses_exact_museum_name_matching():
    history = [{"museum_name": "Met"}] * 3

    assert content_diversity.analyze_museum_diversity("met", history) == 0.0

def test_visual_diversity():
    history = [
        {"artist_name": "Vincent van Gogh", "visual_category": "portrait", "period": "1800s", "medium": "painting"},
        {"artist_name": "Vincent van Gogh", "visual_category": "portrait", "period": "1800s", "medium": "painting"}
    ]
    features = {
        "artist_name": "Vincent van Gogh",
        "visual_category": "portrait",
        "period": "1800s",
        "medium": "painting"
    }
    score = content_diversity.analyze_visual_diversity(features, history)
    # artist (-5), visual (-5), period (0 because count < 3), medium (0 because count < 3)
    assert score < 0.0

    fresh_features = {
        "artist_name": "Claude Monet",
        "visual_category": "landscape",
        "period": "1800s",
        "medium": "painting"
    }
    score2 = content_diversity.analyze_visual_diversity(fresh_features, history)
    # landscape is fresh (+3)
    assert score2 > 0.0


def test_discovery_rewards_high_quality_artist_novelty_relative_to_history():
    features = {
        "artist_name": "Hilma af Klint",
        "visual_category": "other",
        "period": "1900s",
        "medium": "painting",
    }
    history = [{"artist_name": "Claude Monet"}]

    assert content_diversity.analyze_discovery_score(features, 80, history) == 2.0
    assert content_diversity.analyze_discovery_score(features, 79.99, history) == 0.0
    assert content_diversity.analyze_discovery_score(features, 80, [{"artist_name": " Hilma   af Klint "}]) == 0.0


def test_discovery_does_not_reward_unknown_or_missing_artist_metadata():
    for artist_name in (None, "", "   ", "unknown", "Unknown Artist"):
        assert content_diversity.analyze_discovery_score({"artist_name": artist_name}, 100, []) == 0.0


def test_discovery_is_deterministic_and_does_not_double_count_recent_artist_diversity():
    features = {
        "artist_name": "Vincent van Gogh",
        "visual_category": "portrait",
        "period": "1800s",
        "medium": "painting",
    }
    history = [
        {"artist_name": "Vincent van Gogh", "visual_category": "portrait", "period": "1800s", "medium": "painting"}
    ]

    first = content_diversity.analyze_discovery_score(features, 90, history)
    second = content_diversity.analyze_discovery_score(features, 90, history)

    assert first == second == 0.0
    assert content_diversity.analyze_visual_diversity(features, history) < 0.0


def test_discovery_bonus_has_a_bounded_two_point_maximum():
    assert content_diversity.analyze_discovery_score({"artist_name": "New Artist"}, 100, []) == 2.0

def test_select_content_type():
    history = [
        {"content_type": "SINGLE_ARTWORK"},
        {"content_type": "ARTIST_FOCUS"}
    ]
    chosen = content_diversity.select_content_type(history)
    assert chosen not in ["SINGLE_ARTWORK", "ARTIST_FOCUS"]
