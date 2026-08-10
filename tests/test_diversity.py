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

def test_museum_diversity():
    history = [
        {"museum_name": "Met"},
        {"museum_name": "Met"},
        {"museum_name": "Met"}
    ]
    penalty = content_diversity.analyze_museum_diversity("Met", history)
    assert penalty == -10.0 # Heavy penalty for 3 times

    penalty2 = content_diversity.analyze_museum_diversity("Cleveland", history)
    assert penalty2 == 0.0 # Fresh museum

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

def test_select_content_type():
    history = [
        {"content_type": "SINGLE_ARTWORK"},
        {"content_type": "ARTIST_FOCUS"}
    ]
    chosen = content_diversity.select_content_type(history)
    assert chosen not in ["SINGLE_ARTWORK", "ARTIST_FOCUS"]
