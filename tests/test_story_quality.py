from pathlib import Path

from PIL import Image

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import build_story_plan
from src.story_quality import assess_story


def source(tmp_path):
    path = tmp_path / "image.jpg"
    Image.new("RGB", (1600, 1800), "navy").save(path)
    return SourceArtwork(
        artwork=NormalizedArtwork(
            source="cleveland",
            source_id="1",
            title="A Painting",
            artist_name="Known Artist",
            museum_name="Cleveland",
            is_public_domain=True,
            rights_status="CONFIRMED_OPEN_ACCESS",
            license="CC0",
            description="An orchid grows above a distant valley.",
        ),
        image_path=str(path),
    )


def test_valid_plan_is_renderable_but_not_automatically_publishable(tmp_path):
    item = source(tmp_path)
    report = assess_story(build_story_plan([item], "single_study"), [item])
    assert report.can_render is True
    assert report.publication_ready is False
    assert any(i.code == "manual_review_required" for i in report.issues)


def test_duplicate_crops_are_fatal_even_with_other_valid_pages(tmp_path):
    item = source(tmp_path)
    plan = build_story_plan(
        [item],
        "single_study",
        detail_focus={"cleveland_1": [(0, 0, 0.5, 0.5), (0, 0, 0.5, 0.5)]},
    )
    report = assess_story(plan, [item])
    assert report.can_render is False
    assert any(
        i.code == "repeated_detail" and i.severity == "critical" for i in report.issues
    )


def test_forged_context_quote_blocks_rendering(tmp_path):
    item = source(tmp_path)
    plan = build_story_plan([item], "single_study")
    data = plan.model_dump()
    context = next(slide for slide in data["slides"] if slide["role"] == "context")
    context["body"] = "Commissioned by the king."
    from src.story_plan import StoryPlan

    report = assess_story(StoryPlan.model_validate(data), [item])
    assert any(i.code == "unsupported_context" for i in report.issues)
    assert report.can_render is False


def test_small_source_crop_is_blocked(tmp_path):
    item = source(tmp_path)
    Image.new("RGB", (500, 600), "navy").save(item.image_path)
    plan = build_story_plan(
        [item], "single_study", detail_focus={"cleveland_1": [(0.1, 0.1, 0.4, 0.4)]}
    )
    assert any(i.code == "detail_resolution" for i in assess_story(plan, [item]).issues)


def test_missing_image_is_fatal(tmp_path):
    item = source(tmp_path)
    plan = build_story_plan([item], "single_study")
    Path(item.image_path).unlink()
    assert not assess_story(plan, [item]).can_render


def test_repeated_public_title_is_fatal(tmp_path):
    item = source(tmp_path)
    plan = build_story_plan([item], "single_study")
    assert any(
        i.code == "repeated_title"
        for i in assess_story(plan, [item], headline_history=["A Painting"]).issues
    )
