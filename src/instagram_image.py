"""Instagram single-image publishability and minimum compatibility processing.

Image download safety remains in :mod:`src.quality_filter`; this module only
decides whether an already-securely-decoded asset can be published as a single
Instagram image without changing the artwork.

Meta's Content Publishing documentation is the platform contract:
https://developers.facebook.com/docs/instagram-platform/content-publishing/
"""

from __future__ import annotations

import os
import tempfile
from io import BytesIO
from dataclasses import dataclass
from enum import Enum

from PIL import Image, ImageOps


class InstagramImagePublishabilityReason(str, Enum):
    """Deterministic outcome of the single-image technical gate."""

    SUPPORTED_AS_IS = "SUPPORTED_AS_IS"
    SUPPORTED_AFTER_ORIENTATION_NORMALIZATION = (
        "SUPPORTED_AFTER_ORIENTATION_NORMALIZATION"
    )
    SUPPORTED_AFTER_FORMAT_CONVERSION = "SUPPORTED_AFTER_FORMAT_CONVERSION"
    SUPPORTED_AFTER_SIZE_COMPATIBILITY = "SUPPORTED_AFTER_SIZE_COMPATIBILITY"
    ORIENTATION_NORMALIZATION_REQUIRED = "ORIENTATION_NORMALIZATION_REQUIRED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    UNSUPPORTED_TRANSPARENCY = "UNSUPPORTED_TRANSPARENCY"
    ASPECT_RATIO_OUT_OF_RANGE = "ASPECT_RATIO_OUT_OF_RANGE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    SIZE_UNRECOVERABLE = "SIZE_UNRECOVERABLE"
    INVALID_DIMENSIONS = "INVALID_DIMENSIONS"
    INVALID_IMAGE = "INVALID_IMAGE"
    CONVERSION_FAILED = "CONVERSION_FAILED"


class SingleImageProcessing(str, Enum):
    """The only transformations allowed in the single-artwork path."""

    NONE = "NONE"
    ZERO_TOUCH = "ZERO_TOUCH"
    ORIENTATION_ONLY = "ORIENTATION_ONLY"
    FORMAT_ONLY = "FORMAT_ONLY"
    FORMAT_AND_ORIENTATION = "FORMAT_AND_ORIENTATION"
    JPEG_SIZE_COMPATIBILITY = "JPEG_SIZE_COMPATIBILITY"


@dataclass(frozen=True)
class InstagramImageConstraints:
    """Explicit Content Publishing constraints used by this publisher.

    Meta documents JPEG-only image publishing, an inclusive 4:5 through
    1.91:1 aspect-ratio range, and an 8 MB maximum image size. Dimension
    recommendations are intentionally not enforced as resize requirements:
    the platform can resize compatible source pixels itself.
    """

    min_aspect_ratio: float = 4 / 5
    max_aspect_ratio: float = 1.91
    max_file_size_bytes: int = 8_000_000
    supported_formats: frozenset[str] = frozenset({"JPEG"})
    convertible_formats: frozenset[str] = frozenset({"PNG", "TIFF", "WEBP"})


DEFAULT_INSTAGRAM_IMAGE_CONSTRAINTS = InstagramImageConstraints()
MINIMUM_JPEG_COMPATIBILITY_QUALITY = 75
MAXIMUM_JPEG_COMPATIBILITY_QUALITY = 95
JPEG_QUALITY_SEARCH_ATTEMPT_LIMIT = 5
_TRANSPOSED_ORIENTATIONS = frozenset({5, 6, 7, 8})


@dataclass(frozen=True)
class InstagramImagePublishability:
    """Typed technical result, separate from artwork quality or selection."""

    publishable: bool
    reason: InstagramImagePublishabilityReason
    width: int | None
    height: int | None
    aspect_ratio: float | None
    image_format: str | None
    file_size: int | None
    exif_orientation: int | None
    encoded_width: int | None = None
    encoded_height: int | None = None


@dataclass(frozen=True)
class PreparedSingleImage:
    """The exact local asset to upload, plus how it was obtained."""

    path: str | None
    source: InstagramImagePublishability
    publishability: InstagramImagePublishability
    processing: SingleImageProcessing
    source_bytes_preserved: bool
    compatibility_conversion: bool
    jpeg_quality: int | None = None
    compatibility_attempts: int = 0


class InstagramImageNotPublishableError(RuntimeError):
    """A valid artwork cannot be routed to a single-image post unchanged."""

    def __init__(self, result: InstagramImagePublishability):
        self.result = result
        super().__init__(
            "Artwork is not publishable as a single Instagram image: "
            f"{result.reason.value}"
        )


def _result(
    *,
    publishable: bool,
    reason: InstagramImagePublishabilityReason,
    width: int | None = None,
    height: int | None = None,
    image_format: str | None = None,
    file_size: int | None = None,
    exif_orientation: int | None = None,
    encoded_width: int | None = None,
    encoded_height: int | None = None,
) -> InstagramImagePublishability:
    aspect_ratio = width / height if width and height else None
    return InstagramImagePublishability(
        publishable=publishable,
        reason=reason,
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        image_format=image_format,
        file_size=file_size,
        exif_orientation=exif_orientation,
        encoded_width=encoded_width,
        encoded_height=encoded_height,
    )


def inspect_instagram_image_publishability(
    path: str,
    constraints: InstagramImageConstraints = DEFAULT_INSTAGRAM_IMAGE_CONSTRAINTS,
) -> InstagramImagePublishability:
    """Inspect decoded/display metadata without changing the source file."""
    try:
        file_size = os.path.getsize(path)
        with Image.open(path) as image:
            image_format = (image.format or "").upper() or None
            encoded_width, encoded_height = image.size
            orientation_value = image.getexif().get(274, 1)
            exif_orientation = (
                orientation_value if isinstance(orientation_value, int) else None
            )
    except (OSError, SyntaxError, ValueError):
        return _result(
            publishable=False,
            reason=InstagramImagePublishabilityReason.INVALID_IMAGE,
        )

    if exif_orientation not in range(1, 9):
        return _result(
            publishable=False,
            reason=InstagramImagePublishabilityReason.INVALID_IMAGE,
            image_format=image_format,
            file_size=file_size,
            exif_orientation=exif_orientation,
            encoded_width=encoded_width,
            encoded_height=encoded_height,
        )

    if exif_orientation in _TRANSPOSED_ORIENTATIONS:
        width, height = encoded_height, encoded_width
    else:
        width, height = encoded_width, encoded_height

    if width <= 0 or height <= 0:
        return _result(
            publishable=False,
            reason=InstagramImagePublishabilityReason.INVALID_DIMENSIONS,
            width=width,
            height=height,
            image_format=image_format,
            file_size=file_size,
            exif_orientation=exif_orientation,
            encoded_width=encoded_width,
            encoded_height=encoded_height,
        )

    aspect_ratio = width / height
    common = {
        "width": width,
        "height": height,
        "image_format": image_format,
        "file_size": file_size,
        "exif_orientation": exif_orientation,
        "encoded_width": encoded_width,
        "encoded_height": encoded_height,
    }
    if not constraints.min_aspect_ratio <= aspect_ratio <= constraints.max_aspect_ratio:
        return _result(
            publishable=False,
            reason=InstagramImagePublishabilityReason.ASPECT_RATIO_OUT_OF_RANGE,
            **common,
        )
    if image_format not in constraints.supported_formats:
        return _result(
            publishable=False,
            reason=InstagramImagePublishabilityReason.UNSUPPORTED_FORMAT,
            **common,
        )
    if file_size > constraints.max_file_size_bytes:
        return _result(
            publishable=False,
            reason=InstagramImagePublishabilityReason.FILE_TOO_LARGE,
            **common,
        )
    if exif_orientation != 1:
        return _result(
            publishable=False,
            reason=(
                InstagramImagePublishabilityReason.ORIENTATION_NORMALIZATION_REQUIRED
            ),
            **common,
        )
    return _result(
        publishable=True,
        reason=InstagramImagePublishabilityReason.SUPPORTED_AS_IS,
        **common,
    )


def _has_transparency(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )


def _jpeg_compatible_image(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "L", "CMYK"}:
        return image
    return image.convert("RGB")


def _jpeg_save_options(image: Image.Image, quality: int) -> dict[str, object]:
    options: dict[str, object] = {
        "format": "JPEG",
        "quality": quality,
        "subsampling": 0,
    }
    icc_profile = image.info.get("icc_profile")
    if isinstance(icc_profile, bytes) and icc_profile:
        options["icc_profile"] = icc_profile
    exif = image.getexif()
    if exif:
        options["exif"] = exif.tobytes()
    return options


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    encoded = BytesIO()
    image.save(encoded, **_jpeg_save_options(image, quality))
    return encoded.getvalue()


def _write_bytes_atomically(data: bytes, output_path: str) -> None:
    output_directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_directory, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".instagram-image-",
            suffix=".jpg",
            dir=output_directory,
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write(data)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass


def _save_jpeg_atomically(
    image: Image.Image,
    output_path: str,
    quality: int = MAXIMUM_JPEG_COMPATIBILITY_QUALITY,
) -> None:
    _write_bytes_atomically(_encode_jpeg(image, quality), output_path)


def _find_size_compatible_jpeg(
    image: Image.Image,
    max_file_size_bytes: int,
) -> tuple[bytes | None, int | None, int]:
    """Find the highest allowed quality with a bounded deterministic search."""
    low = MINIMUM_JPEG_COMPATIBILITY_QUALITY
    high = MAXIMUM_JPEG_COMPATIBILITY_QUALITY
    best_bytes: bytes | None = None
    best_quality: int | None = None
    attempts = 0

    while low <= high and attempts < JPEG_QUALITY_SEARCH_ATTEMPT_LIMIT:
        quality = (low + high) // 2
        encoded = _encode_jpeg(image, quality)
        attempts += 1
        if len(encoded) <= max_file_size_bytes:
            best_bytes = encoded
            best_quality = quality
            low = quality + 1
        else:
            high = quality - 1

    return best_bytes, best_quality, attempts


def _failed_preparation(
    result: InstagramImagePublishability,
    source: InstagramImagePublishability | None = None,
) -> PreparedSingleImage:
    return PreparedSingleImage(
        path=None,
        source=source or result,
        publishability=result,
        processing=SingleImageProcessing.NONE,
        source_bytes_preserved=False,
        compatibility_conversion=False,
    )


def _size_unrecoverable_result(
    source_result: InstagramImagePublishability,
) -> InstagramImagePublishability:
    return _result(
        publishable=False,
        reason=InstagramImagePublishabilityReason.SIZE_UNRECOVERABLE,
        width=source_result.width,
        height=source_result.height,
        image_format=source_result.image_format,
        file_size=source_result.file_size,
        exif_orientation=source_result.exif_orientation,
        encoded_width=source_result.encoded_width,
        encoded_height=source_result.encoded_height,
    )


def _prepare_oversized_jpeg(
    source_path: str,
    output_path: str,
    source_result: InstagramImagePublishability,
    constraints: InstagramImageConstraints,
) -> PreparedSingleImage:
    """Re-encode an oversized JPEG without changing its pixel dimensions."""
    attempts = 0
    try:
        with Image.open(source_path) as opened:
            opened.load()
            normalized = ImageOps.exif_transpose(opened)
            compatible = _jpeg_compatible_image(normalized)
            encoded, quality, attempts = _find_size_compatible_jpeg(
                compatible,
                constraints.max_file_size_bytes,
            )
            if encoded is None or quality is None:
                return PreparedSingleImage(
                    path=None,
                    source=source_result,
                    publishability=_size_unrecoverable_result(source_result),
                    processing=SingleImageProcessing.NONE,
                    source_bytes_preserved=False,
                    compatibility_conversion=False,
                    compatibility_attempts=attempts,
                )
            _write_bytes_atomically(encoded, output_path)
    except (OSError, SyntaxError, ValueError):
        return _failed_preparation(
            _result(
                publishable=False,
                reason=InstagramImagePublishabilityReason.CONVERSION_FAILED,
                width=source_result.width,
                height=source_result.height,
                image_format=source_result.image_format,
                file_size=source_result.file_size,
                exif_orientation=source_result.exif_orientation,
                encoded_width=source_result.encoded_width,
                encoded_height=source_result.encoded_height,
            ),
            source_result,
        )

    compatible_result = inspect_instagram_image_publishability(output_path, constraints)
    if not compatible_result.publishable:
        try:
            os.remove(output_path)
        except FileNotFoundError:
            pass
        result = (
            _size_unrecoverable_result(source_result)
            if compatible_result.reason is InstagramImagePublishabilityReason.FILE_TOO_LARGE
            else compatible_result
        )
        return PreparedSingleImage(
            path=None,
            source=source_result,
            publishability=result,
            processing=SingleImageProcessing.NONE,
            source_bytes_preserved=False,
            compatibility_conversion=False,
            jpeg_quality=quality,
            compatibility_attempts=attempts,
        )

    final_result = InstagramImagePublishability(
        publishable=True,
        reason=InstagramImagePublishabilityReason.SUPPORTED_AFTER_SIZE_COMPATIBILITY,
        width=compatible_result.width,
        height=compatible_result.height,
        aspect_ratio=compatible_result.aspect_ratio,
        image_format=compatible_result.image_format,
        file_size=compatible_result.file_size,
        exif_orientation=compatible_result.exif_orientation,
        encoded_width=compatible_result.encoded_width,
        encoded_height=compatible_result.encoded_height,
    )
    return PreparedSingleImage(
        path=output_path,
        source=source_result,
        publishability=final_result,
        processing=SingleImageProcessing.JPEG_SIZE_COMPATIBILITY,
        source_bytes_preserved=False,
        compatibility_conversion=False,
        jpeg_quality=quality,
        compatibility_attempts=attempts,
    )


def prepare_single_instagram_image(
    source_path: str,
    output_path: str,
    constraints: InstagramImageConstraints = DEFAULT_INSTAGRAM_IMAGE_CONSTRAINTS,
) -> PreparedSingleImage:
    """Return the source bytes or perform only a required compatibility transform."""
    source_result = inspect_instagram_image_publishability(source_path, constraints)
    if source_result.publishable:
        return PreparedSingleImage(
            path=source_path,
            source=source_result,
            publishability=source_result,
            processing=SingleImageProcessing.ZERO_TOUCH,
            source_bytes_preserved=True,
            compatibility_conversion=False,
        )

    if (
        source_result.reason is InstagramImagePublishabilityReason.FILE_TOO_LARGE
        and source_result.image_format == "JPEG"
    ):
        return _prepare_oversized_jpeg(
            source_path,
            output_path,
            source_result,
            constraints,
        )

    allowed_processing_reasons = {
        InstagramImagePublishabilityReason.ORIENTATION_NORMALIZATION_REQUIRED,
        InstagramImagePublishabilityReason.UNSUPPORTED_FORMAT,
    }
    if source_result.reason not in allowed_processing_reasons:
        return _failed_preparation(source_result)
    if (
        source_result.reason is InstagramImagePublishabilityReason.UNSUPPORTED_FORMAT
        and source_result.image_format not in constraints.convertible_formats
    ):
        return _failed_preparation(source_result)

    try:
        with Image.open(source_path) as opened:
            opened.load()
            if _has_transparency(opened):
                return _failed_preparation(
                    _result(
                        publishable=False,
                        reason=(
                            InstagramImagePublishabilityReason.UNSUPPORTED_TRANSPARENCY
                        ),
                        width=source_result.width,
                        height=source_result.height,
                        image_format=source_result.image_format,
                        file_size=source_result.file_size,
                        exif_orientation=source_result.exif_orientation,
                        encoded_width=source_result.encoded_width,
                        encoded_height=source_result.encoded_height,
                    ),
                    source_result,
                )
            normalized = ImageOps.exif_transpose(opened)
            converted = _jpeg_compatible_image(normalized)
            _save_jpeg_atomically(converted, output_path)
    except (OSError, SyntaxError, ValueError):
        return _failed_preparation(
            _result(
                publishable=False,
                reason=InstagramImagePublishabilityReason.CONVERSION_FAILED,
                width=source_result.width,
                height=source_result.height,
                image_format=source_result.image_format,
                file_size=source_result.file_size,
                exif_orientation=source_result.exif_orientation,
                encoded_width=source_result.encoded_width,
                encoded_height=source_result.encoded_height,
            ),
            source_result,
        )

    converted_result = inspect_instagram_image_publishability(output_path, constraints)
    if not converted_result.publishable:
        try:
            os.remove(output_path)
        except FileNotFoundError:
            pass
        if converted_result.reason is InstagramImagePublishabilityReason.FILE_TOO_LARGE:
            return _failed_preparation(
                _size_unrecoverable_result(converted_result), source_result
            )
        return _failed_preparation(converted_result, source_result)

    is_format_conversion = (
        source_result.reason is InstagramImagePublishabilityReason.UNSUPPORTED_FORMAT
    )
    final_reason = (
        InstagramImagePublishabilityReason.SUPPORTED_AFTER_FORMAT_CONVERSION
        if is_format_conversion
        else InstagramImagePublishabilityReason.SUPPORTED_AFTER_ORIENTATION_NORMALIZATION
    )
    final_result = InstagramImagePublishability(
        publishable=True,
        reason=final_reason,
        width=converted_result.width,
        height=converted_result.height,
        aspect_ratio=converted_result.aspect_ratio,
        image_format=converted_result.image_format,
        file_size=converted_result.file_size,
        exif_orientation=converted_result.exif_orientation,
        encoded_width=converted_result.encoded_width,
        encoded_height=converted_result.encoded_height,
    )
    if is_format_conversion:
        processing = (
            SingleImageProcessing.FORMAT_AND_ORIENTATION
            if source_result.exif_orientation != 1
            else SingleImageProcessing.FORMAT_ONLY
        )
    else:
        processing = SingleImageProcessing.ORIENTATION_ONLY
    return PreparedSingleImage(
        path=output_path,
        source=source_result,
        publishability=final_result,
        processing=processing,
        source_bytes_preserved=False,
        compatibility_conversion=is_format_conversion,
    )
