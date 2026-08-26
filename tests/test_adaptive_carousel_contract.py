import json
from types import SimpleNamespace

import pytest

from src import gemini_ai
from src.carousel_plan import CarouselPlan, CoverAsset, CoverMode, CoverScoreBreakdown
from src.carousel_themes import get_default_theme_registry


def _artwork(index: int) -> dict:
    return {
        "id": f"work_{index}",
        "title": f"Title {index}",
        "artist": f"Artist {index}",
        "date": str(1800 + index),
        "museum": f"Museum {index}",
    }


def _cover() -> CoverAsset:
    breakdown = CoverScoreBreakdown(10, 10, 10, 10, 10, 10, 10)
    artwork = _artwork(99)
    artwork["id"] = "distinct_cover"
    return CoverAsset(
        artwork=artwork,
        local_image_path="cover.jpg",
        mode=CoverMode.FULL_ARTWORK,
        cover_score=breakdown.total,
        score_breakdown=breakdown,
    )


@pytest.mark.parametrize("featured_count", range(3, 9))
def test_carousel_plan_accepts_every_adaptive_featured_count(featured_count):
    plan = CarouselPlan.build(
        theme=get_default_theme_registry().enabled_themes[0],
        editorial_title="Adaptive",
        editorial_subtitle="A variable editorial set.",
        cover_micro_facts=(),
        cover=_cover(),
        featured_artworks=[_artwork(index) for index in range(featured_count)],
        caption="Caption",
    )

    assert len(plan.featured_artworks) == featured_count
    assert len(plan.publication_ids) == featured_count + 1
    assert len(set(plan.publication_ids)) == featured_count + 1


@pytest.mark.parametrize("featured_count", [2, 9])
def test_carousel_plan_rejects_out_of_contract_featured_counts(featured_count):
    with pytest.raises(ValueError, match="between 3 and 8"):
        CarouselPlan.build(
            theme=get_default_theme_registry().enabled_themes[0],
            editorial_title="Adaptive",
            editorial_subtitle="A variable editorial set.",
            cover_micro_facts=(),
            cover=_cover(),
            featured_artworks=[_artwork(index) for index in range(featured_count)],
            caption="Caption",
        )


@pytest.mark.parametrize("featured_count", [3, 5, 8])
def test_gemini_receives_exact_final_featured_list(monkeypatch, featured_count):
    prompts = []

    class Models:
        def generate_content(self, **kwargs):
            prompts.append(kwargs["contents"][0])
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "editorial_intro": "A grounded editorial introduction.",
                        "editorial_subtitle": "A grounded subtitle.",
                        "hashtags": "#Art",
                        "recommended_font_size": 46,
                        "theme_title": "Adaptive Theme",
                    }
                )
            )

    monkeypatch.setattr(gemini_ai.config, "GEMINI_ENABLED", True)
    monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        gemini_ai, "_create_client", lambda _api_key: SimpleNamespace(models=Models())
    )
    artworks = [_artwork(index) for index in range(1, featured_count + 1)]

    editorial_facts = {
        "featured_count": featured_count,
        "distinct_museum_count": featured_count,
        "date_span_label": "1801–1808",
    }
    result = gemini_ai.analyze_carousel(
        "Adaptive Theme", artworks, editorial_facts=editorial_facts
    )

    assert result is not None
    prompt = prompts[0]
    assert prompt.count("\nArtwork ") == featured_count
    assert f"Artwork {featured_count}:" in prompt
    assert f"Artwork {featured_count + 1}:" not in prompt
    assert '"featured_count": ' + str(featured_count) in prompt
    assert "READ ONLY" in prompt
    assert "does not prove that every work" in prompt
