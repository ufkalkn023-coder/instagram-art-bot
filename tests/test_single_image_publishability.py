import pytest
from PIL import Image

from src import image_processor, instagram_image
from src.instagram_image import (
    JPEG_QUALITY_SEARCH_ATTEMPT_LIMIT,
    MINIMUM_JPEG_COMPATIBILITY_QUALITY,
    InstagramImageConstraints,
    InstagramImagePublishabilityReason,
    SingleImageProcessing,
    inspect_instagram_image_publishability,
    prepare_single_instagram_image,
)


def _save_image(
    path,
    image_format="JPEG",
    *,
    size=(120, 120),
    mode="RGB",
    color=(90, 120, 150),
    exif_orientation=None,
    icc_profile=None,
):
    image = Image.new(mode, size, color)
    save_options = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        save_options["exif"] = exif
    if icc_profile is not None:
        save_options["icc_profile"] = icc_profile
    image.save(path, image_format, **save_options)


@pytest.mark.parametrize("size", [(191, 100), (180, 120), (120, 120), (96, 120)])
def test_compatible_jpeg_orientations_are_zero_touch(size, tmp_path):
    source = tmp_path / "source.jpg"
    output = tmp_path / "unused-output.jpg"
    _save_image(source, size=size)
    original_bytes = source.read_bytes()

    prepared = prepare_single_instagram_image(str(source), str(output))

    assert prepared.path == str(source)
    assert prepared.publishability.publishable
    assert prepared.publishability.reason is InstagramImagePublishabilityReason.SUPPORTED_AS_IS
    assert (prepared.publishability.width, prepared.publishability.height) == size
    assert prepared.processing is SingleImageProcessing.ZERO_TOUCH
    assert prepared.source_bytes_preserved
    assert not prepared.compatibility_conversion
    assert source.read_bytes() == original_bytes
    assert not output.exists()


def test_zero_touch_preserves_source_pixels_dimensions_and_icc(tmp_path):
    source = tmp_path / "profiled.jpg"
    embedded_profile = b"test-icc-profile"
    _save_image(source, size=(180, 120), icc_profile=embedded_profile)
    source_bytes = source.read_bytes()

    prepared = prepare_single_instagram_image(
        str(source), str(tmp_path / "output.jpg")
    )

    assert prepared.path == str(source)
    assert source.read_bytes() == source_bytes
    with Image.open(prepared.path) as published:
        assert published.size == (180, 120)
        assert published.info["icc_profile"] == embedded_profile


@pytest.mark.parametrize("size", [(200, 100), (100, 126)])
def test_out_of_range_aspect_is_typed_and_never_padded_or_cropped(size, tmp_path):
    source = tmp_path / "source.jpg"
    output = tmp_path / "output.jpg"
    _save_image(source, size=size)
    source_bytes = source.read_bytes()

    prepared = prepare_single_instagram_image(str(source), str(output))

    assert not prepared.publishability.publishable
    assert (
        prepared.publishability.reason
        is InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE
    )
    assert prepared.processing is SingleImageProcessing.NONE
    assert prepared.path is None
    assert source.read_bytes() == source_bytes
    assert not output.exists()


def test_impossible_file_size_reduction_is_typed_and_bounded(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    _save_image(source)
    source_bytes = source.read_bytes()
    constraints = InstagramImageConstraints(max_file_size_bytes=len(source_bytes) - 1)
    qualities = []
    original_encode = instagram_image._encode_jpeg

    def track_encode(image, quality):
        qualities.append(quality)
        return original_encode(image, quality)

    monkeypatch.setattr(instagram_image, "_encode_jpeg", track_encode)

    prepared = prepare_single_instagram_image(
        str(source), str(tmp_path / "output.jpg"), constraints
    )

    assert prepared.publishability.reason is InstagramImagePublishabilityReason.SIZE_UNRECOVERABLE
    assert prepared.path is None
    assert source.read_bytes() == source_bytes
    assert len(qualities) <= JPEG_QUALITY_SEARCH_ATTEMPT_LIMIT
    assert min(qualities) >= MINIMUM_JPEG_COMPATIBILITY_QUALITY


def test_oversized_jpeg_uses_quality_only_size_compatibility(tmp_path):
    source = tmp_path / "source.jpg"
    output = tmp_path / "output.jpg"
    image = Image.effect_noise((600, 600), 100).convert("RGB")
    image.save(source, "JPEG", quality=100, subsampling=0)
    source_bytes = source.read_bytes()
    constraints = InstagramImageConstraints(max_file_size_bytes=270_000)

    prepared = prepare_single_instagram_image(
        str(source), str(output), constraints
    )

    assert prepared.path == str(output)
    assert prepared.publishability.publishable
    assert (
        prepared.publishability.reason
        is InstagramImagePublishabilityReason.SUPPORTED_AFTER_SIZE_COMPATIBILITY
    )
    assert prepared.processing is SingleImageProcessing.JPEG_SIZE_COMPATIBILITY
    assert not prepared.source_bytes_preserved
    assert not prepared.compatibility_conversion
    assert prepared.jpeg_quality >= MINIMUM_JPEG_COMPATIBILITY_QUALITY
    assert prepared.compatibility_attempts <= JPEG_QUALITY_SEARCH_ATTEMPT_LIMIT
    assert prepared.source.file_size == len(source_bytes)
    assert prepared.publishability.file_size == output.stat().st_size
    assert output.stat().st_size <= constraints.max_file_size_bytes
    assert source.read_bytes() == source_bytes
    with Image.open(output) as published:
        assert published.size == image.size
    assert prepared.source.aspect_ratio == prepared.publishability.aspect_ratio == 1.0


def test_rgb_png_receives_format_only_conversion_without_canvas(tmp_path):
    source = tmp_path / "source.png"
    output = tmp_path / "output.jpg"
    embedded_profile = b"test-icc-profile"
    _save_image(
        source,
        "PNG",
        size=(180, 120),
        color=(210, 35, 20),
        icc_profile=embedded_profile,
    )

    prepared = prepare_single_instagram_image(str(source), str(output))

    assert prepared.path == str(output)
    assert (
        prepared.publishability.reason
        is InstagramImagePublishabilityReason.SUPPORTED_AFTER_FORMAT_CONVERSION
    )
    assert prepared.processing is SingleImageProcessing.FORMAT_ONLY
    assert prepared.compatibility_conversion
    assert not prepared.source_bytes_preserved
    with Image.open(output) as published:
        assert published.format == "JPEG"
        assert published.size == (180, 120)
        assert published.info["icc_profile"] == embedded_profile
        red, green, blue = published.getpixel((90, 60))
        assert red > 190 and green < 55 and blue < 40


def test_transparent_source_fails_instead_of_inventing_a_background(tmp_path):
    source = tmp_path / "transparent.png"
    output = tmp_path / "output.jpg"
    _save_image(source, "PNG", mode="RGBA", color=(20, 40, 60, 120))

    prepared = prepare_single_instagram_image(str(source), str(output))

    assert (
        prepared.publishability.reason
        is InstagramImagePublishabilityReason.UNSUPPORTED_TRANSPARENCY
    )
    assert prepared.path is None
    assert not output.exists()


def test_nonconvertible_format_fails_cleanly(tmp_path):
    source = tmp_path / "source.bmp"
    _save_image(source, "BMP")

    prepared = prepare_single_instagram_image(
        str(source), str(tmp_path / "output.jpg")
    )

    assert (
        prepared.publishability.reason
        is InstagramImagePublishabilityReason.UNSUPPORTED_FORMAT
    )
    assert prepared.path is None


def test_exif_orientation_is_normalized_only_when_required(tmp_path):
    source = tmp_path / "rotated.jpg"
    output = tmp_path / "normalized.jpg"
    image = Image.new("RGB", (120, 100))
    for x in range(image.width):
        color = (220, 20, 20) if x < image.width // 2 else (20, 20, 220)
        for y in range(image.height):
            image.putpixel((x, y), color)
    exif = Image.Exif()
    exif[274] = 6
    embedded_profile = b"test-icc-profile"
    image.save(
        source,
        "JPEG",
        quality=95,
        exif=exif,
        icc_profile=embedded_profile,
    )
    source_bytes = source.read_bytes()

    inspection = inspect_instagram_image_publishability(str(source))
    assert (inspection.encoded_width, inspection.encoded_height) == (120, 100)
    assert (inspection.width, inspection.height) == (100, 120)
    assert (
        inspection.reason
        is InstagramImagePublishabilityReason.ORIENTATION_NORMALIZATION_REQUIRED
    )

    prepared = prepare_single_instagram_image(str(source), str(output))

    assert (
        prepared.publishability.reason
        is InstagramImagePublishabilityReason.SUPPORTED_AFTER_ORIENTATION_NORMALIZATION
    )
    assert prepared.processing is SingleImageProcessing.ORIENTATION_ONLY
    assert source.read_bytes() == source_bytes
    with Image.open(output) as published:
        assert published.size == (100, 120)
        assert published.getexif().get(274, 1) == 1
        assert published.info["icc_profile"] == embedded_profile
        top = published.getpixel((50, 15))
        bottom = published.getpixel((50, 105))
        assert top[0] > top[2]
        assert bottom[2] > bottom[0]


def test_exif_display_aspect_controls_publishability_before_transform(tmp_path):
    source = tmp_path / "rotated-too-tall.jpg"
    _save_image(source, size=(180, 100), exif_orientation=6)

    result = inspect_instagram_image_publishability(str(source))

    assert (result.encoded_width, result.encoded_height) == (180, 100)
    assert (result.width, result.height) == (100, 180)
    assert result.reason is InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE


def test_r2_upload_uses_decoded_content_type_and_does_not_rewrite_source(
    monkeypatch, tmp_path
):
    source = tmp_path / "source.jpg"
    _save_image(source, size=(180, 120))
    original_bytes = source.read_bytes()
    for key in (
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_BUCKET_NAME",
    ):
        monkeypatch.setenv(key, "configured")
    monkeypatch.setenv("CLOUDFLARE_R2_PUBLIC_URL", "https://media.example")

    uploads = []

    class FakeS3Client:
        def upload_file(self, file_path, bucket, object_key, ExtraArgs):
            uploads.append((file_path, bucket, object_key, ExtraArgs))

    class HeadResponse:
        status_code = 200
        headers = {
            "Content-Type": "image/jpeg; charset=binary",
            "Content-Length": str(len(original_bytes)),
        }

    monkeypatch.setattr(image_processor.boto3, "client", lambda *args, **kwargs: FakeS3Client())
    monkeypatch.setattr(image_processor.requests, "head", lambda *args, **kwargs: HeadResponse())
    monkeypatch.setattr(image_processor.time, "sleep", lambda _seconds: None)

    public_url = image_processor.upload_temp_media(str(source))

    assert uploads[0][0] == str(source)
    assert uploads[0][1] == "configured"
    assert uploads[0][2].endswith(".jpg")
    assert uploads[0][3] == {"ContentType": "image/jpeg"}
    assert public_url.endswith(uploads[0][2])
    assert source.read_bytes() == original_bytes
