"""Profile-isolated Keychain credentials for local Artfolio operations."""

from __future__ import annotations

import getpass
import os
import subprocess
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Callable, Literal

CredentialProfileName = Literal["collector", "engagement-audit"]

COLLECTOR_PROFILE: CredentialProfileName = "collector"
ENGAGEMENT_AUDIT_PROFILE: CredentialProfileName = "engagement-audit"

COLLECTOR_CREDENTIALS = (
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
)
ENGAGEMENT_AUDIT_CREDENTIALS = (
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
)


@dataclass(frozen=True)
class CredentialProfile:
    service_prefix: str
    variables: tuple[str, ...]


_PROFILES: dict[CredentialProfileName, CredentialProfile] = {
    COLLECTOR_PROFILE: CredentialProfile(
        service_prefix="com.artfolio.instagram-insights",
        variables=COLLECTOR_CREDENTIALS,
    ),
    ENGAGEMENT_AUDIT_PROFILE: CredentialProfile(
        service_prefix="com.artfolio.engagement-audit",
        variables=ENGAGEMENT_AUDIT_CREDENTIALS,
    ),
}


def credential_profile(profile: CredentialProfileName) -> CredentialProfile:
    try:
        return _PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"Unsupported local credential profile: {profile}") from exc


def credential_variables(profile: CredentialProfileName) -> tuple[str, ...]:
    return credential_profile(profile).variables


def keychain_service(profile: CredentialProfileName, variable: str) -> str:
    selected = credential_profile(profile)
    if variable not in selected.variables:
        raise ValueError(f"Unsupported {profile} credential: {variable}")
    return f"{selected.service_prefix}.{variable}"


def read_keychain_credential(
    variable: str,
    *,
    profile: CredentialProfileName,
    account: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Read one selected-profile secret without logging or passing it as an argument."""
    try:
        result = runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account or getpass.getuser(),
                "-s",
                keychain_service(profile, variable),
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


def keychain_credential_available(
    variable: str,
    *,
    profile: CredentialProfileName,
    account: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Check that a selected-profile value is readable and nonblank without exposing it."""
    return bool(
        read_keychain_credential(
            variable,
            profile=profile,
            account=account,
            runner=runner,
        )
    )


def load_keychain_credentials(
    profile: CredentialProfileName,
    environment: MutableMapping[str, str] | None = None,
    *,
    reader: Callable[..., str | None] = read_keychain_credential,
) -> dict[str, bool]:
    """Fill missing environment values from one profile and return presence state."""
    selected = credential_profile(profile)
    target = os.environ if environment is None else environment
    status: dict[str, bool] = {}
    for variable in selected.variables:
        existing = target.get(variable, "").strip()
        if not existing:
            stored = reader(variable, profile=profile)
            if stored:
                target[variable] = stored
                existing = stored
        status[variable] = bool(existing)
    return status


def active_r2_credential_matches_keychain_profile(
    profile: CredentialProfileName,
    environment: MutableMapping[str, str] | None = None,
    *,
    reader: Callable[..., str | None] = read_keychain_credential,
) -> bool:
    """Return whether the active R2 key pair duplicates another local profile.

    This is a role-separation guard, not a fallback: values from ``profile`` are
    compared in memory and are never copied into the active environment.
    """
    target = os.environ if environment is None else environment
    names = (
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    )
    active = tuple(target.get(name, "").strip() for name in names)
    stored = tuple((reader(name, profile=profile) or "").strip() for name in names)
    return all(active) and all(stored) and active == stored


def active_collector_r2_credential_matches_audit_profile(
    environment: MutableMapping[str, str] | None = None,
    *,
    reader: Callable[..., str | None] = read_keychain_credential,
) -> bool:
    """Detect unsafe local role reuse without exposing the audit profile to callers."""
    return active_r2_credential_matches_keychain_profile(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=reader,
    )


def format_credential_status(
    profile: CredentialProfileName,
    status: dict[str, bool],
) -> str:
    selected = credential_profile(profile)
    return "\n".join(
        f"[{profile}] {variable}={'AVAILABLE' if status.get(variable) else 'MISSING'}"
        for variable in selected.variables
    )
