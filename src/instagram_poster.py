from typing import Any, Callable, Optional
import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30
MEDIA_STATUS_TIMEOUT_SECONDS = 120
MEDIA_STATUS_POLL_INTERVAL_SECONDS = 5
TRANSIENT_RETRY_ATTEMPTS = 3
TRANSIENT_RETRY_BASE_DELAY_SECONDS = 1
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_CAROUSEL_ITEMS = 10
MIN_CAROUSEL_ITEMS = 2


class InstagramAPIError(RuntimeError):
    """A permanent or otherwise non-retryable Instagram API failure."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: int | None = None,
        error_type: str | None = None,
        error_subcode: int | None = None,
        fbtrace_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type
        self.error_subcode = error_subcode
        self.fbtrace_id = fbtrace_id


class InstagramTransientError(InstagramAPIError):
    """A retryable failure after retries have been exhausted."""


class InstagramAuthError(InstagramAPIError):
    """An authentication or permission failure that must not be retried."""


class InstagramMediaProcessingError(InstagramAPIError):
    """A media container did not become ready for publishing."""


class InstagramPublishAmbiguousError(InstagramAPIError):
    """Publishing may have succeeded but no media ID was safely obtained."""


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _backoff(attempt: int) -> None:
    time.sleep(TRANSIENT_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))


def _parse_json(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise InstagramAPIError(
            f"Instagram {operation} returned malformed JSON.",
            status_code=response.status_code,
        ) from error

    if not isinstance(payload, dict):
        raise InstagramAPIError(
            f"Instagram {operation} returned an unexpected JSON payload.",
            status_code=response.status_code,
        )
    return payload


def _error_from_response(response: requests.Response, payload: dict[str, Any], operation: str) -> InstagramAPIError:
    error = payload.get("error")
    error = error if isinstance(error, dict) else {}
    message = error.get("message") if isinstance(error.get("message"), str) else "Unknown Graph API error"
    error_code = error.get("code") if isinstance(error.get("code"), int) else None
    error_type = error.get("type") if isinstance(error.get("type"), str) else None
    error_subcode = error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None
    fbtrace_id = error.get("fbtrace_id") if isinstance(error.get("fbtrace_id"), str) else None
    status_code = response.status_code

    if error_code == 190:
        return InstagramAuthError(
            f"Instagram {operation} failed: access token is expired or invalid.",
            status_code=status_code,
            error_code=error_code,
            error_type=error_type,
            error_subcode=error_subcode,
            fbtrace_id=fbtrace_id,
        )

    exception_type = InstagramTransientError if status_code in TRANSIENT_HTTP_STATUS_CODES else InstagramAPIError
    return exception_type(
        f"Instagram {operation} failed (HTTP {status_code}, code {error_code}): {message}",
        status_code=status_code,
        error_code=error_code,
        error_type=error_type,
        error_subcode=error_subcode,
        fbtrace_id=fbtrace_id,
    )


def _request_json(
    request: Callable[..., requests.Response],
    operation: str,
    *,
    retry_transient: bool,
    **kwargs: Any,
) -> dict[str, Any]:
    for attempt in range(1, TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            response = request(timeout=HTTP_TIMEOUT_SECONDS, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as error:
            if retry_transient and attempt < TRANSIENT_RETRY_ATTEMPTS:
                _backoff(attempt)
                continue
            raise InstagramTransientError(f"Instagram {operation} timed out or lost its connection.") from error

        payload = _parse_json(response, operation)
        if 200 <= response.status_code < 300:
            return payload

        api_error = _error_from_response(response, payload, operation)
        if isinstance(api_error, InstagramTransientError) and retry_transient and attempt < TRANSIENT_RETRY_ATTEMPTS:
            _backoff(attempt)
            continue
        raise api_error

    raise AssertionError("unreachable")


def _require_id(payload: dict[str, Any], operation: str) -> str:
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise InstagramAPIError(f"Instagram {operation} response did not include a valid id.")
    return identifier


def _create_container(account_id: str, access_token: str, payload: dict[str, str], operation: str) -> str:
    url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media"
    response_payload = _request_json(
        requests.post,
        operation,
        retry_transient=True,
        url=url,
        data=payload,
        headers=_auth_headers(access_token),
    )
    return _require_id(response_payload, operation)


def _wait_until_finished(container_id: str, access_token: str) -> None:
    url = f"{config.GRAPH_API_BASE_URL}/{container_id}"
    deadline = time.monotonic() + MEDIA_STATUS_TIMEOUT_SECONDS

    while True:
        payload = _request_json(
            requests.get,
            "container status check",
            retry_transient=True,
            url=url,
            params={"fields": "status_code,status"},
            headers=_auth_headers(access_token),
        )
        status = payload.get("status_code")
        if not isinstance(status, str):
            raise InstagramMediaProcessingError("Instagram container status is missing or malformed.")

        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise InstagramMediaProcessingError(f"Instagram container entered {status} state.")
        if status == "PUBLISHED":
            raise InstagramMediaProcessingError("Instagram container was already published before this publish attempt.")
        if status != "IN_PROGRESS":
            raise InstagramMediaProcessingError(f"Instagram container returned unknown status: {status}.")
        if time.monotonic() >= deadline:
            raise InstagramMediaProcessingError("Instagram container did not reach FINISHED before polling timed out.")

        time.sleep(MEDIA_STATUS_POLL_INTERVAL_SECONDS)


def _publish_container(account_id: str, access_token: str, container_id: str) -> str:
    url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media_publish"
    payload = {"creation_id": container_id}

    for attempt in range(1, TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            response = requests.post(
                url,
                data=payload,
                headers=_auth_headers(access_token),
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError) as error:
            raise InstagramPublishAmbiguousError(
                "Instagram publish outcome is unknown after a network failure; do not retry automatically."
            ) from error

        try:
            response_payload = _parse_json(response, "media publish")
        except InstagramAPIError as error:
            raise InstagramPublishAmbiguousError(
                "Instagram publish outcome is unknown because the response was malformed."
            ) from error

        if 200 <= response.status_code < 300:
            try:
                return _require_id(response_payload, "media publish")
            except InstagramAPIError as error:
                raise InstagramPublishAmbiguousError(
                    "Instagram publish outcome is unknown because no media id was returned."
                ) from error

        api_error = _error_from_response(response, response_payload, "media publish")
        if isinstance(api_error, InstagramTransientError) and response.status_code == 429:
            if attempt < TRANSIENT_RETRY_ATTEMPTS:
                _backoff(attempt)
                continue
        if isinstance(api_error, InstagramTransientError):
            raise InstagramPublishAmbiguousError(
                "Instagram publish outcome is unknown after a transient server failure; do not retry automatically."
            ) from api_error
        raise api_error

    raise AssertionError("unreachable")


def _validate_credentials(account_id: str, access_token: str) -> None:
    if not account_id or not access_token:
        raise ValueError("INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN must be provided.")


def post_to_instagram_graph_api(
    media_url: str,
    caption: str,
    account_id: str,
    access_token: str,
    alt_text: Optional[str] = None,
    media_type: str = "IMAGE",
) -> str:
    """Create, wait for, and publish one Instagram media container."""
    _validate_credentials(account_id, access_token)

    payload = {"caption": caption}
    if media_type == "REELS":
        payload.update({"video_url": media_url, "media_type": "REELS"})
    else:
        payload["image_url"] = media_url
        if alt_text:
            payload["alt_text_custom"] = alt_text

    container_id = _create_container(account_id, access_token, payload, f"{media_type} container creation")
    _wait_until_finished(container_id, access_token)
    media_id = _publish_container(account_id, access_token, container_id)
    logger.info(f"Successfully published {media_type} post. Media ID: {media_id}")
    return media_id


def post_story_to_instagram_graph_api(image_url: str, account_id: str, access_token: str) -> str:
    """Create, wait for, and publish an Instagram story container."""
    _validate_credentials(account_id, access_token)
    container_id = _create_container(
        account_id,
        access_token,
        {"image_url": image_url, "media_type": "STORIES"},
        "Story container creation",
    )
    _wait_until_finished(container_id, access_token)
    return _publish_container(account_id, access_token, container_id)


def get_instagram_permalink(media_id: str, access_token: str) -> str | None:
    """Fetch a permalink without placing the access token in the request URL."""
    if not media_id or not access_token:
        return None

    try:
        payload = _request_json(
            requests.get,
            "permalink lookup",
            retry_transient=True,
            url=f"{config.GRAPH_API_BASE_URL}/{media_id}",
            params={"fields": "permalink"},
            headers=_auth_headers(access_token),
        )
    except InstagramAPIError as error:
        logger.warning(f"Instagram permalink lookup failed: {error}")
        return None
    permalink = payload.get("permalink")
    return permalink if isinstance(permalink, str) else None


def post_carousel_to_instagram_graph_api(media_urls: list[str], caption: str, account_id: str, access_token: str) -> str:
    """Publish a carousel only after every child and parent container is FINISHED."""
    _validate_credentials(account_id, access_token)
    if not MIN_CAROUSEL_ITEMS <= len(media_urls) <= MAX_CAROUSEL_ITEMS:
        raise ValueError(f"Instagram carousels require {MIN_CAROUSEL_ITEMS}-{MAX_CAROUSEL_ITEMS} media items.")

    child_container_ids = []
    for index, media_url in enumerate(media_urls, start=1):
        child_id = _create_container(
            account_id,
            access_token,
            {"image_url": media_url, "is_carousel_item": "true"},
            f"carousel item {index} creation",
        )
        _wait_until_finished(child_id, access_token)
        child_container_ids.append(child_id)

    carousel_id = _create_container(
        account_id,
        access_token,
        {"media_type": "CAROUSEL", "children": ",".join(child_container_ids), "caption": caption},
        "carousel container creation",
    )
    _wait_until_finished(carousel_id, access_token)
    media_id = _publish_container(account_id, access_token, carousel_id)
    logger.info(f"Successfully published carousel. Media ID: {media_id}")
    return media_id
