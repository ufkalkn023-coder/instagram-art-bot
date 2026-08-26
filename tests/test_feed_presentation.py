from pathlib import Path

import pytest
from PIL import Image

from src import image_processor
from src.instagram_image import InstagramImageNotPublishableError


def test_prepare_local_image_preserves_source_bytes_and_natural_orientation(tmp_path):
    source_path = tmp_path / "source.jpg"
    Image.new("RGB", (1600, 1000), (38, 78, 118)).save(source_path, "JPEG")
    source_bytes = source_path.read_bytes()

    prepared_path, orientation = image_processor.prepare_local_image(str(source_path))

    assert prepared_path == str(source_path)
    assert orientation == "horizontal"
    assert source_path.read_bytes() == source_bytes


def test_legacy_feed_wrapper_is_zero_touch_for_publishable_jpeg(tmp_path, monkeypatch):
    source_path = tmp_path / "source.jpg"
    output_path = tmp_path / "unused.jpg"
    Image.new("RGB", (1600, 1000), (38, 78, 118)).save(source_path, "JPEG")
    source_bytes = source_path.read_bytes()

    def forbidden_filter(*args, **kwargs):
        pytest.fail("single artwork presentation must not create a blurred background")

    monkeypatch.setattr(Image.Image, "filter", forbidden_filter)
    rendered_path = image_processor.create_feed_post(
        str(source_path), output_path=str(output_path)
    )

    assert rendered_path == str(source_path)
    assert source_path.read_bytes() == source_bytes
    assert not output_path.exists()
    with Image.open(rendered_path) as rendered:
        assert rendered.size == (1600, 1000)


def test_legacy_feed_wrapper_only_converts_format_when_required(tmp_path):
    source_path = tmp_path / "source.png"
    output_path = tmp_path / "converted.jpg"
    Image.new("RGB", (1000, 1200), (38, 78, 118)).save(source_path, "PNG")

    rendered_path = image_processor.create_feed_post(
        str(source_path), output_path=str(output_path)
    )

    assert rendered_path == str(output_path)
    with Image.open(rendered_path) as rendered:
        assert rendered.format == "JPEG"
        assert rendered.size == (1000, 1200)


def test_legacy_feed_wrapper_rejects_unpublishable_aspect_without_crop(tmp_path):
    source_path = tmp_path / "panorama.jpg"
    output_path = tmp_path / "feed.jpg"
    Image.new("RGB", (2400, 800), (38, 78, 118)).save(source_path, "JPEG")

    with pytest.raises(InstagramImageNotPublishableError):
        image_processor.create_feed_post(
            str(source_path), output_path=str(output_path)
        )

    assert not output_path.exists()


def test_workflow_uses_explicit_split_single_and_carousel_schedules():
    workflow = Path(".github/workflows/instagram_bot.yml").read_text()

    assert 'cron: "0 0,3,6,9,15,18 * * *"' in workflow
    assert 'cron: "0 12,21 * * *"' in workflow
    assert "python main.py --mode single" in workflow
    assert "python main.py --mode carousel" in workflow
    assert "workflow_dispatch" in workflow
