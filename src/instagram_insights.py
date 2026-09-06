"""GET-only Instagram media discovery and Insights client."""

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests

import config

logger = logging.getLogger(__name__)

# Keep Meta metric names unchanged in persisted raw snapshots. Unsupported
# metrics are isolated by splitting failed requests, so one metric cannot make
# an otherwise useful snapshot fail.
CORE_METRICS = ("views", "reach", "likes", "comments", "saved", "shares")
OPTIONAL_METRICS = (
    "total_interactions",
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "clips_replays_count",
    "ig_reels_aggregated_all_plays_count",
)
TARGET_METRICS = CORE_METRICS + OPTIONAL_METRICS
MEDIA_FIELDS = ("id", "media_type", "media_product_type", "caption", "permalink", "timestamp")
REQUEST_TIMEOUT_SECONDS = 20
DISCOVERY_PAGE_LIMIT = 100
DISCOVERY_MAX_PAGES = 2
MAX_META_MESSAGE_LENGTH = 500
_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)(access_token|appsecret_proof)=([^&\s]+)"
)
_AUTHORIZATION_PATTERN = re.compile(r"(?i)authorization\s*:\s*(?:bearer\s+)?\S+")


class InstagramInsightsError(Exception):
    """A safe, analytics-only Instagram API failure."""


class InstagramInsightsConfigurationError(InstagramInsightsError):
    """Insights cannot run because local configuration is invalid."""


class InstagramInsightsRequestError(InstagramInsightsError):
    """A read-only Instagram endpoint could not be read safely."""


class InstagramInsightsPermissionError(InstagramInsightsRequestError):
    """The access token lacks permission for the requested read."""


class InstagramInsightsAuthenticationError(InstagramInsightsRequestError):
    """The configured access token is invalid, expired, or unparsable."""


@dataclass(frozen=True)
class InstagramMedia:
    id: str
    media_type: str
    media_product_type: str | None
    caption: str | None
    permalink: str | None
    timestamp: str


@dataclass(frozen=True)
class MediaDiscoveryResponse:
    media: tuple[InstagramMedia, ...]
    api_calls: int
    rate_limit_usage: dict[str, int | float] = field(default_factory=dict)


@dataclass(frozen=True)
class InsightsResponse:
    metrics: dict[str, int | float]
    requested_metrics: tuple[str, ...]
    returned_metrics: tuple[str, ...]
    missing_metrics: tuple[str, ...]
    api_calls: int = 1
    rate_limit_usage: dict[str, int | float] = field(default_factory=dict)
    permanent_failure_category: str | None = None
    permanently_failed_metrics: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def permanently_unavailable(self) -> bool:
        return self.permanent_failure_category is not None


@dataclass(frozen=True)
class _MetricGroupResponse:
    metrics: dict[str, int | float]
    api_calls: int
    permanent_failures: dict[str, str] = field(default_factory=dict)


def _safe_media_suffix(media_id: str) -> str:
    return media_id[-6:] if len(media_id) > 6 else media_id


def _safe_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _sanitize_meta_text(value: Any, access_token: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unavailable"
    sanitized = value.replace(access_token, "[REDACTED]")
    sanitized = _SECRET_QUERY_PATTERN.sub(r"\1=[REDACTED]", sanitized)
    sanitized = _AUTHORIZATION_PATTERN.sub("Authorization: [REDACTED]", sanitized)
    sanitized = " ".join(sanitized.split())
    return sanitized[:MAX_META_MESSAGE_LENGTH]


def _meta_error_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validated_access_token(value: str | None) -> str:
    token = (value or "").strip()
    if not token or "\n" in token or "\r" in token:
        raise InstagramInsightsConfigurationError("INSTAGRAM_ACCESS_TOKEN is missing or malformed")
    return token


def _validated_identifier(value: str | None, label: str) -> str:
    identifier = (value or "").strip()
    if not identifier or not identifier.isascii() or not all(character.isalnum() or character in {"-", "_"} for character in identifier):
        raise InstagramInsightsConfigurationError(f"{label} is missing or malformed")
    return identifier


def _usable_metric_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _parse_metrics(payload: Any, requested_metrics: Iterable[str] = TARGET_METRICS) -> dict[str, int | float]:
    """Keep only requested metrics with a valid, actually returned value."""
    if not isinstance(payload, dict):
        raise InstagramInsightsRequestError("Instagram Insights returned malformed JSON")

    entries = payload.get("data")
    if entries is None:
        raise InstagramInsightsRequestError("Instagram Insights response is missing data")
    if not isinstance(entries, list):
        raise InstagramInsightsRequestError("Instagram Insights response data is malformed")

    requested = set(requested_metrics)
    metrics: dict[str, int | float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name not in requested:
            continue
        values = entry.get("values")
        raw_value: Any = None
        if isinstance(values, list) and values and isinstance(values[0], dict):
            raw_value = values[0].get("value")
        elif isinstance(entry.get("total_value"), dict):
            raw_value = entry["total_value"].get("value")
        value = _usable_metric_value(raw_value)
        if value is not None:
            metrics[name] = value
    return metrics


def _numeric_usage(prefix: str, value: Any, output: dict[str, int | float]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"call_count", "total_cputime", "total_time", "estimated_time_to_regain_access"}:
                metric = _usable_metric_value(nested)
                if metric is not None:
                    name = f"{prefix}.{key}"
                    output[name] = max(output.get(name, 0), metric)
            elif isinstance(nested, (dict, list)):
                _numeric_usage(prefix, nested, output)
    elif isinstance(value, list):
        for nested in value:
            _numeric_usage(prefix, nested, output)


def parse_rate_limit_headers(headers: Any) -> dict[str, int | float]:
    """Extract only anonymous numeric usage values from Meta usage headers."""
    if headers is None or not hasattr(headers, "get"):
        return {}
    parsed: dict[str, int | float] = {}
    for header, prefix in (
        ("x-app-usage", "app"),
        ("x-page-usage", "page"),
        ("x-business-use-case-usage", "business"),
    ):
        raw = headers.get(header) or headers.get(header.title())
        if not isinstance(raw, str):
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        _numeric_usage(prefix, value, parsed)
    return parsed


def _merge_usage(target: dict[str, int | float], update: dict[str, int | float]) -> None:
    for key, value in update.items():
        target[key] = max(target.get(key, 0), value)


def _media_from_payload(value: Any) -> InstagramMedia | None:
    if not isinstance(value, dict):
        return None
    media_id = value.get("id")
    media_type = value.get("media_type")
    timestamp = value.get("timestamp")
    if not all(isinstance(item, str) and item.strip() for item in (media_id, media_type, timestamp)):
        return None
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        # Meta commonly emits +0000. Python 3.11 accepts it in fromisoformat,
        # while the production Python 3.10 runtime requires explicit %z parsing.
        try:
            parsed_timestamp = datetime.strptime(
                timestamp, "%Y-%m-%dT%H:%M:%S%z"
            )
        except ValueError:
            return None
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        return None
    product_type = value.get("media_product_type")
    caption = value.get("caption")
    permalink = value.get("permalink")
    return InstagramMedia(
        id=media_id.strip(),
        media_type=media_type.strip().upper(),
        media_product_type=product_type.strip().upper() if isinstance(product_type, str) and product_type.strip() else None,
        caption=caption if isinstance(caption, str) else None,
        permalink=permalink if isinstance(permalink, str) and permalink.startswith("https://") else None,
        timestamp=parsed_timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def is_reel_media(media: InstagramMedia) -> bool:
    """Accept explicit Reels and legacy video records lacking product type."""
    return media.media_product_type == "REELS" or (
        media.media_type == "VIDEO" and media.media_product_type in {None, "REELS"}
    )


class InstagramInsightsClient:
    """Reads owned media and media insights; it exposes no mutating method."""

    def __init__(self, access_token: str | None = None, session=requests):
        self._access_token = _validated_access_token(
            access_token if access_token is not None else os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        )
        self._session = session

    def _get(self, url: str, params: dict[str, Any], media_id: str | None = None):
        safe_params = {**params, "access_token": self._access_token}
        try:
            response = self._session.get(url, params=safe_params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            suffix = _safe_media_suffix(media_id) if media_id else "account"
            logger.warning("Instagram read network error for %s", suffix)
            raise InstagramInsightsRequestError("Instagram read request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("Instagram read returned invalid JSON (HTTP %s)", response.status_code)
            raise InstagramInsightsRequestError("Instagram read returned invalid JSON") from exc
        return response, payload, parse_rate_limit_headers(getattr(response, "headers", None))

    def _meta_error_diagnostic(
        self,
        response,
        payload: Any,
        context: str,
        endpoint: str,
    ) -> tuple[str, int | None]:
        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = _meta_error_number(error.get("code")) if isinstance(error, dict) else None
        error_subcode = _meta_error_number(error.get("error_subcode")) if isinstance(error, dict) else None
        error_type = _sanitize_meta_text(error.get("type"), self._access_token) if isinstance(error, dict) else "unavailable"
        message = _sanitize_meta_text(error.get("message"), self._access_token) if isinstance(error, dict) else "unavailable"
        diagnostic = (
            f"Meta {context} rejected: HTTP {response.status_code} "
            f"type={error_type} code={error_code if error_code is not None else 'unavailable'} "
            f"error_subcode={error_subcode if error_subcode is not None else 'unavailable'} "
            f"message={message} endpoint={_safe_endpoint(endpoint)} "
            f"api_version={config.INSTAGRAM_GRAPH_API_VERSION}"
        )
        return diagnostic, error_code

    def _raise_for_error(self, response, payload: Any, context: str, endpoint: str) -> None:
        if response.status_code < 400:
            return
        diagnostic, error_code = self._meta_error_diagnostic(
            response,
            payload,
            context,
            endpoint,
        )
        logger.warning("%s", diagnostic)
        if response.status_code == 401 or error_code == 190:
            raise InstagramInsightsAuthenticationError(
                f"{diagnostic}; replace or refresh INSTAGRAM_ACCESS_TOKEN."
            )
        if response.status_code == 403 or error_code in {10, 200}:
            raise InstagramInsightsPermissionError(
                f"{diagnostic}; verify instagram_basic, instagram_manage_insights, and pages_read_engagement."
            )
        raise InstagramInsightsRequestError(diagnostic)

    def discover_recent_media(self, account_id: str | None = None) -> MediaDiscoveryResponse:
        account = _validated_identifier(
            account_id if account_id is not None else os.environ.get("INSTAGRAM_ACCOUNT_ID"),
            "INSTAGRAM_ACCOUNT_ID",
        )
        if self._access_token == account:
            raise InstagramInsightsConfigurationError(
                "INSTAGRAM_ACCESS_TOKEN contains INSTAGRAM_ACCOUNT_ID instead of a Meta access token"
            )
        url = f"{config.GRAPH_API_BASE_URL}/{account}/media"
        params: dict[str, Any] = {"fields": ",".join(MEDIA_FIELDS), "limit": DISCOVERY_PAGE_LIMIT}
        media: list[InstagramMedia] = []
        usage: dict[str, int | float] = {}
        api_calls = 0
        for _ in range(DISCOVERY_MAX_PAGES):
            response, payload, response_usage = self._get(url, params)
            api_calls += 1
            _merge_usage(usage, response_usage)
            self._raise_for_error(response, payload, "media discovery", url)
            entries = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise InstagramInsightsRequestError("Instagram media discovery response is malformed")
            for entry in entries:
                parsed = _media_from_payload(entry)
                if parsed is not None and is_reel_media(parsed):
                    media.append(parsed)
            paging = payload.get("paging") if isinstance(payload, dict) else None
            cursors = paging.get("cursors") if isinstance(paging, dict) else None
            after = cursors.get("after") if isinstance(cursors, dict) else None
            next_url = paging.get("next") if isinstance(paging, dict) else None
            if not (isinstance(after, str) and after and isinstance(next_url, str) and next_url.startswith("https://")):
                break
            params = {"fields": ",".join(MEDIA_FIELDS), "limit": DISCOVERY_PAGE_LIMIT, "after": after}
        unique = {item.id: item for item in media}
        return MediaDiscoveryResponse(tuple(unique.values()), api_calls, usage)

    def fetch_media(self, media_id: str) -> InstagramMedia:
        normalized = _validated_identifier(media_id, "Instagram media ID")
        response, payload, _ = self._get(
            f"{config.GRAPH_API_BASE_URL}/{normalized}",
            {"fields": ",".join(MEDIA_FIELDS)},
            normalized,
        )
        self._raise_for_error(response, payload, "media lookup", f"{config.GRAPH_API_BASE_URL}/{normalized}")
        media = _media_from_payload(payload)
        if media is None:
            raise InstagramInsightsRequestError("Instagram media lookup response is malformed")
        if not is_reel_media(media):
            raise InstagramInsightsRequestError("Instagram media is not a Reel")
        return media

    def _fetch_metric_group(
        self,
        media_id: str,
        metrics: tuple[str, ...],
        usage: dict[str, int | float],
    ) -> _MetricGroupResponse:
        endpoint = f"{config.GRAPH_API_BASE_URL}/{media_id}/insights"
        response, payload, response_usage = self._get(
            endpoint,
            {"metric": ",".join(metrics)},
            media_id,
        )
        _merge_usage(usage, response_usage)
        if response.status_code < 400:
            return _MetricGroupResponse(_parse_metrics(payload, metrics), 1)

        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = _meta_error_number(error.get("code")) if isinstance(error, dict) else None
        if response.status_code in {401, 403} or error_code in {10, 190, 200}:
            self._raise_for_error(response, payload, "Insights", endpoint)
        if response.status_code != 400 or error_code != 100:
            self._raise_for_error(response, payload, "Insights", endpoint)
        if len(metrics) == 1:
            diagnostic, _ = self._meta_error_diagnostic(
                response,
                payload,
                f"Insights metric={metrics[0]}",
                endpoint,
            )
            logger.info("Instagram metric permanently unavailable: %s", diagnostic)
            return _MetricGroupResponse({}, 1, {metrics[0]: diagnostic})
        midpoint = len(metrics) // 2
        left = self._fetch_metric_group(media_id, metrics[:midpoint], usage)
        right = self._fetch_metric_group(media_id, metrics[midpoint:], usage)
        return _MetricGroupResponse(
            metrics={**left.metrics, **right.metrics},
            api_calls=1 + left.api_calls + right.api_calls,
            permanent_failures={
                **left.permanent_failures,
                **right.permanent_failures,
            },
        )

    def fetch_media_insights(self, media_id: str) -> InsightsResponse:
        normalized = _validated_identifier(media_id, "Instagram media ID")
        usage: dict[str, int | float] = {}
        result = self._fetch_metric_group(normalized, TARGET_METRICS, usage)
        returned_metrics = tuple(metric for metric in TARGET_METRICS if metric in result.metrics)
        permanently_failed = tuple(
            metric for metric in TARGET_METRICS if metric in result.permanent_failures
        )
        all_permanently_failed = len(permanently_failed) == len(TARGET_METRICS)
        return InsightsResponse(
            metrics=result.metrics,
            requested_metrics=TARGET_METRICS,
            returned_metrics=returned_metrics,
            missing_metrics=tuple(
                metric for metric in TARGET_METRICS if metric not in result.metrics
            ),
            api_calls=result.api_calls,
            rate_limit_usage=usage,
            permanent_failure_category=(
                "all_metrics_permanently_unavailable"
                if all_permanently_failed
                else None
            ),
            permanently_failed_metrics=permanently_failed,
            diagnostics=tuple(
                result.permanent_failures[metric] for metric in permanently_failed
            ),
        )
