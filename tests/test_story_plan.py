import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import StoryPlan, StorySlide, build_story_plan


@pytest.fixture
def sources(tmp_path):
    result = []
    for index in range(5):
        path = tmp_path / f"work-{index}.jpg"
        image = Image.new("RGB", (1600, 1800), "ivory")
        draw = ImageDraw.Draw(image)
        for n in range(20):
            draw.rectangle(
                (n * 60, n * 70, n * 60 + 280, n * 70 + 300), fill=(n * 10, 60, 160)
            )
        image.save(path)
        result.append(
            SourceArtwork(
                artwork=NormalizedArtwork(
                    source="cleveland",
                    source_id=str(index),
                    title=f"Painting {index}",
                    artist_name="Known artist",
                    creation_date="1873",
                    museum_name="Cleveland Museum of Art",
                    is_public_domain=True,
                    license="CC0",
                    rights_status="CONFIRMED_OPEN_ACCESS",
                ),
                image_path=str(path),
            )
        )
    return result


def test_single_story_separates_artwork_identity_from_pages(sources):
    plan = build_story_plan(
        sources[:1],
        "single_study",
        detail_focus={"cleveland_0": [(0.1, 0.1, 0.6, 0.6)]},
    )
    assert plan.artwork_ids == ("cleveland_0",)
    assert len(plan.slides) >= 4
    assert plan.slides[0].role == "cover" and plan.slides[-1].role == "closing"
    assert sum("cleveland_0" in slide.artwork_ids for slide in plan.slides) >= 4
    assert len({slide.id for slide in plan.slides}) == len(plan.slides)
    assert any(
        slide.role == "detail" and slide.focus_basis == "preview_focus"
        for slide in plan.slides
    )


def test_comparison_has_a_two_source_page(sources):
    plan = build_story_plan(sources[:2], "comparison")
    assert any(
        slide.role == "comparison" and len(slide.artwork_ids) == 2
        for slide in plan.slides
    )
    assert len(plan.artwork_ids) == 2


def test_thematic_selection_preserves_unique_registry(sources):
    plan = build_story_plan(sources, "thematic_selection")
    assert len(plan.artwork_ids) == 5
    assert {
        slide.artwork_ids[0] for slide in plan.slides if slide.role == "artwork"
    } == set(plan.artwork_ids)


def test_context_is_only_an_exact_museum_excerpt(sources):
    source = sources[0]
    source = source.model_copy(
        update={
            "artwork": source.artwork.model_copy(
                update={"description": "An orchid grows above a distant valley."}
            )
        }
    )
    plan = build_story_plan([source], "single_study")
    context = next(slide for slide in plan.slides if slide.role == "context")
    assert context.body == source.artwork.description
    assert context.evidence[0].source_quote == context.body
    assert not any(
        slide.role == "context"
        for slide in build_story_plan(sources[:1], "single_study").slides
    )


def test_flat_image_does_not_produce_fake_detail_pages(sources):
    Image.new("RGB", (1600, 1800), "navy").save(sources[0].image_path)
    plan = build_story_plan(sources[:1], "single_study")
    assert not any(slide.role == "detail" for slide in plan.slides)
    assert "no_usable_detail" in plan.fallback_reasons


def test_slide_with_unknown_artwork_is_rejected(sources):
    data = build_story_plan(sources[:1], "single_study").model_dump()
    data["slides"][1]["artwork_ids"] = ["unknown"]
    with pytest.raises(ValidationError, match="unknown artwork"):
        StoryPlan.model_validate(data)


def test_wrong_narrative_cardinality_is_rejected(sources):
    with pytest.raises(ValueError):
        build_story_plan(sources[:1], "comparison")


def test_cover_copy_is_owned_by_public_title():
    with pytest.raises(ValueError, match="public title"):
        StorySlide(
            id="cover", role="cover", artwork_ids=("met_1",), title="Ignored title"
        )


def test_whitespace_public_title_is_rejected(sources):
    plan = build_story_plan(sources[:1], "single_study")
    data = plan.model_dump(mode="json")
    data["public_title"] = "   "
    with pytest.raises(ValueError, match="visible text"):
        StoryPlan.model_validate(data)


def test_existing_editorial_theme_is_preserved_independently_of_narrative(sources):
    from src.editorial_v2 import EditorialPlan
    from src.editorial_design import CoverStyle

    editorial = EditorialPlan(
        theme_id="botanical-studies",
        theme_title="Botanical studies",
        public_title=sources[0].artwork.title,
        editorial_angle="Observe the composition",
        headline_evidence=[],
        cover_style=CoverStyle.ARTWORK_FIRST,
        focus=None,
        status="factual_fallback",
        rejections=[],
    )
    plan = build_story_plan(sources[:1], "single_study", editorial=editorial)
    assert plan.theme_id == "botanical-studies"
    assert plan.theme_title == "Botanical studies"
    assert plan.narrative == "single_study"
