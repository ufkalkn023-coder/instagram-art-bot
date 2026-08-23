"""Bounded, secret-free source failure classification for acquisition runs."""

from __future__ import annotations

from collections.abc import Mapping

import requests


SOURCE_FAILURE_CATEGORIES = frozenset(
    {
        "MISSING_CREDENTIAL",
        "AUTH_FAILED",
        "RATE_LIMITED",
        "HTTP_BLOCKED",
        "CLOUDFLARE_CHALLENGE",
        "NETWORK_ERROR",
        "API_ERROR",
        "INVALID_RESPONSE",
        "UNKNOWN",
    }
)

FAIL_FAST_SOURCE_FAILURE_CATEGORIES = frozenset(
    {"MISSING_CREDENTIAL", "AUTH_FAILED", "CLOUDFLARE_CHALLENGE"}
)


def is_cloudflare_challenge(status_code: object, headers: Mapping[str, object] | None) -> bool:
    """Identify Cloudflare's documented challenge signal without retaining headers."""
    if status_code != 403 or headers is None:
        return False
    normalized_headers = {str(name).casefold(): value for name, value in headers.items()}
    content_type = str(normalized_headers.get("content-type", "")).split(";", 1)[0].strip().casefold()
    mitigated = str(normalized_headers.get("cf-mitigated", "")).strip().casefold()
    return content_type == "text/html" and mitigated == "challenge"


def classify_http_failure(status_code: object, headers: Mapping[str, object] | None = None) -> str:
    if is_cloudflare_challenge(status_code, headers):
        return "CLOUDFLARE_CHALLENGE"
    if status_code == 401:
        return "AUTH_FAILED"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code == 403:
        return "HTTP_BLOCKED"
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return "API_ERROR"
    return "UNKNOWN"


def classify_exception(error: BaseException) -> str:
    return "NETWORK_ERROR" if isinstance(error, requests.RequestException) else "UNKNOWN"


def normalize_source_failure_category(category: object) -> str:
    return category if isinstance(category, str) and category in SOURCE_FAILURE_CATEGORIES else "UNKNOWN"
