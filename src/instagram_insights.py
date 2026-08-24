"""Read-only client for Instagram feed media insights."""

import logging
import math
import os
from dataclasses import dataclass
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

TARGET_METRICS = (
    "views",
    "reach",
    "likes",
    "comments",
    "saved",
    "shares",
    "total_interactions",
)
REQUEST_TIMEOUT_SECONDS = 20


class InstagramInsightsError(Exception):
    """A safe, analytics-only Instagram Insights failure."""


class InstagramInsightsConfigurationError(InstagramInsightsError):
    """Insights cannot run because local configuration is invalid."""


class InstagramInsightsRequestError(InstagramInsightsError):
    """The Insights endpoint could not be read safely."""


class InstagramInsightsPermissionError(InstagramInsightsRequestError):
    """The access token lacks Insights access or is no longer valid."""


@dataclass(frozen=True)
class InsightsResponse:
    metrics: dict[str, int | float]
    requested_metrics: tuple[str, ...]
    returned_metrics: tuple[str, ...]
    missing_metrics: tuple[str, ...]


def _safe_media_suffix(media_id: str) -> str:
    return media_id[-6:] if len(media_id) > 6 else media_id


def _validated_access_token(value: str | None) -> str:
    token = (value or "").strip()
    if not token or "\n" in token or "\r" in token:
        raise InstagramInsightsConfigurationError("INSTAGRAM_ACCESS_TOKEN is missing or malformed")
    return token


def _usable_metric_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _parse_metrics(payload: Any) -> dict[str, int | float]:
    """Keep only supported metrics with a valid, actually returned value."""
    if not isinstance(payload, dict):
        raise InstagramInsightsRequestError("Instagram Insights returned malformed JSON")

    entries = payload.get("data")
    if entries is None:
        raise InstagramInsightsRequestError("Instagram Insights response is missing data")
    if not isinstance(entries, list):
        raise InstagramInsightsRequestError("Instagram Insights response data is malformed")

    metrics: dict[str, int | float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        values = entry.get("values")
        if name not in TARGET_METRICS or not isinstance(values, list) or not values:
            continue
        first_value = values[0]
        if not isinstance(first_value, dict):
            continue
        value = _usable_metric_value(first_value.get("value"))
        if value is not None:
            metrics[name] = value
    return metrics


class InstagramInsightsClient:
    """Fetches parent media insights and never changes Instagram state."""

    def __init__(self, access_token: str | None = None, session=requests):
        self._access_token = _validated_access_token(
            access_token if access_token is not None else os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        )
        self._session = session

    def fetch_media_insights(self, media_id: str) -> InsightsResponse:
        if not isinstance(media_id, str) or not media_id.strip() or "\n" in media_id or "\r" in media_id:
            raise InstagramInsightsConfigurationError("Instagram media ID is missing or malformed")

        normalized_media_id = media_id.strip()
        url = f"{config.GRAPH_API_BASE_URL}/{normalized_media_id}/insights"
        try:
            response = self._session.get(
                url,
                params={"metric": ",".join(TARGET_METRICS), "access_token": self._access_token},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("Instagram Insights network error for media suffix %s", _safe_media_suffix(normalized_media_id))
            raise InstagramInsightsRequestError("Instagram Insights request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "Instagram Insights returned invalid JSON for media suffix %s (HTTP %s)",
                _safe_media_suffix(normalized_media_id), response.status_code,
            )
            raise InstagramInsightsRequestError("Instagram Insights returned invalid JSON") from exc

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            error_code = error.get("code") if isinstance(error, dict) else None
            logger.warning(
                "Instagram Insights request rejected for media suffix %s (HTTP %s)",
                _safe_media_suffix(normalized_media_id), response.status_code,
            )
            if response.status_code in {401, 403} or error_code in {10, 190, 200}:
                raise InstagramInsightsPermissionError(
                    "Instagram Insights request rejected; verify instagram_manage_insights and related permissions."
                )
            raise InstagramInsightsRequestError(f"Instagram Insights request failed with HTTP {response.status_code}")

        metrics = _parse_metrics(payload)
        returned_metrics = tuple(metric for metric in TARGET_METRICS if metric in metrics)
        return InsightsResponse(
            metrics=metrics,
            requested_metrics=TARGET_METRICS,
            returned_metrics=returned_metrics,
            missing_metrics=tuple(metric for metric in TARGET_METRICS if metric not in metrics),
        )
