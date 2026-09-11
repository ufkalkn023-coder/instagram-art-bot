"""Fail-closed Cloudflare R2 staging-media ownership and cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Sequence

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)
import requests


logger = logging.getLogger(__name__)

OWNED_MEDIA_ROOT = "images/publications"
REEL_OWNED_MEDIA_ROOT = "reels/publications"
MEDIA_OPERATION_ATTEMPTS = 3
PUBLIC_HEALTH_CHECK_ATTEMPTS = 3
PUBLICATION_LIST_PAGE_SIZE = 25
PUBLICATION_CLEANUP_MAX_OBJECTS = 100
PUBLICATION_CLEANUP_MAX_PAGES = (
    PUBLICATION_CLEANUP_MAX_OBJECTS // PUBLICATION_LIST_PAGE_SIZE
) + 1
R2_CLIENT_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"total_max_attempts": 1, "mode": "standard"},
)

_PUBLICATION_ID_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?"
)
_OWNED_OBJECT_KEY_PATTERN = re.compile(
    rf"{re.escape(OWNED_MEDIA_ROOT)}/"
    r"(?P<publication_id>[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?)/"
    r"(?P<timestamp>[0-9]{14})_(?P<nonce>[0-9a-f]{32})"
    r"(?P<suffix>\.jpg|\.png|\.webp)"
)
_OWNED_REEL_OBJECT_KEY_PATTERN = re.compile(
    rf"{re.escape(REEL_OWNED_MEDIA_ROOT)}/"
    r"(?P<publication_id>[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?)/"
    r"(?P<timestamp>[0-9]{14})_(?P<nonce>[0-9a-f]{32})"
    r"(?P<suffix>\.mp4)"
)
_TRANSIENT_R2_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_R2_ERROR_CODES = {
    "InternalError",
    "RequestTimeout",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
}
_TRANSIENT_R2_EXCEPTIONS = (
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)
_MISSING_R2_ERROR_CODES = {"NoSuchKey", "NotFound", "404"}


@dataclass(frozen=True)
class TempMediaUpload:
    """An exact application-owned R2 object and the URL supplied to Meta."""

    object_key: str
    public_url: str
    publication_id: str

    def __post_init__(self) -> None:
        validate_owned_object_key(self.object_key, self.publication_id)
        if not isinstance(self.public_url, str) or not self.public_url:
            raise ValueError("Temporary media public URL must not be empty")


@dataclass(frozen=True)
class TempReelUpload:
    """An exact application-owned Reel MP4 object and its public URL."""

    object_key: str
    public_url: str
    publication_id: str

    def __post_init__(self) -> None:
        validate_owned_reel_object_key(self.object_key, self.publication_id)
        if not isinstance(self.public_url, str) or not self.public_url:
            raise ValueError("Temporary Reel public URL must not be empty")


@dataclass(frozen=True)
class MediaCleanupSummary:
    publication_id: str
    discovered: int
    deleted: int
    failures: int
    complete: bool
    reason: str


@dataclass(frozen=True)
class _R2MediaConfiguration:
    account_id: str
    access_key: str
    secret_key: str
    bucket_name: str
    public_url_base: str | None


def is_valid_publication_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and _PUBLICATION_ID_PATTERN.fullmatch(value) is not None
    )


def validate_publication_id(value: object) -> str:
    if not is_valid_publication_id(value):
        raise ValueError("Publication ID is not a normalized application identifier")
    return value


def publication_media_prefix(publication_id: object) -> str:
    normalized = validate_publication_id(publication_id)
    return f"{OWNED_MEDIA_ROOT}/{normalized}/"


def reel_publication_media_prefix(publication_id: object) -> str:
    normalized = validate_publication_id(publication_id)
    return f"{REEL_OWNED_MEDIA_ROOT}/{normalized}/"


def validate_owned_object_key(object_key: object, publication_id: object) -> str:
    normalized_publication_id = validate_publication_id(publication_id)
    if not isinstance(object_key, str):
        raise ValueError("Temporary media object key must be a string")
    match = _OWNED_OBJECT_KEY_PATTERN.fullmatch(object_key)
    if match is None or match.group("publication_id") != normalized_publication_id:
        raise ValueError("Refusing to operate on a non-owned temporary media key")
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S")
    except ValueError as error:
        raise ValueError(
            "Refusing to operate on a malformed temporary media key"
        ) from error
    return object_key


def validate_owned_reel_object_key(object_key: object, publication_id: object) -> str:
    normalized_publication_id = validate_publication_id(publication_id)
    if not isinstance(object_key, str):
        raise ValueError("Temporary Reel object key must be a string")
    match = _OWNED_REEL_OBJECT_KEY_PATTERN.fullmatch(object_key)
    if match is None or match.group("publication_id") != normalized_publication_id:
        raise ValueError("Refusing to operate on a non-owned temporary Reel key")
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S")
    except ValueError as error:
        raise ValueError(
            "Refusing to operate on a malformed temporary Reel key"
        ) from error
    return object_key


def _load_configuration(*, require_public_url: bool) -> _R2MediaConfiguration:
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
    bucket_name = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
    public_url_base = os.environ.get("CLOUDFLARE_R2_PUBLIC_URL", "").strip()
    required = [account_id, access_key, secret_key, bucket_name]
    if require_public_url:
        required.append(public_url_base)
    if not all(required):
        raise ValueError("Missing one or more CLOUDFLARE_R2_* environment variables")
    return _R2MediaConfiguration(
        account_id=account_id,
        access_key=access_key,
        secret_key=secret_key,
        bucket_name=bucket_name,
        public_url_base=public_url_base.rstrip("/") if public_url_base else None,
    )


def _get_s3_client(configuration: _R2MediaConfiguration):
    return boto3.client(
        "s3",
        endpoint_url=(
            f"https://{configuration.account_id}.r2.cloudflarestorage.com"
        ),
        aws_access_key_id=configuration.access_key,
        aws_secret_access_key=configuration.secret_key,
        region_name="auto",
        config=R2_CLIENT_CONFIG,
    )


def _is_transient_r2_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSIENT_R2_EXCEPTIONS):
            return True
        if isinstance(current, ClientError):
            response = current.response
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            return (
                status in _TRANSIENT_R2_HTTP_STATUSES
                or code in _TRANSIENT_R2_ERROR_CODES
            )
        current = current.__cause__ or current.__context__
    return False


def _is_missing_r2_object(error: BaseException) -> bool:
    if not isinstance(error, ClientError):
        return False
    response = error.response
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(response.get("Error", {}).get("Code", ""))
    return status == 404 or code in _MISSING_R2_ERROR_CODES


def _new_owned_object_key(publication_id: str, file_suffix: str) -> str:
    normalized = validate_publication_id(publication_id)
    if file_suffix not in {".jpg", ".png", ".webp"}:
        raise ValueError("Unsupported temporary media suffix")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    object_key = (
        f"{publication_media_prefix(normalized)}"
        f"{timestamp}_{uuid.uuid4().hex}{file_suffix}"
    )
    return validate_owned_object_key(object_key, normalized)


def _new_owned_reel_object_key(publication_id: str) -> str:
    normalized = validate_publication_id(publication_id)
    object_key = (
        f"{reel_publication_media_prefix(normalized)}"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_"
        f"{uuid.uuid4().hex}.mp4"
    )
    return validate_owned_reel_object_key(object_key, normalized)


def _delete_owned_object(
    client,
    bucket_name: str,
    *,
    object_key: str,
    publication_id: str,
    reason: str,
    key_validator=validate_owned_object_key,
) -> bool:
    key_validator(object_key, publication_id)
    for attempt in range(1, MEDIA_OPERATION_ATTEMPTS + 1):
        try:
            client.delete_object(Bucket=bucket_name, Key=object_key)
            logger.info(
                "r2_temp_media_cleanup publication_id=%s object_count=1 "
                "result=deleted reason=%s attempt=%s",
                publication_id,
                reason,
                attempt,
            )
            return True
        except Exception as error:
            if _is_missing_r2_object(error):
                logger.info(
                    "r2_temp_media_cleanup publication_id=%s object_count=1 "
                    "result=already_absent reason=%s attempt=%s",
                    publication_id,
                    reason,
                    attempt,
                )
                return True
            retryable = _is_transient_r2_error(error)
            if retryable and attempt < MEDIA_OPERATION_ATTEMPTS:
                logger.warning(
                    "r2_temp_media_cleanup_failed publication_id=%s reason=%s "
                    "attempt=%s retryable=true error=%s",
                    publication_id,
                    reason,
                    attempt,
                    type(error).__name__,
                )
                time.sleep(2 ** (attempt - 1))
                continue
            logger.error(
                "r2_temp_media_cleanup_failed publication_id=%s reason=%s "
                "attempt=%s retryable=%s error=%s",
                publication_id,
                reason,
                attempt,
                retryable,
                type(error).__name__,
            )
            return False
    raise AssertionError("unreachable")


def cleanup_temp_media_upload(
    upload: TempMediaUpload, *, reason: str
) -> bool:
    """Delete one exact owned upload; invalid ownership never reaches boto3."""
    if not isinstance(upload, TempMediaUpload):
        raise ValueError("Exact cleanup accepts only an owned upload handle")
    validate_owned_object_key(upload.object_key, upload.publication_id)
    try:
        configuration = _load_configuration(require_public_url=False)
        client = _get_s3_client(configuration)
    except Exception as error:
        logger.error(
            "r2_temp_media_cleanup_failed publication_id=%s reason=%s "
            "attempt=0 retryable=false error=%s",
            upload.publication_id,
            reason,
            type(error).__name__,
        )
        return False
    return _delete_owned_object(
        client,
        configuration.bucket_name,
        object_key=upload.object_key,
        publication_id=upload.publication_id,
        reason=reason,
    )


def cleanup_temp_reel_upload(upload: TempReelUpload, *, reason: str) -> bool:
    """Delete one exact owned Reel upload; image handles are rejected."""
    if not isinstance(upload, TempReelUpload):
        raise ValueError("Reel cleanup accepts only an owned Reel upload handle")
    validate_owned_reel_object_key(upload.object_key, upload.publication_id)
    try:
        configuration = _load_configuration(require_public_url=False)
        client = _get_s3_client(configuration)
    except Exception as error:
        logger.error(
            "r2_temp_reel_cleanup_failed publication_id=%s reason=%s "
            "attempt=0 retryable=false error=%s",
            upload.publication_id,
            reason,
            type(error).__name__,
        )
        return False
    return _delete_owned_object(
        client,
        configuration.bucket_name,
        object_key=upload.object_key,
        publication_id=upload.publication_id,
        reason=reason,
        key_validator=validate_owned_reel_object_key,
    )


def rollback_temp_media_uploads(
    publication_id: str,
    uploads: Sequence[TempMediaUpload],
    *,
    reason: str = "pre_meta_staging_rollback",
) -> MediaCleanupSummary:
    """Best-effort exact rollback for handles that provably never reached Meta."""
    normalized = validate_publication_id(publication_id)
    owned_uploads = tuple(uploads)
    for upload in owned_uploads:
        if not isinstance(upload, TempMediaUpload):
            raise ValueError("Temporary media rollback accepts only owned upload handles")
        if upload.publication_id != normalized:
            raise ValueError("Refusing cross-publication temporary media rollback")
        validate_owned_object_key(upload.object_key, normalized)

    deleted = 0
    failures = 0
    for upload in reversed(owned_uploads):
        if cleanup_temp_media_upload(upload, reason=reason):
            deleted += 1
        else:
            failures += 1
    return MediaCleanupSummary(
        publication_id=normalized,
        discovered=len(owned_uploads),
        deleted=deleted,
        failures=failures,
        complete=failures == 0,
        reason=reason,
    )


def _list_owned_publication_objects(
    client,
    bucket_name: str,
    publication_id: str,
    *,
    prefix_builder=publication_media_prefix,
    key_validator=validate_owned_object_key,
) -> tuple[str, ...] | None:
    prefix = prefix_builder(publication_id)
    object_keys: list[str] = []
    continuation_token: str | None = None
    page_count = 0

    while True:
        if page_count >= PUBLICATION_CLEANUP_MAX_PAGES:
            logger.warning(
                "r2_temp_media_cleanup_failed publication_id=%s "
                "reason=pagination_page_bound_exceeded",
                publication_id,
            )
            return None
        remaining = PUBLICATION_CLEANUP_MAX_OBJECTS - len(object_keys)
        if remaining < 0:
            return None
        request = {
            "Bucket": bucket_name,
            "Prefix": prefix,
            "MaxKeys": min(PUBLICATION_LIST_PAGE_SIZE, remaining + 1),
        }
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token

        response = None
        for attempt in range(1, MEDIA_OPERATION_ATTEMPTS + 1):
            try:
                response = client.list_objects_v2(**request)
                break
            except Exception as error:
                retryable = _is_transient_r2_error(error)
                if retryable and attempt < MEDIA_OPERATION_ATTEMPTS:
                    logger.warning(
                        "r2_temp_media_cleanup_failed publication_id=%s "
                        "reason=list attempt=%s retryable=true error=%s",
                        publication_id,
                        attempt,
                        type(error).__name__,
                    )
                    time.sleep(2 ** (attempt - 1))
                    continue
                logger.error(
                    "r2_temp_media_cleanup_failed publication_id=%s reason=list "
                    "attempt=%s retryable=%s error=%s",
                    publication_id,
                    attempt,
                    retryable,
                    type(error).__name__,
                )
                return None

        if not isinstance(response, dict):
            logger.error(
                "r2_temp_media_cleanup_failed publication_id=%s "
                "reason=malformed_list_response",
                publication_id,
            )
            return None
        page_count += 1
        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            logger.error(
                "r2_temp_media_cleanup_failed publication_id=%s "
                "reason=malformed_list_contents",
                publication_id,
            )
            return None
        page_keys: list[str] = []
        for item in contents:
            key = item.get("Key") if isinstance(item, dict) else None
            try:
                page_keys.append(key_validator(key, publication_id))
            except ValueError:
                logger.warning(
                    "r2_temp_media_cleanup_failed publication_id=%s "
                    "reason=unexpected_key_under_owned_prefix",
                    publication_id,
                )
                return None
        if len(object_keys) + len(page_keys) > PUBLICATION_CLEANUP_MAX_OBJECTS:
            logger.warning(
                "r2_temp_media_cleanup_failed publication_id=%s "
                "reason=object_count_bound_exceeded object_count=%s",
                publication_id,
                len(object_keys) + len(page_keys),
            )
            return None
        object_keys.extend(page_keys)
        if len(object_keys) != len(set(object_keys)):
            logger.warning(
                "r2_temp_media_cleanup_failed publication_id=%s "
                "reason=duplicate_listed_key",
                publication_id,
            )
            return None

        truncated = response.get("IsTruncated", False)
        if truncated is not True:
            if truncated is not False:
                logger.warning(
                    "r2_temp_media_cleanup_failed publication_id=%s "
                    "reason=malformed_truncation_flag",
                    publication_id,
                )
                return None
            return tuple(object_keys)
        continuation_token = response.get("NextContinuationToken")
        if (
            not isinstance(continuation_token, str)
            or not continuation_token
            or len(object_keys) >= PUBLICATION_CLEANUP_MAX_OBJECTS
            or page_count >= PUBLICATION_CLEANUP_MAX_PAGES
        ):
            logger.warning(
                "r2_temp_media_cleanup_failed publication_id=%s "
                "reason=pagination_bound_or_token_invalid",
                publication_id,
            )
            return None


def cleanup_publication_media(
    publication_id: str, *, reason: str
) -> MediaCleanupSummary:
    """List and delete only one bounded, exact application-owned prefix."""
    normalized = validate_publication_id(publication_id)
    try:
        configuration = _load_configuration(require_public_url=False)
        client = _get_s3_client(configuration)
    except Exception as error:
        logger.error(
            "r2_publication_cleanup_summary publication_id=%s object_count=0 "
            "result=failed reason=%s error=%s",
            normalized,
            reason,
            type(error).__name__,
        )
        return MediaCleanupSummary(normalized, 0, 0, 1, False, reason)

    object_keys = _list_owned_publication_objects(
        client, configuration.bucket_name, normalized
    )
    if object_keys is None:
        logger.error(
            "r2_publication_cleanup_summary publication_id=%s object_count=0 "
            "result=failed reason=%s",
            normalized,
            reason,
        )
        return MediaCleanupSummary(normalized, 0, 0, 1, False, reason)

    deleted = 0
    failures = 0
    for object_key in object_keys:
        if _delete_owned_object(
            client,
            configuration.bucket_name,
            object_key=object_key,
            publication_id=normalized,
            reason=reason,
        ):
            deleted += 1
        else:
            failures += 1
    complete = failures == 0
    logger.info(
        "r2_publication_cleanup_summary publication_id=%s object_count=%s "
        "deleted=%s failures=%s result=%s reason=%s",
        normalized,
        len(object_keys),
        deleted,
        failures,
        "complete" if complete else "incomplete",
        reason,
    )
    return MediaCleanupSummary(
        normalized,
        len(object_keys),
        deleted,
        failures,
        complete,
        reason,
    )


def cleanup_publication_reels(
    publication_id: str, *, reason: str
) -> MediaCleanupSummary:
    """List and delete only one bounded, exact Reel-owned prefix."""
    normalized = validate_publication_id(publication_id)
    try:
        configuration = _load_configuration(require_public_url=False)
        client = _get_s3_client(configuration)
    except Exception as error:
        logger.error(
            "r2_publication_reel_cleanup_summary publication_id=%s object_count=0 "
            "result=failed reason=%s error=%s",
            normalized,
            reason,
            type(error).__name__,
        )
        return MediaCleanupSummary(normalized, 0, 0, 1, False, reason)

    object_keys = _list_owned_publication_objects(
        client,
        configuration.bucket_name,
        normalized,
        prefix_builder=reel_publication_media_prefix,
        key_validator=validate_owned_reel_object_key,
    )
    if object_keys is None:
        logger.error(
            "r2_publication_reel_cleanup_summary publication_id=%s object_count=0 "
            "result=failed reason=%s",
            normalized,
            reason,
        )
        return MediaCleanupSummary(normalized, 0, 0, 1, False, reason)

    deleted = 0
    failures = 0
    for object_key in object_keys:
        if _delete_owned_object(
            client,
            configuration.bucket_name,
            object_key=object_key,
            publication_id=normalized,
            reason=reason,
            key_validator=validate_owned_reel_object_key,
        ):
            deleted += 1
        else:
            failures += 1
    complete = failures == 0
    logger.info(
        "r2_publication_reel_cleanup_summary publication_id=%s object_count=%s "
        "deleted=%s failures=%s result=%s reason=%s",
        normalized,
        len(object_keys),
        deleted,
        failures,
        "complete" if complete else "incomplete",
        reason,
    )
    return MediaCleanupSummary(
        normalized,
        len(object_keys),
        deleted,
        failures,
        complete,
        reason,
    )


def stage_temp_media(
    file_path: str,
    *,
    publication_id: str,
    content_type: str,
    file_suffix: str,
) -> TempMediaUpload:
    """Upload and publicly validate one publication-owned staging object."""
    normalized = validate_publication_id(publication_id)
    configuration = _load_configuration(require_public_url=True)
    client = _get_s3_client(configuration)
    object_key = _new_owned_object_key(normalized, file_suffix)

    upload_success = False
    for attempt in range(1, MEDIA_OPERATION_ATTEMPTS + 1):
        try:
            client.upload_file(
                file_path,
                configuration.bucket_name,
                object_key,
                ExtraArgs={"ContentType": content_type},
            )
            upload_success = True
            logger.info(
                "r2_temp_media_uploaded publication_id=%s object_count=1 "
                "result=uploaded attempt=%s",
                normalized,
                attempt,
            )
            break
        except Exception as error:
            retryable = _is_transient_r2_error(error)
            logger.warning(
                "R2 upload failed attempt=%s/%s error=%s retryable=%s",
                attempt,
                MEDIA_OPERATION_ATTEMPTS,
                type(error).__name__,
                retryable,
            )
            if retryable and attempt < MEDIA_OPERATION_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            break
    if not upload_success:
        raise RuntimeError("Failed to upload media to Cloudflare R2 after 3 attempts.")

    if configuration.public_url_base is None:
        raise RuntimeError("R2 public URL configuration unexpectedly missing")
    public_url = f"{configuration.public_url_base}/{object_key}"
    upload = TempMediaUpload(object_key, public_url, normalized)
    for _attempt in range(1, PUBLIC_HEALTH_CHECK_ATTEMPTS + 1):
        try:
            response = requests.head(
                public_url, allow_redirects=True, timeout=10
            )
            if response.status_code == 200:
                response_content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .casefold()
                )
                try:
                    response_content_length = int(
                        response.headers.get("Content-Length", 0)
                    )
                except (TypeError, ValueError):
                    response_content_length = 0
                if (
                    response_content_length > 0
                    and response_content_type == content_type
                ):
                    return upload
            logger.warning(
                "R2 public health check failed publication_id=%s attempt=%s",
                normalized,
                _attempt,
            )
        except Exception as error:
            logger.warning(
                "R2 public health check error publication_id=%s attempt=%s error=%s",
                normalized,
                _attempt,
                type(error).__name__,
            )
        if _attempt < PUBLIC_HEALTH_CHECK_ATTEMPTS:
            time.sleep(2)

    _delete_owned_object(
        client,
        configuration.bucket_name,
        object_key=object_key,
        publication_id=normalized,
        reason="public_health_check_failed",
    )
    raise RuntimeError(
        f"R2 object {object_key} uploaded successfully, but its public health check failed."
    )


def stage_reel_mp4(file_path: str, publication_id: str) -> TempReelUpload:
    """Upload and publicly validate an MP4 in the dedicated Reel namespace."""
    if not isinstance(file_path, str) or not file_path.endswith(".mp4"):
        raise ValueError("Reel staging requires an .mp4 MP4 source file")
    normalized = validate_publication_id(publication_id)
    configuration = _load_configuration(require_public_url=True)
    client = _get_s3_client(configuration)
    object_key = _new_owned_reel_object_key(normalized)

    upload_success = False
    for attempt in range(1, MEDIA_OPERATION_ATTEMPTS + 1):
        try:
            client.upload_file(
                file_path,
                configuration.bucket_name,
                object_key,
                ExtraArgs={"ContentType": "video/mp4"},
            )
            upload_success = True
            logger.info(
                "r2_temp_reel_uploaded publication_id=%s object_count=1 "
                "result=uploaded attempt=%s",
                normalized,
                attempt,
            )
            break
        except Exception as error:
            retryable = _is_transient_r2_error(error)
            logger.warning(
                "R2 Reel upload failed attempt=%s/%s error=%s retryable=%s",
                attempt,
                MEDIA_OPERATION_ATTEMPTS,
                type(error).__name__,
                retryable,
            )
            if retryable and attempt < MEDIA_OPERATION_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            break
    if not upload_success:
        raise RuntimeError("Failed to upload Reel MP4 to Cloudflare R2 after 3 attempts.")

    if configuration.public_url_base is None:
        raise RuntimeError("R2 public URL configuration unexpectedly missing")
    public_url = f"{configuration.public_url_base}/{object_key}"
    upload = TempReelUpload(object_key, public_url, normalized)
    for attempt in range(1, PUBLIC_HEALTH_CHECK_ATTEMPTS + 1):
        try:
            response = requests.head(public_url, allow_redirects=True, timeout=10)
            response_content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )
            try:
                response_content_length = int(response.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                response_content_length = 0
            if (
                response.status_code == 200
                and response_content_length > 0
                and response_content_type == "video/mp4"
            ):
                return upload
            logger.warning(
                "R2 Reel public health check failed publication_id=%s attempt=%s",
                normalized,
                attempt,
            )
        except Exception as error:
            logger.warning(
                "R2 Reel public health check error publication_id=%s attempt=%s "
                "error=%s",
                normalized,
                attempt,
                type(error).__name__,
            )
        if attempt < PUBLIC_HEALTH_CHECK_ATTEMPTS:
            time.sleep(2)

    _delete_owned_object(
        client,
        configuration.bucket_name,
        object_key=object_key,
        publication_id=normalized,
        reason="public_health_check_failed",
        key_validator=validate_owned_reel_object_key,
    )
    raise RuntimeError(
        f"R2 Reel object {object_key} uploaded successfully, but its public health check failed."
    )
