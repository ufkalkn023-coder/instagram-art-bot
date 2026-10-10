import json

import pytest
from PIL import Image

from scripts.preview_editorial_v2 import build_gallery, load_detail_focus, load_sources
from src.models import NormalizedArtwork


def manifest(tmp_path, *, path="image.jpg", title="A <script>alert(1)</script>"):
    Image.new("RGB", (900, 1000), "navy").save(tmp_path / "image.jpg")
    artwork = NormalizedArtwork(
        source="cleveland",
        source_id="1",
        title=title,
        museum_name="Cleveland Museum of Art",
        license="CC0",
        is_public_domain=True,
        rights_status="CONFIRMED_OPEN_ACCESS",
        artwork_url="https://www.clevelandart.org/art/1",
    )
    location = tmp_path / "sources.json"
    location.write_text(
        json.dumps(
            {"artworks": [{"artwork": artwork.model_dump(), "image_path": path}]}
        )
    )
    return location


def test_manifest_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError, match="manifest directory"):
        load_sources(manifest(tmp_path, path="../outside.jpg"), count=1)


def test_manifest_rejects_unconfirmed_rights(tmp_path):
    path = manifest(tmp_path)
    data = json.loads(path.read_text())
    data["artworks"][0]["artwork"]["is_public_domain"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="rights"):
        load_sources(path, count=1)


def test_default_gallery_is_ai_disabled_escaped_and_has_three_covers(tmp_path):
    sources = load_sources(manifest(tmp_path), count=1)
    output = tmp_path / "output"
    report = build_gallery(sources, output)
    assert report["ai_enabled"] is False
    assert report["ai_calls"] == 0
    assert report["cover_count"] == 3
    assert report["factual_fallback_count"] == 1
    assert len(list(output.glob("covers/*.jpg"))) == 3
    html = (output / "index.html").read_text()
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert report["items"][0]["plan"]["manual_review_required"] is True
    assert report["items"][0]["covers"][2]["crop_basis"] == "missing_focus_full_artwork"


def test_gallery_refuses_existing_output(tmp_path):
    sources = load_sources(manifest(tmp_path), count=1)
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("user data")
    with pytest.raises(ValueError, match="empty"):
        build_gallery(sources, output)
    assert (output / "keep.txt").read_text() == "user data"


def test_reviewed_preview_focus_renders_a_real_detail_without_ai(tmp_path):
    sources = load_sources(manifest(tmp_path), count=1)
    # A left-hand red subject and a right-hand blue background distinguish crops.
    image = Image.new("RGB", (900, 1000), "blue")
    image.paste("red", (0, 0, 450, 1000))
    image.save(sources[0].image_path)
    report = build_gallery(
        sources, tmp_path / "detail", detail_focus={"cleveland_1": (0, 0, 0.5, 1)}
    )
    detail = report["items"][0]["covers"][2]
    assert detail["crop_basis"] == "preview_focus"
    assert detail["actual_style"] == "detail_study"
    assert detail["focus"] == [0, 0, 0.5, 1]
    assert report["preview_detail_count"] == 1
    assert report["ai_calls"] == 0
    with Image.open(tmp_path / "detail" / detail["output_path"]) as rendered:
        red, green, blue = rendered.getpixel((540, 500))
        assert red > 240 and green < 10 and blue < 10


@pytest.mark.parametrize(
    "focus", [{"unknown": (0, 0, 0.5, 1)}, {"cleveland_1": (0, 0, 1, 1)}]
)
def test_preview_focus_is_validated_before_output_writes(tmp_path, focus):
    sources = load_sources(manifest(tmp_path), count=1)
    with pytest.raises(ValueError):
        build_gallery(sources, tmp_path / "output", detail_focus=focus)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "value", [None, [0, 0, 1], [False, 0, 0.5, 1], ["0", 0, 0.5, 1]]
)
def test_detail_focus_file_rejects_malformed_coordinates(tmp_path, value):
    path = tmp_path / "focus.json"
    path.write_text(json.dumps({"cleveland_1": value}))
    with pytest.raises(ValueError, match="four numeric"):
        load_detail_focus(path)


def test_cli_rejects_reviewed_crops_with_acquisition_before_any_download(
    tmp_path, monkeypatch
):
    from scripts import preview_editorial_v2 as preview

    monkeypatch.setattr(
        "sys.argv",
        [
            "preview_editorial_v2.py",
            "--acquire-cleveland",
            str(tmp_path / "sources"),
            "--output",
            str(tmp_path / "gallery"),
            "--detail-focus",
            str(tmp_path / "focus.json"),
        ],
    )

    def unexpected_download(*args, **kwargs):
        pytest.fail("Input validation must precede source acquisition")

    monkeypatch.setattr(preview, "acquire_sources", unexpected_download)
    with pytest.raises(SystemExit) as error:
        preview.main()
    assert error.value.code == 2
    assert not (tmp_path / "sources").exists()


def test_acquisition_skips_valid_but_too_small_images(tmp_path, monkeypatch):
    from scripts import preview_editorial_v2 as preview
    from src.quality_filter import ImageValidationResult

    small = NormalizedArtwork(
        source="cleveland",
        source_id="1",
        title="Small",
        museum_name="Cleveland",
        license="CC0",
        is_public_domain=True,
        rights_status="CONFIRMED_OPEN_ACCESS",
        image_url="https://example.org/1.jpg",
    )
    large = small.model_copy(update={"source_id": "2", "title": "Large"})
    monkeypatch.setattr(
        preview.ClevelandAdapter, "fetch_candidates", lambda *a, **k: [small, large]
    )

    def download(url, path):
        size = (100, 100) if url.endswith("1.jpg") and "001" in path else (900, 1000)
        Image.new("RGB", size, "navy").save(path)
        return ImageValidationResult(valid=True, width=size[0], height=size[1])

    monkeypatch.setattr(preview, "validate_and_download_image_with_metadata", download)
    sources = preview.acquire_sources(tmp_path / "sources", count=1, seed=1)
    assert sources[0].artwork.source_id == "2"
    data = json.loads((tmp_path / "sources/sources.json").read_text())
    assert len(data["rejected"]) == 1
