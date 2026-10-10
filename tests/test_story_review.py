"""Exercise the generated editor script at its save boundary without a browser dependency."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import StoryPlan, StorySlide
from src.story_project import StoryProject
from src.story_review import review_page


@pytest.fixture
def editor_html():
    source = SourceArtwork(
        artwork=NormalizedArtwork(
            source="cleveland",
            source_id="1",
            title="Orchid Blossoms",
            artist_name="Known Artist",
            museum_name="Cleveland",
            is_public_domain=True,
            license="CC0",
            rights_status="CONFIRMED_OPEN_ACCESS",
        ),
        image_path="sources/1.jpg",
    )
    plan = StoryPlan(
        theme_id="botanical-study",
        theme_title="Botanical study",
        public_title="Orchid Blossoms",
        headline_kind="source_title",
        narrative="single_study",
        artwork_ids=("cleveland_1",),
        cover_style="detail_study",
        slides=(
            StorySlide(id="cover", role="cover", artwork_ids=("cleveland_1",)),
            StorySlide(id="work", role="artwork", artwork_ids=("cleveland_1",)),
            StorySlide(
                id="detail",
                role="detail",
                artwork_ids=("cleveland_1",),
                focus=(0.22, 0.12, 0.91, 0.6),
                focus_basis="preview_focus",
            ),
            StorySlide(id="closing", role="closing", artwork_ids=("cleveland_1",)),
        ),
    )
    project = StoryProject(revision=1, sources=(source,), plan=plan)
    return review_page(
        project,
        {
            "package": {"slides": [{"path": "pages/test.jpg"}] * 4},
            "quality": {"issues": []},
        },
    )


@pytest.mark.parametrize(
    ("left", "should_save"),
    [
        ("", False),
        (" ", False),
        ("NaN", False),
        ("-0.1", False),
        ("1.1", False),
        ("0", True),
        ("0.22", True),
    ],
)
def test_editor_does_not_convert_missing_crop_input_into_zero(
    tmp_path, editor_html, left, should_save
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the generated editor script")
    page = tmp_path / "review.html"
    page.write_text(editor_html)
    harness = Path(__file__).parent / "fixtures" / "story_editor_harness.cjs"
    result = subprocess.run(
        [node, str(harness), str(page), left],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = json.loads(result.stdout)
    assert output["saved"] is should_save
    if should_save:
        assert output["focus"] == [float(left), 0.12, 0.91, 0.6]
        assert output["reloaded"] is True
    else:
        assert output["reloaded"] is False
        assert output["status"]
        assert output["disabled"] is False
