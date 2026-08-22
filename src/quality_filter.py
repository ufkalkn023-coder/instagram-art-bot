import ipaddress
import logging
import os
import socket
import tempfile
import warnings
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from PIL import Image

from src.models import NormalizedArtwork, normalize_image_dimensions

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = (5, 20)
MAX_REDIRECTS = 4
MAX_IMAGE_DOWNLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024
ACCEPTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF"}
REJECTED_CONTENT_TYPES = {
    "application/json",
    "application/xml",
    "image/svg+xml",
    "text/html",
    "text/xml",
}


@dataclass(frozen=True)
class ImageValidationResult:
    """Outcome of one secure download, including decoded raster metadata."""

    valid: bool
    width: int | None = None
    height: int | None = None
    image_format: str | None = None
    reason: str | None = None


def _safe_url_for_log(url: str) -> str:
    """Return only scheme, host, and path so URL credentials never log."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname:
            return "<invalid-url>"
        display_host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            display_host = f"{display_host}:{parsed.port}"
        return urlunsplit((parsed.scheme, display_host, parsed.path, "", ""))
    except ValueError:
        return "<invalid-url>"


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    # Treat IPv4-mapped IPv6 according to the embedded IPv4 address.
    mapped_ipv4 = getattr(ip, "ipv4_mapped", None)
    if mapped_ipv4 is not None:
        ip = mapped_ipv4

    return ip.is_global


def _host_resolves_to_public_ips(hostname: str, port: int) -> bool:
    try:
        # Reject a mixed DNS answer: requests may choose any returned address.
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return False

    resolved_ips = {address[4][0] for address in addresses if address[4]}
    return bool(resolved_ips) and all(_is_public_ip(address) for address in resolved_ips)


def _is_safe_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError:
        return False

    hostname = parsed.hostname
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False

    hostname = hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        return _host_resolves_to_public_ips(hostname, port)
    return _is_public_ip(str(literal_ip))


def _is_rejected_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    return (
        media_type in REJECTED_CONTENT_TYPES
        or media_type.startswith("text/")
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def _content_length_exceeds_limit(headers: Dict[str, str]) -> bool:
    content_length = headers.get("Content-Length")
    if content_length is None:
        return False
    try:
        return int(content_length) > MAX_IMAGE_DOWNLOAD_BYTES
    except (TypeError, ValueError):
        # A missing or malformed header is not trusted; streaming still enforces
        # the actual byte limit.
        return False


def _validate_downloaded_image(path: str) -> ImageValidationResult:
    try:
        # Pillow otherwise emits this as a warning. Treat it as a hard reject in
        # this validation context without changing Pillow's global policy.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()

            # verify() invalidates the image object, so reopen before inspecting
            # dimensions, format, and decoding the validated raster.
            with Image.open(path) as image:
                image_format = (image.format or "").upper()
                if image_format not in ACCEPTED_IMAGE_FORMATS:
                    return ImageValidationResult(False, reason="unsupported_format")

                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    return ImageValidationResult(False, reason="too_many_pixels")
                if width < 100 or height < 100:
                    return ImageValidationResult(False, reason="image_too_small")

                # Detect truncated/corrupt data that verify() alone may not read.
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return ImageValidationResult(False, reason="decompression_bomb")
    except Exception:
        return ImageValidationResult(False, reason="invalid_image")

    return ImageValidationResult(True, width, height, image_format, "ok")


def _remove_file_if_present(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def validate_and_download_image_with_metadata(url: str, output_path: str) -> ImageValidationResult:
    """Safely download one image and return its decoded dimensions from that download."""
    if not isinstance(url, str) or not _is_safe_image_url(url):
        logger.warning("Image validation failed: unsafe_url (%s)", _safe_url_for_log(str(url)))
        return ImageValidationResult(False, reason="unsafe_url")

    current_url = url
    visited_urls = set()
    temporary_path = None
    response = None

    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            if current_url in visited_urls or not _is_safe_image_url(current_url):
                logger.warning("Image validation failed: unsafe_url (%s)", _safe_url_for_log(current_url))
                return ImageValidationResult(False, reason="unsafe_url")
            visited_urls.add(current_url)

            response = requests.get(
                current_url,
                headers={"User-Agent": "InstagramArtBot/1.0"},
                stream=True,
                timeout=HTTP_TIMEOUT_SECONDS,
                allow_redirects=False,
            )

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location or redirect_count == MAX_REDIRECTS:
                    logger.warning("Image validation failed: too_many_redirects (%s)", _safe_url_for_log(current_url))
                    return ImageValidationResult(False, reason="too_many_redirects")
                current_url = urljoin(current_url, location)
                continue

            if response.status_code != 200:
                logger.warning("Image validation failed: http_status (%s)", _safe_url_for_log(current_url))
                return ImageValidationResult(False, reason="http_status")
            if _content_length_exceeds_limit(response.headers):
                logger.warning("Image validation failed: too_large (%s)", _safe_url_for_log(current_url))
                return ImageValidationResult(False, reason="too_large")
            if _is_rejected_content_type(response.headers.get("Content-Type", "")):
                logger.warning("Image validation failed: invalid_content_type (%s)", _safe_url_for_log(current_url))
                return ImageValidationResult(False, reason="invalid_content_type")

            output_directory = os.path.dirname(os.path.abspath(output_path))
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".image-download-",
                suffix=".tmp",
                dir=output_directory,
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                downloaded_bytes = 0
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > MAX_IMAGE_DOWNLOAD_BYTES:
                        logger.warning("Image validation failed: too_large (%s)", _safe_url_for_log(current_url))
                        return ImageValidationResult(False, reason="too_large")
                    temporary_file.write(chunk)

            validation_result = _validate_downloaded_image(temporary_path)
            if not validation_result.valid:
                logger.warning(
                    "Image validation failed: %s (%s)", validation_result.reason, _safe_url_for_log(current_url)
                )
                return validation_result

            os.replace(temporary_path, output_path)
            temporary_path = None
            return validation_result
    except requests.RequestException:
        logger.warning("Image validation failed: network_error (%s)", _safe_url_for_log(current_url))
        return ImageValidationResult(False, reason="network_error")
    except OSError:
        logger.warning("Image validation failed: file_error (%s)", _safe_url_for_log(current_url))
        return ImageValidationResult(False, reason="file_error")
    except ValueError:
        logger.warning("Image validation failed: unsafe_url (%s)", _safe_url_for_log(current_url))
        return ImageValidationResult(False, reason="unsafe_url")
    finally:
        if response is not None:
            response.close()
        _remove_file_if_present(temporary_path)

    logger.warning("Image validation failed: too_many_redirects (%s)", _safe_url_for_log(current_url))
    return ImageValidationResult(False, reason="too_many_redirects")


def validate_and_download_image(url: str, output_path: str) -> bool:
    """Backward-compatible boolean wrapper for secure image validation."""
    return validate_and_download_image_with_metadata(url, output_path).valid


_METADATA_PLACEHOLDERS = {
    "title": {"unknown", "untitled"},
    "artist": {"unknown", "unknown artist"},
    "date": {"unknown", "unknown date"},
}


def _is_metadata_placeholder(value: object, field: str) -> bool:
    """Return whether a field has no usable metadata, without substring matching."""
    if value is None or not isinstance(value, str):
        return True
    normalized_value = value.strip().casefold()
    return not normalized_value or normalized_value in _METADATA_PLACEHOLDERS[field]


def calculate_quality_score(artwork: NormalizedArtwork, museum_weights: dict) -> float:
    """
    Calculates a deterministic 0-100 score based on metadata and source.
    """
    score = 0.0
    available_points = 35.0  # Metadata (20) + source confidence (15) are always measurable.

    # 1. Image Quality / Resolution Info (Max 40), only with real pixel dimensions.
    image_width, image_height = normalize_image_dimensions(artwork.image_width, artwork.image_height)
    has_dimensions = image_width is not None and image_height is not None
    if has_dimensions:
        available_points += 60.0
        if max(image_width, image_height) >= 1080:
            score += 40.0
        elif max(artwork.image_width, artwork.image_height) >= 800:
            score += 30.0
        else:
            score += 15.0
        
    # 2. Core metadata usability (Max 20). Medium remains enrichment context,
    # not a quality signal: some adapters synthesize or omit it by source.
    meta_score = 20.0
    if _is_metadata_placeholder(artwork.title, "title"):
        meta_score -= 5
    if _is_metadata_placeholder(artwork.artist_name, "artist"):
        meta_score -= 10
    if _is_metadata_placeholder(artwork.creation_date, "date"):
        meta_score -= 5
    score += max(0, meta_score)
    
    # 3. Source Confidence (Max 15)
    # Uses the configured weights (defaults to 15 for all to keep them equal unless configured otherwise)
    source_weight = museum_weights.get(artwork.source, 15)
    score += min(15.0, float(source_weight))
    
    # 4. Instagram Suitability / Aspect Ratio (Max 20), only with real dimensions.
    if has_dimensions:
        ratio = image_width / image_height
        if 0.5 <= ratio <= 2.0:
            score += 20.0  # Portrait, square, and ordinary landscape are all usable.
        else:
            score += 10.0  # Extremely panoramic or tall sources are less flexible.

    # When API pixel metadata is unavailable, compare only the measured signals
    # on their available denominator instead of inventing resolution/ratio points.
    return min(100.0, max(0.0, (score / available_points) * 100.0))


def calculate_measurement_coverage(artwork: NormalizedArtwork) -> float:
    """Return the share of the quality denominator backed by measured signals."""
    image_width, image_height = normalize_image_dimensions(artwork.image_width, artwork.image_height)
    return 1.0 if image_width is not None and image_height is not None else 0.4
