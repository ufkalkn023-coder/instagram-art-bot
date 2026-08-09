import pytest
from src.models import NormalizedArtwork
from src.museums import AICAdapter, MetAdapter, ClevelandAdapter, RijksmuseumAdapter
from src.quality_filter import calculate_quality_score, validate_and_download_image
import os

def test_normalized_artwork():
    art = NormalizedArtwork(
        source="test",
        source_id="123",
        title="Test Painting",
        museum_name="Test Museum"
    )
    assert art.canonical_id == "test_123"
    assert art.artist_name == "Unknown Artist"
    
def test_quality_filter_scoring():
    art = NormalizedArtwork(
        source="test",
        source_id="1",
        title="Valid Title",
        artist_name="Valid Artist",
        creation_date="1900",
        medium="Oil on canvas",
        museum_name="Test Museum",
        image_width=2000,
        image_height=2000
    )
    score = calculate_quality_score(art, museum_weights={"test": 15})
    # Perfect metadata (25), great image res (40), good source (15), perfect ratio (20)
    assert score == 100

    art_bad = NormalizedArtwork(
        source="test",
        source_id="2",
        title="Untitled",
        artist_name="Unknown",
        creation_date="Unknown",
        medium=None,
        museum_name="Test Museum",
        image_width=100,
        image_height=300
    )
    score_bad = calculate_quality_score(art_bad, museum_weights={"test": 15})
    # Bad metadata (0), low res (15), good source (15), extreme ratio (10)
    assert score_bad == 40
    
def test_image_validator_invalid_url():
    assert validate_and_download_image("not a url", "test.jpg") == False
    assert validate_and_download_image("http://invalid.domain.that.does.not.exist.com/image.jpg", "test.jpg") == False
    if os.path.exists("test.jpg"):
        os.remove("test.jpg")

def test_adapters_instantiation():
    adapters = [AICAdapter(), MetAdapter(), ClevelandAdapter(), RijksmuseumAdapter()]
    assert len(adapters) == 4
    for a in adapters:
        assert isinstance(a.source_id, str)
