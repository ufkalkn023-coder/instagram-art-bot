"""Fail-closed helpers for the isolated live R2 verification suite."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import time
from collections.abc import Mapping
from typing import Any
import uuid

from botocore.exceptions import ClientError


R2_INTEGRATION_OPT_IN = "ARTFOLIO_RUN_R2_INTEGRATION"
R2_INTEGRATION_ROOT = "artfolio-integration-tests"
REQUIRED_R2_ENVIRONMENT = (
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
)
_RUN_PREFIX_PATTERN = re.compile(
    rf"\A{re.escape(R2_INTEGRATION_ROOT)}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/\Z"
)


def integration_enabled(environment: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if environment is None else environment
    return environment.get(R2_INTEGRATION_OPT_IN) == "1"


def missing_r2_configuration(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    environment = os.environ if environment is None else environment
    return tuple(
        name
        for name in REQUIRED_R2_ENVIRONMENT
        if not environment.get(name, "").strip()
    )


def make_run_prefix(run_id: uuid.UUID | None = None) -> str:
    return f"{R2_INTEGRATION_ROOT}/{run_id or uuid.uuid4()}/"


def assert_integration_test_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or _RUN_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ValueError("Refusing R2 operation outside an exact integration run prefix")


def assert_integration_test_key(key: str, run_prefix: str) -> None:
    assert_integration_test_prefix(run_prefix)
    if not isinstance(key, str) or key == run_prefix or not key.startswith(run_prefix):
        raise ValueError("Refusing R2 operation outside the integration run prefix")
    relative_key = key[len(run_prefix) :]
    if not relative_key or relative_key.startswith("/"):
        raise ValueError("Refusing malformed R2 integration key")


def is_missing_object_error(error: ClientError) -> bool:
    response = error.response
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404


@dataclass
class R2IntegrationContext:
    client: Any
    bucket: str
    prefix: str
    created_keys: set[str] = field(default_factory=set)
    _last_write_by_key: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert_integration_test_prefix(self.prefix)

    def key(self, relative_key: str) -> str:
        if not isinstance(relative_key, str) or not relative_key:
            raise ValueError("Integration object key suffix must be non-empty")
        key = f"{self.prefix}{relative_key.lstrip('/')}"
        assert_integration_test_key(key, self.prefix)
        self.created_keys.add(key)
        return key

    def wait_for_write_slot(self, key: str, minimum_interval: float = 1.1) -> None:
        """Avoid obscuring CAS results with R2's documented same-key rate limit."""
        assert_integration_test_key(key, self.prefix)
        elapsed = time.monotonic() - self._last_write_by_key.get(key, 0.0)
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)
        self._last_write_by_key[key] = time.monotonic()

    def list_run_keys(self) -> list[str]:
        assert_integration_test_prefix(self.prefix)
        keys: list[str] = []
        continuation_token: str | None = None
        while True:
            parameters: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": self.prefix,
            }
            if continuation_token is not None:
                parameters["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**parameters)
            for item in response.get("Contents", []):
                key = item.get("Key")
                assert_integration_test_key(key, self.prefix)
                keys.append(key)
            if not response.get("IsTruncated"):
                return keys
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise RuntimeError("R2 returned a truncated listing without a token")

    def cleanup(self) -> int:
        """Delete only exact keys discovered beneath this run's UUID namespace."""
        listed_keys = self.list_run_keys()
        cleanup_keys = sorted(self.created_keys.union(listed_keys))
        for key in cleanup_keys:
            assert_integration_test_key(key, self.prefix)
            self.client.delete_object(Bucket=self.bucket, Key=key)

        remaining = self.list_run_keys()
        if remaining:
            raise RuntimeError(
                "R2 integration cleanup left objects: " + ", ".join(remaining)
            )

        for key in cleanup_keys:
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as error:
                if is_missing_object_error(error):
                    continue
                raise
            raise RuntimeError(f"R2 integration cleanup did not remove key: {key}")
        return len(remaining)
