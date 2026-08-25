"""Keychain-backed credentials for the hourly local Insights collector."""

import getpass
import os
import subprocess
from collections.abc import MutableMapping
from typing import Callable

KEYCHAIN_SERVICE_PREFIX = "com.artfolio.instagram-insights"
REQUIRED_CREDENTIALS = (
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
)


def keychain_service(variable: str) -> str:
    if variable not in REQUIRED_CREDENTIALS:
        raise ValueError(f"Unsupported Insights credential: {variable}")
    return f"{KEYCHAIN_SERVICE_PREFIX}.{variable}"


def read_keychain_credential(
    variable: str,
    *,
    account: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Read one secret without writing it to logs or command arguments."""
    try:
        result = runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account or getpass.getuser(),
                "-s",
                keychain_service(variable),
                "-w",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def load_keychain_credentials(
    environment: MutableMapping[str, str] | None = None,
    *,
    reader: Callable[[str], str | None] = read_keychain_credential,
) -> dict[str, bool]:
    """Fill missing environment values from Keychain and return SET/MISSING state."""
    target = os.environ if environment is None else environment
    status: dict[str, bool] = {}
    for variable in REQUIRED_CREDENTIALS:
        existing = target.get(variable, "").strip()
        if not existing:
            stored = reader(variable)
            if stored:
                target[variable] = stored
                existing = stored
        status[variable] = bool(existing)
    return status


def format_credential_status(status: dict[str, bool]) -> str:
    return "\n".join(
        f"[insights] {variable}={'SET' if status.get(variable) else 'MISSING'}"
        for variable in REQUIRED_CREDENTIALS
    )
