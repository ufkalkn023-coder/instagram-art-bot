from PIL import Image
import pytest

from src.editorial_design import CoverStyle
from src.editorial_v2 import HeadlineEvidence, SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import StorySlide
from src.story_design import render_story_slide


def make_source(tmp_path, identity="met_1", halves=((210, 35, 25), (20, 55, 210))):
    path = tmp_path / f"{identity}.png"
    image = Image.new("RGB", (600, 400), halves[0])
    image.paste(halves[1], (300, 0, 600, 400))
    image.save(path)
    source = SourceArtwork(
        artwork=NormalizedArtwork(
            source="met",
            source_id=identity,
            title="Red and Blue",
            artist_name="A. Artist",
            creation_date="1890",
            museum_name="The Metropolitan Museum of Art",
            description="A museum description with enough exact words to quote.",
            is_public_domain=True,
            rights_status="CONFIRMED_PUBLIC_DOMAIN",
        ),
        image_path=str(path),
    )
    return source


def slide(role, identity="met_1", **kwargs):
    ids = (identity, "met_2") if role == "comparison" else (identity,)
    return StorySlide(id=f"{role}-slide", role=role, artwork_ids=ids, **kwargs)


def render(
    tmp_path,
    slide_value,
    sources,
    *,
    position=1,
    total=5,
    cover_detail=None,
    cover_style=CoverStyle.ARTWORK_FIRST,
):
    output = tmp_path / f"{slide_value.role}.jpg"
    result = render_story_slide(
        slide_value,
        sources,
        "A Public Story Title",
        cover_style,
        str(output),
        position=position,
        total=total,
        cover_detail=cover_detail,
    )
    return result, output


def test_artwork_and_detail_have_page_frame_and_real_focus_crop(tmp_path):
    source = make_source(tmp_path)
    sources = {"met_1": source}
    _, artwork_path = render(tmp_path, slide("artwork", title="Red and Blue"), sources)
    detail = slide("detail", focus=(0.55, 0.0, 1.0, 1.0), focus_basis="preview_focus")
    result, detail_path = render(tmp_path, detail, sources)
    with Image.open(artwork_path) as artwork, Image.open(detail_path) as closeup:
        assert artwork.format == closeup.format == "JPEG"
        assert artwork.size == closeup.size == (1080, 1350)
        assert artwork.tobytes() != closeup.tobytes()
        assert artwork.getpixel((200, 400))[0] > artwork.getpixel((200, 400))[2]
        assert artwork.getpixel((850, 400))[2] > artwork.getpixel((850, 400))[0]
        assert closeup.getpixel((540, 500))[2] > closeup.getpixel((540, 500))[0]
    assert result["crop_basis"] == "preview_focus"


def test_comparison_contains_both_sources_and_credits_are_metadata(tmp_path):
    first = make_source(tmp_path, "met_1", ((220, 20, 20), (220, 20, 20)))
    second = make_source(tmp_path, "met_2", ((20, 20, 220), (20, 20, 220)))
    result, output = render(
        tmp_path, slide("comparison"), {"met_1": first, "met_2": second}
    )
    with Image.open(output) as image:
        assert image.size == (1080, 1350)
        assert image.getpixel((300, 500))[0] > image.getpixel((300, 500))[2]
        assert image.getpixel((780, 500))[2] > image.getpixel((780, 500))[0]
    assert result["role"] == "comparison"
    assert result["actual_style"] is None


def test_context_quote_is_exact_and_long_unfittable_copy_fails(tmp_path):
    source = make_source(tmp_path)
    quote = "A museum description with enough exact words to quote."
    context = slide(
        "context",
        title="From the museum",
        body=quote,
        evidence=(
            HeadlineEvidence(
                artwork_id="met_1",
                kind="museum",
                statement="Museum quotation",
                source_quote=quote,
            ),
        ),
    )
    _, output = render(tmp_path, context, {"met_1": source})
    with Image.open(output) as image:
        assert image.size == (1080, 1350)
    with pytest.raises(ValueError, match="fit|capacity"):
        render(tmp_path, slide("artwork", title="unfit " * 200), {"met_1": source})


def test_cover_uses_requested_renderer_and_invalid_source_fails_before_output(tmp_path):
    source = make_source(tmp_path)
    result, output = render(tmp_path, slide("cover"), {"met_1": source})
    assert result["actual_style"] == CoverStyle.ARTWORK_FIRST.value
    assert result["role"] == "cover"
    invalid = SourceArtwork.model_validate(
        {**source.model_dump(), "image_path": str(tmp_path / "missing.png")}
    )
    destination = tmp_path / "missing-output.jpg"
    with pytest.raises(ValueError, match="valid|image"):
        render_story_slide(
            slide("artwork"),
            {"met_1": invalid},
            "Title",
            CoverStyle.ARTWORK_FIRST,
            str(destination),
            position=1,
            total=1,
        )
    assert not destination.exists()


def test_detail_study_cover_uses_matching_focus_and_reports_original_provenance(
    tmp_path,
):
    source = make_source(tmp_path)
    cover = slide("cover")
    focus = (0.55, 0.0, 1.0, 1.0)
    detail = slide(
        "detail",
        focus=focus,
        focus_basis="local_geometry",
        title="ignored detail title",
    )
    result, output = render(
        tmp_path,
        cover,
        {"met_1": source},
        cover_detail=detail,
        cover_style=CoverStyle.DETAIL_STUDY,
    )
    assert result["actual_style"] == CoverStyle.DETAIL_STUDY.value
    assert result["crop_basis"] == "local_geometry"
    with Image.open(output) as image:
        assert image.size == (1080, 1350)
        assert image.getpixel((540, 500))[2] > image.getpixel((540, 500))[0]


def test_cover_detail_must_match_cover_source_before_writing(tmp_path):
    first = make_source(tmp_path, "met_1")
    second = make_source(tmp_path, "met_2")
    cover = slide("cover", "met_1")
    detail = slide(
        "detail", "met_2", focus=(0.1, 0.1, 0.9, 0.9), focus_basis="preview_focus"
    )
    destination = tmp_path / "cover.jpg"
    with pytest.raises(ValueError, match="cover artwork"):
        render_story_slide(
            cover,
            {"met_1": first, "met_2": second},
            "Title",
            CoverStyle.DETAIL_STUDY,
            str(destination),
            position=1,
            total=1,
            cover_detail=detail,
        )
    assert not destination.exists()
