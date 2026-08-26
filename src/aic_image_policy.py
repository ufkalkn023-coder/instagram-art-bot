"""Shared, process-local policy for Art Institute of Chicago HTTP traffic."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, TypeVar
from urllib.parse import urlsplit, urlunsplit


AIC_API_HOST = "api.artic.edu"
AIC_IIIF_HOST = "www.artic.edu"
AIC_USER_AGENT = "Artfolio Instagram Art Bot"
AIC_REQUEST_INTERVAL_SECONDS = 1.0
AIC_RATE_LIMIT_STATUSES = {403, 429}
AIC_RATE_LIMIT_FAILURE_THRESHOLD = 3
AIC_ANALYSIS_WIDTH = 843
AIC_FINAL_RENDER_WIDTH = 1686


class ImageDownloadPurpose(str, Enum):
    """The smallest useful distinction in downstream image resolution needs."""

    IMAGE_ANALYSIS = "image_analysis"
    FINAL_RENDER = "final_render"


@dataclass(frozen=True)
class AICImageRequestDiagnostics:
    analysis_843: int = 0
    final_1686: int = 0
    fallback_843: int = 0
    rate_limited: int = 0
    recovered: int = 0
    failed: int = 0
    circuit_open: bool = False


ResponseT = TypeVar("ResponseT")


def _normalized_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".").casefold()
    except (TypeError, ValueError):
        return ""


def is_aic_http_url(url: str) -> bool:
    """Return whether a URL belongs to an official AIC API or IIIF host."""
    return _normalized_host(url) in {AIC_API_HOST, AIC_IIIF_HOST}


def is_aic_iiif_url(url: str) -> bool:
    """Return whether a URL is an AIC IIIF image request."""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return (parsed.hostname or "").rstrip(
        "."
    ).casefold() == AIC_IIIF_HOST and parsed.path.startswith("/iiif/2/")


def aic_request_headers(url: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Add AIC's documented non-secret application identifier on AIC requests."""
    headers = dict(base or {})
    if is_aic_http_url(url):
        headers["AIC-User-Agent"] = AIC_USER_AGENT
    return headers


def aic_image_url_for_purpose(url: str, purpose: ImageDownloadPurpose) -> str:
    """Select AIC's documented 843px analysis derivative when appropriate."""
    if purpose is not ImageDownloadPurpose.IMAGE_ANALYSIS or not is_aic_iiif_url(url):
        return url
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return url
    preferred = f"/full/{AIC_FINAL_RENDER_WIDTH},/0/default.jpg"
    if not parsed.path.endswith(preferred):
        return url
    path = parsed.path[: -len(preferred)] + f"/full/{AIC_ANALYSIS_WIDTH},/0/default.jpg"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


class AICImageRequestPolicy:
    """Serialize, pace, and health-protect AIC IIIF requests in this process."""

    def __init__(
        self,
        *,
        interval_seconds: float = AIC_REQUEST_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request_started: float | None = None
        self._consecutive_rate_limits = 0
        self._diagnostics = AICImageRequestDiagnostics()

    def request(
        self,
        url: str,
        purpose: ImageDownloadPurpose,
        request: Callable[[], ResponseT],
        *,
        fallback: bool = False,
    ) -> ResponseT | None:
        """Run one physical request while holding the shared AIC image lock."""
        if not is_aic_iiif_url(url):
            return request()

        with self._lock:
            if self._diagnostics.circuit_open:
                return None
            now = self._monotonic()
            if self._last_request_started is not None:
                remaining = self.interval_seconds - (now - self._last_request_started)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_started = self._monotonic()
            response = request()
            width = _iiif_width(url)
            if fallback and width == AIC_ANALYSIS_WIDTH:
                self._diagnostics = replace(
                    self._diagnostics,
                    fallback_843=self._diagnostics.fallback_843 + 1,
                )
            elif (
                purpose is ImageDownloadPurpose.IMAGE_ANALYSIS
                and width == AIC_ANALYSIS_WIDTH
            ):
                self._diagnostics = replace(
                    self._diagnostics,
                    analysis_843=self._diagnostics.analysis_843 + 1,
                )
            elif (
                purpose is ImageDownloadPurpose.FINAL_RENDER
                and width == AIC_FINAL_RENDER_WIDTH
            ):
                self._diagnostics = replace(
                    self._diagnostics,
                    final_1686=self._diagnostics.final_1686 + 1,
                )

            status_code = getattr(response, "status_code", None)
            if status_code in AIC_RATE_LIMIT_STATUSES:
                self._consecutive_rate_limits += 1
                circuit_open = (
                    self._consecutive_rate_limits >= AIC_RATE_LIMIT_FAILURE_THRESHOLD
                )
                self._diagnostics = replace(
                    self._diagnostics,
                    rate_limited=self._diagnostics.rate_limited + 1,
                    circuit_open=circuit_open,
                )
            else:
                self._consecutive_rate_limits = 0
            return response

    def record_outcome(self, *, valid: bool, recovered: bool = False) -> None:
        """Record one completed logical AIC download without per-candidate logging."""
        with self._lock:
            self._diagnostics = replace(
                self._diagnostics,
                recovered=self._diagnostics.recovered + int(recovered),
                failed=self._diagnostics.failed + int(not valid),
            )

    def diagnostics(self) -> AICImageRequestDiagnostics:
        with self._lock:
            return self._diagnostics


def _iiif_width(url: str) -> int | None:
    try:
        path = urlsplit(url).path
    except (TypeError, ValueError):
        return None
    for width in (AIC_FINAL_RENDER_WIDTH, AIC_ANALYSIS_WIDTH):
        if f"/full/{width},/0/default.jpg" in path:
            return width
    return None


_SHARED_AIC_IMAGE_POLICY = AICImageRequestPolicy()


def get_aic_image_request_policy() -> AICImageRequestPolicy:
    """Return the single process-local AIC image policy used by every caller."""
    return _SHARED_AIC_IMAGE_POLICY


def reset_aic_image_request_policy_for_tests(
    policy: AICImageRequestPolicy | None = None,
) -> AICImageRequestPolicy:
    """Replace process-local state; intended for deterministic test isolation."""
    global _SHARED_AIC_IMAGE_POLICY
    _SHARED_AIC_IMAGE_POLICY = policy or AICImageRequestPolicy()
    return _SHARED_AIC_IMAGE_POLICY
