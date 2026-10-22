"""Exercise the generated editor script at its save boundary without a browser dependency."""

import json
import base64
import hashlib
import io
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import Image

from src.editorial_v2 import SourceArtwork
from src.models import NormalizedArtwork
from src.story_plan import StoryPlan, StorySlide
from src.story_project import StoryProject
from src.story_review import review_page


@pytest.fixture
def editor_html(tmp_path):
    image = tmp_path / "original.tiff"
    Image.new("RGB", (600, 400), "ivory").save(image)
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
        image_path=str(image),
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


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("drag", [0.1, 0.2, 0.8, 0.8]),
        ("reverse", [0.1, 0.2, 0.8, 0.8]),
        ("clamp", [0.22, 0.12, 0.91, 0.6]),
        ("clamp-valid", [0.1, 0.2, 1, 0.8]),
        ("tiny", [0.22, 0.12, 0.91, 0.6]),
        ("cancel", [0.22, 0.12, 0.91, 0.6]),
        ("zero", [0.22, 0.12, 0.91, 0.6]),
    ],
)
def test_visual_crop_selection_reaches_revisioned_save(
    tmp_path, editor_html, scenario, expected
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the generated editor script")
    page = tmp_path / "review.html"
    page.write_text(editor_html)
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "fixtures" / "story_editor_harness.cjs"),
            str(page),
            "0.22",
            scenario,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = json.loads(result.stdout)
    assert output["focus"] == expected
    assert output["revision"] == 1
    assert output["saved"] is True
    assert output["preview_hidden"] is False
    assert output["preview_crop"] == pytest.approx(
        [
            expected[0] * 600,
            expected[1] * 400,
            (expected[2] - expected[0]) * 600,
            (expected[3] - expected[1]) * 400,
        ]
    )


@pytest.mark.parametrize(
    ("focus", "should_save"),
    [
        ([0, 0, 0.079, 1], False),
        ([0, 0, 0.08, 1], True),
        ([0, 0, 0.85, 1], True),
        ([0, 0, 0.851, 1], False),
        ([0, 0, 1, 1], False),
        ([0.2, 0.2, 0.3, 0.3], False),
    ],
)
def test_crop_area_limits_apply_to_preview_and_save(
    tmp_path, editor_html, focus, should_save
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the generated editor script")
    page = tmp_path / "review.html"
    page.write_text(editor_html)
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "fixtures" / "story_editor_harness.cjs"),
            str(page),
            "0.22",
            "",
            json.dumps(focus),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = json.loads(result.stdout)
    assert output["saved"] is should_save
    assert output["reloaded"] is should_save
    assert output["preview_hidden"] is not should_save
    if should_save:
        assert output["focus"] == focus
    else:
        assert "%8" in output["feedback"] and "%85" in output["feedback"]
        assert output["status"] == output["feedback"]
        assert output["disabled"] is False


def test_portable_source_preview_preserves_exif_orientation_and_original(tmp_path):
    from src.story_review import source_preview

    path = tmp_path / "source.tiff"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (600, 400), "green").save(path, exif=exif)
    before = hashlib.sha256(path.read_bytes()).digest()
    uri = source_preview(str(path))
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as preview:
        assert preview.format == "JPEG"
        assert preview.size == (400, 600)
    assert hashlib.sha256(path.read_bytes()).digest() == before
