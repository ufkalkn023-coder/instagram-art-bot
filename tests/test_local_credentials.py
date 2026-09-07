import subprocess

import pytest

from src.local_credentials import (
    COLLECTOR_CREDENTIALS,
    COLLECTOR_PROFILE,
    ENGAGEMENT_AUDIT_CREDENTIALS,
    ENGAGEMENT_AUDIT_PROFILE,
    active_r2_credential_matches_keychain_profile,
    credential_variables,
    format_credential_status,
    keychain_credential_available,
    keychain_service,
    load_keychain_credentials,
    read_keychain_credential,
)


R2_CREDENTIALS = (
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
)


def test_collector_profile_service_name_mapping():
    assert keychain_service(COLLECTOR_PROFILE, "INSTAGRAM_ACCOUNT_ID") == (
        "com.artfolio.instagram-insights.INSTAGRAM_ACCOUNT_ID"
    )
    assert keychain_service(COLLECTOR_PROFILE, "CLOUDFLARE_R2_BUCKET_NAME") == (
        "com.artfolio.instagram-insights.CLOUDFLARE_R2_BUCKET_NAME"
    )


def test_engagement_audit_profile_service_name_mapping():
    for variable in ENGAGEMENT_AUDIT_CREDENTIALS:
        assert keychain_service(ENGAGEMENT_AUDIT_PROFILE, variable) == (
            f"com.artfolio.engagement-audit.{variable}"
        )


def test_profile_allowlists_are_exact_and_audit_excludes_instagram():
    assert credential_variables(COLLECTOR_PROFILE) == COLLECTOR_CREDENTIALS
    assert COLLECTOR_CREDENTIALS == (
        "INSTAGRAM_ACCOUNT_ID",
        "INSTAGRAM_ACCESS_TOKEN",
        *R2_CREDENTIALS,
    )
    assert credential_variables(ENGAGEMENT_AUDIT_PROFILE) == R2_CREDENTIALS
    assert ENGAGEMENT_AUDIT_CREDENTIALS == R2_CREDENTIALS
    assert not {"INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN"}.intersection(
        ENGAGEMENT_AUDIT_CREDENTIALS
    )


@pytest.mark.parametrize("variable", ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN"))
def test_audit_profile_rejects_instagram_credentials(variable):
    with pytest.raises(ValueError, match="Unsupported engagement-audit credential"):
        keychain_service(ENGAGEMENT_AUDIT_PROFILE, variable)


def test_collector_never_falls_back_to_audit_namespace():
    calls = []

    def reader(variable, *, profile):
        calls.append((profile, variable))
        return "audit-only" if profile == ENGAGEMENT_AUDIT_PROFILE else None

    environment = {}
    status = load_keychain_credentials(COLLECTOR_PROFILE, environment, reader=reader)

    assert not environment
    assert not any(status.values())
    assert {profile for profile, _ in calls} == {COLLECTOR_PROFILE}


def test_audit_never_falls_back_to_collector_namespace():
    calls = []

    def reader(variable, *, profile):
        calls.append((profile, variable))
        return "collector-only" if profile == COLLECTOR_PROFILE else None

    environment = {}
    status = load_keychain_credentials(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=reader,
    )

    assert not environment
    assert not any(status.values())
    assert {profile for profile, _ in calls} == {ENGAGEMENT_AUDIT_PROFILE}
    assert {variable for _, variable in calls} == set(ENGAGEMENT_AUDIT_CREDENTIALS)


def test_existing_environment_takes_precedence_within_selected_profile():
    calls = []

    def reader(variable, *, profile):
        calls.append((profile, variable))
        return f"keychain-{variable}"

    environment = {"CLOUDFLARE_R2_ACCOUNT_ID": "existing-account"}
    status = load_keychain_credentials(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=reader,
    )

    assert environment["CLOUDFLARE_R2_ACCOUNT_ID"] == "existing-account"
    assert all(status.values())
    assert (ENGAGEMENT_AUDIT_PROFILE, "CLOUDFLARE_R2_ACCOUNT_ID") not in calls


def test_missing_credentials_remain_missing_and_are_not_added_to_environment():
    environment = {}
    status = load_keychain_credentials(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=lambda variable, *, profile: None,
    )

    assert environment == {}
    assert status == {variable: False for variable in ENGAGEMENT_AUDIT_CREDENTIALS}


def test_status_output_never_contains_values():
    secrets = {variable: f"secret-{index}" for index, variable in enumerate(R2_CREDENTIALS)}
    environment = {}
    status = load_keychain_credentials(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=lambda variable, *, profile: secrets[variable],
    )

    output = format_credential_status(ENGAGEMENT_AUDIT_PROFILE, status)

    assert set(output.splitlines()) == {
        f"[engagement-audit] {variable}=AVAILABLE" for variable in R2_CREDENTIALS
    }
    assert all(secret not in output for secret in secrets.values())


def test_active_r2_profile_collision_is_detected_without_copying_values():
    environment = {
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "shared-access",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "shared-secret",
    }

    assert active_r2_credential_matches_keychain_profile(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=lambda variable, *, profile: environment[variable],
    )
    assert environment == {
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "shared-access",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "shared-secret",
    }


def test_active_r2_profile_collision_requires_complete_matching_key_pair():
    environment = {
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "collector-access",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "collector-secret",
    }

    assert not active_r2_credential_matches_keychain_profile(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=lambda variable, *, profile: (
            "audit-access" if variable.endswith("ACCESS_KEY_ID") else "audit-secret"
        ),
    )
    assert not active_r2_credential_matches_keychain_profile(
        ENGAGEMENT_AUDIT_PROFILE,
        environment,
        reader=lambda variable, *, profile: None,
    )


def test_keychain_read_uses_only_selected_service_and_not_secret_arguments():
    captured = []
    secret = "never-an-argument"

    def runner(arguments, **kwargs):
        captured.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout=f"{secret}\n")

    assert read_keychain_credential(
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        profile=ENGAGEMENT_AUDIT_PROFILE,
        account="test-user",
        runner=runner,
    ) == secret
    arguments, kwargs = captured[0]
    assert "com.artfolio.engagement-audit.CLOUDFLARE_R2_ACCESS_KEY_ID" in arguments
    assert secret not in arguments
    assert kwargs["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("returncode", "stored_value", "expected"),
    ((0, "usable", True), (0, "  \n", False), (44, "ignored", False)),
)
def test_keychain_availability_requires_a_readable_nonblank_value(
    returncode,
    stored_value,
    expected,
):
    captured = []

    def runner(arguments, **kwargs):
        captured.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, returncode, stdout=stored_value)

    assert (
        keychain_credential_available(
            "CLOUDFLARE_R2_BUCKET_NAME",
            profile=ENGAGEMENT_AUDIT_PROFILE,
            account="test-user",
            runner=runner,
        )
        is expected
    )
    arguments, kwargs = captured[0]
    assert "com.artfolio.engagement-audit.CLOUDFLARE_R2_BUCKET_NAME" in arguments
    assert arguments[-1] == "-w"
    assert stored_value not in arguments
    assert kwargs["stderr"] is subprocess.DEVNULL
