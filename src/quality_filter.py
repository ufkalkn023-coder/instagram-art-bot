import ipaddress
import logging
import os
import socket
import tempfile
import warnings
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from PIL import Image

from src.aic_image_policy import (
    AIC_ANALYSIS_WIDTH,
    AIC_FINAL_RENDER_WIDTH,
    AIC_RATE_LIMIT_STATUSES,
    ImageDownloadPurpose,
    aic_image_url_for_purpose,
    aic_request_headers,
    get_aic_image_request_policy,
    is_aic_iiif_url,
)
from src.models import NormalizedArtwork, normalize_image_dimensions
from src.source_health import is_cloudflare_challenge

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = (5, 20)
MAX_REDIRECTS = 4
MAX_IMAGE_DOWNLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024
ACCEPTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF"}
AIC_PREFERRED_IIIF_WIDTH = AIC_FINAL_RENDER_WIDTH
AIC_FALLBACK_IIIF_WIDTH = AIC_ANALYSIS_WIDTH
AIC_FALLBACK_HTTP_STATUSES = {400, 403, 404, 410, 422, 429}
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
    status_code: int | None = None
    aic_fallback_attempted: bool = False
    aic_fallback_recovered: bool = False
    http_status: int | None = None
    cloudflare_challenge: bool = False

    def __post_init__(self) -> None:
        """Keep donor and GitHub diagnostic field names backwards-compatible."""
        if self.status_code is None and self.http_status is not None:
            object.__setattr__(self, "status_code", self.http_status)
        elif self.http_status is None and self.status_code is not None:
            object.__setattr__(self, "http_status", self.status_code)


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


def validate_local_image_file(path: str) -> ImageValidationResult:
    """Validate an existing raster with the production decode policy."""
    return _validate_downloaded_image(path)


def _remove_file_if_present(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _aic_iiif_fallback_url(url: str) -> str | None:
    """Return the one documented smaller AIC derivative for a preferred URL."""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    if parsed.hostname and parsed.hostname.rstrip(".").casefold() == "www.artic.edu":
        preferred_segment = f"/full/{AIC_PREFERRED_IIIF_WIDTH},/0/default.jpg"
        if parsed.path.startswith("/iiif/2/") and parsed.path.endswith(preferred_segment):
            fallback_path = (
                parsed.path[: -len(preferred_segment)]
                + f"/full/{AIC_FALLBACK_IIIF_WIDTH},/0/default.jpg"
            )
            return urlunsplit(
                (parsed.scheme, parsed.netloc, fallback_path, parsed.query, parsed.fragment)
            )
    return None


def _validate_and_download_image_once(
    url: str,
    output_path: str,
    *,
    purpose: ImageDownloadPurpose = ImageDownloadPurpose.FINAL_RENDER,
    aic_fallback_request: bool = False,
    log_failure: bool = True,
) -> ImageValidationResult:
    """Run the complete secure validation pipeline for exactly one URL."""
    def reject(
        reason: str,
        current_url: str,
        *,
        status_code: int | None = None,
        headers: Dict[str, str] | None = None,
    ):
        if log_failure:
            logger.warning(
                "Image validation failed: %s (%s)",
                reason,
                _safe_url_for_log(current_url),
            )
        return ImageValidationResult(
            False,
            reason=reason,
            status_code=status_code,
            cloudflare_challenge=is_cloudflare_challenge(status_code, headers),
        )

    if not isinstance(url, str) or not _is_safe_image_url(url):
        return reject("unsafe_url", str(url))

    current_url = url
    visited_urls = set()
    temporary_path = None
    response = None

    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            if current_url in visited_urls or not _is_safe_image_url(current_url):
                return reject("unsafe_url", current_url)
            visited_urls.add(current_url)

            request_headers = aic_request_headers(
                current_url, {"User-Agent": "InstagramArtBot/1.0"}
            )
            def request():
                return requests.get(
                    current_url,
                    headers=request_headers,
                    stream=True,
                    timeout=HTTP_TIMEOUT_SECONDS,
                    allow_redirects=False,
                )
            response = get_aic_image_request_policy().request(
                current_url,
                purpose,
                request,
                fallback=aic_fallback_request,
            )
            if response is None:
                return reject("aic_circuit_open", current_url)

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location or redirect_count == MAX_REDIRECTS:
                    return reject("too_many_redirects", current_url)
                current_url = urljoin(current_url, location)
                continue

            if response.status_code != 200:
                return reject(
                    "http_status",
                    current_url,
                    status_code=response.status_code,
                    headers=response.headers,
                )
            if _content_length_exceeds_limit(response.headers):
                return reject("too_large", current_url)
            if _is_rejected_content_type(response.headers.get("Content-Type", "")):
                return reject("invalid_content_type", current_url)

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
                        return reject("too_large", current_url)
                    temporary_file.write(chunk)

            validation_result = _validate_downloaded_image(temporary_path)
            if not validation_result.valid:
                if log_failure:
                    logger.warning(
                        "Image validation failed: %s (%s)",
                        validation_result.reason,
                        _safe_url_for_log(current_url),
                    )
                return validation_result

            os.replace(temporary_path, output_path)
            temporary_path = None
            return validation_result
    except requests.RequestException:
        return reject("network_error", current_url)
    except OSError:
        return reject("file_error", current_url)
    except ValueError:
        return reject("unsafe_url", current_url)
    finally:
        if response is not None:
            response.close()
        _remove_file_if_present(temporary_path)

    return reject("too_many_redirects", current_url)


def validate_and_download_image_with_metadata(
    url: str,
    output_path: str,
    *,
    purpose: ImageDownloadPurpose = ImageDownloadPurpose.FINAL_RENDER,
) -> ImageValidationResult:
    """Safely download one purpose-sized image through the shared secure boundary."""
    selected_url = (
        aic_image_url_for_purpose(url, purpose) if isinstance(url, str) else url
    )
    is_aic_request = isinstance(selected_url, str) and is_aic_iiif_url(selected_url)
    fallback_url = (
        _aic_iiif_fallback_url(selected_url)
        if isinstance(selected_url, str)
        else None
    )
    preferred_result = _validate_and_download_image_once(
        selected_url,
        output_path,
        purpose=purpose,
        log_failure=not is_aic_request,
    )
    should_fallback = (
        fallback_url is not None
        and preferred_result.reason == "http_status"
        and preferred_result.status_code in AIC_FALLBACK_HTTP_STATUSES
    )
    # Analysis already starts at AIC's recommended 843px derivative. A single
    # paced retry is useful for transient rate limiting, but never becomes an
    # immediate 1686 -> 843 pair or an unbounded retry loop.
    should_retry_analysis = (
        is_aic_request
        and purpose is ImageDownloadPurpose.IMAGE_ANALYSIS
        and preferred_result.reason == "http_status"
        and preferred_result.status_code in AIC_RATE_LIMIT_STATUSES
    )
    if should_retry_analysis:
        retry_result = _validate_and_download_image_once(
            selected_url,
            output_path,
            purpose=purpose,
            log_failure=False,
        )
        get_aic_image_request_policy().record_outcome(valid=retry_result.valid)
        return retry_result

    if not should_fallback:
        if is_aic_request:
            get_aic_image_request_policy().record_outcome(
                valid=preferred_result.valid
            )
        return preferred_result

    fallback_result = _validate_and_download_image_once(
        fallback_url,
        output_path,
        purpose=purpose,
        aic_fallback_request=True,
        log_failure=False,
    )
    if fallback_result.valid:
        logger.debug(
            "AIC IIIF preferred derivative unavailable; secure 843px fallback recovered image"
        )
        result = ImageValidationResult(
            True,
            fallback_result.width,
            fallback_result.height,
            fallback_result.image_format,
            fallback_result.reason,
            fallback_result.status_code,
            aic_fallback_attempted=True,
            aic_fallback_recovered=True,
            cloudflare_challenge=(
                preferred_result.cloudflare_challenge
                or fallback_result.cloudflare_challenge
            ),
        )
        get_aic_image_request_policy().record_outcome(valid=True, recovered=True)
        return result

    result = ImageValidationResult(
        False,
        reason=fallback_result.reason,
        status_code=fallback_result.status_code,
        aic_fallback_attempted=True,
        aic_fallback_recovered=False,
        cloudflare_challenge=(
            preferred_result.cloudflare_challenge
            or fallback_result.cloudflare_challenge
        ),
    )
    get_aic_image_request_policy().record_outcome(valid=False)
    return result


def validate_and_download_image(
    url: str,
    output_path: str,
    *,
    purpose: ImageDownloadPurpose = ImageDownloadPurpose.FINAL_RENDER,
) -> bool:
    """Backward-compatible boolean wrapper for secure image validation."""
    return validate_and_download_image_with_metadata(
        url, output_path, purpose=purpose
    ).valid


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

    # 1. Image Quality / Resolution Info (Max 60), only with real pixel dimensions.
    # Aspect ratio is deliberately not an editorial-quality signal; platform
    # publishability is evaluated separately after the secure download. The
    # existing 20-point image-evidence weight remains neutral across every
    # shape so ordinary candidate scores do not shift as a side effect.
    image_width, image_height = normalize_image_dimensions(artwork.image_width, artwork.image_height)
    has_dimensions = image_width is not None and image_height is not None
    if has_dimensions:
        available_points += 60.0
        score += 20.0
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
    
    # When API pixel metadata is unavailable, compare only the measured signals
    # on their available denominator instead of inventing resolution points.
    return min(100.0, max(0.0, (score / available_points) * 100.0))


def calculate_measurement_coverage(artwork: NormalizedArtwork) -> float:
    """Return the share of the quality denominator backed by measured signals."""
    image_width, image_height = normalize_image_dimensions(artwork.image_width, artwork.image_height)
    return 1.0 if image_width is not None and image_height is not None else 0.4
