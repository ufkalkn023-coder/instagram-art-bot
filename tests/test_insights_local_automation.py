import plistlib
import sys
from pathlib import Path

import pytest

import scripts.collect_insights as collect_insights
import scripts.install_insights_launchd as launchd_installer
from scripts.collect_insights import ROOT, _default_reels_root
from scripts.install_insights_launchd import DEFAULT_REELS_ROOT, launchd_payload, render_plist
from src.local_credentials import (
    COLLECTOR_CREDENTIALS,
    COLLECTOR_PROFILE,
    ENGAGEMENT_AUDIT_CREDENTIALS,
    ENGAGEMENT_AUDIT_PROFILE,
    format_credential_status,
    load_keychain_credentials,
)


def test_launchd_plist_is_hourly_absolute_and_contains_no_secret_values(tmp_path):
    secrets = {
        name: f"super-secret-{index}"
        for index, name in enumerate(COLLECTOR_CREDENTIALS)
    }
    payload = launchd_payload(
        repo_root=tmp_path / "art-bot",
        reels_root=tmp_path / "artfolio-reels",
        python_executable=Path("/usr/bin/python3"),
        log_path=tmp_path / "logs" / "insights.log",
    )
    encoded = render_plist(
        repo_root=tmp_path / "art-bot",
        reels_root=tmp_path / "artfolio-reels",
        python_executable=Path("/usr/bin/python3"),
        log_path=tmp_path / "logs" / "insights.log",
    )
    decoded = plistlib.loads(encoded)

    assert decoded == payload
    assert payload["StartInterval"] == 3600
    assert payload["RunAtLoad"] is True
    assert Path(payload["WorkingDirectory"]).is_absolute()
    assert all(Path(value).is_absolute() for value in payload["ProgramArguments"] if "/" in value)
    assert payload["StandardOutPath"] == payload["StandardErrorPath"] == "/dev/null"
    text = encoded.decode("utf-8")
    for variable, secret in secrets.items():
        assert variable not in text
        assert secret not in text


def test_authoritative_default_repository_paths_are_absolute():
    assert ROOT == Path(__file__).resolve().parents[1]
    assert DEFAULT_REELS_ROOT == ROOT.parent / "Remotion İnstagram Reels" / "artfolio-reels"
    assert _default_reels_root() == DEFAULT_REELS_ROOT
    assert ROOT.is_absolute() and DEFAULT_REELS_ROOT.is_absolute()


def test_secret_diagnostic_reports_only_set_or_missing():
    environment = {"INSTAGRAM_ACCOUNT_ID": "account-id"}
    secret_values = {
        variable: (f"secret-{variable}" if variable == "INSTAGRAM_ACCESS_TOKEN" else None)
        for variable in COLLECTOR_CREDENTIALS
    }
    status = load_keychain_credentials(
        COLLECTOR_PROFILE,
        environment,
        reader=lambda variable, *, profile: secret_values.get(variable),
    )
    output = format_credential_status(COLLECTOR_PROFILE, status)

    assert set(output.splitlines()) == {
        f"[collector] {variable}={'AVAILABLE' if status[variable] else 'MISSING'}"
        for variable in COLLECTOR_CREDENTIALS
    }
    assert "account-id" not in output
    assert "secret-INSTAGRAM_ACCESS_TOKEN" not in output


def test_instagram_insights_client_source_is_get_only():
    source = (ROOT / "src" / "instagram_insights.py").read_text(encoding="utf-8")
    for mutation in (".post(", ".put(", ".patch(", ".delete("):
        assert mutation not in source


def test_collect_insights_explicitly_requests_collector_profile(monkeypatch, capsys):
    requested_profiles = []

    def fake_load(profile):
        requested_profiles.append(profile)
        return {variable: False for variable in COLLECTOR_CREDENTIALS}

    monkeypatch.setattr(collect_insights, "load_keychain_credentials", fake_load)
    monkeypatch.setattr(sys, "argv", ["collect_insights.py", "--check-secrets"])

    assert collect_insights.main() == 0
    assert requested_profiles == [COLLECTOR_PROFILE]
    assert "engagement-audit" not in capsys.readouterr().out


def test_launchagent_collector_path_cannot_consume_audit_profile():
    payload = launchd_payload()
    arguments = payload["ProgramArguments"]
    collector_source = Path(arguments[1]).read_text(encoding="utf-8")

    assert arguments[1].endswith("scripts/collect_insights.py")
    assert "load_keychain_credentials(COLLECTOR_PROFILE)" in collector_source
    assert "ENGAGEMENT_AUDIT_PROFILE" not in collector_source


@pytest.mark.parametrize(
    ("profile", "variables", "service_prefix"),
    (
        (COLLECTOR_PROFILE, COLLECTOR_CREDENTIALS, "com.artfolio.instagram-insights."),
        (
            ENGAGEMENT_AUDIT_PROFILE,
            ENGAGEMENT_AUDIT_CREDENTIALS,
            "com.artfolio.engagement-audit.",
        ),
    ),
)
def test_keychain_configuration_uses_only_selected_profile_services(
    monkeypatch,
    profile,
    variables,
    service_prefix,
):
    invocations = []
    monkeypatch.setattr(launchd_installer.sys, "platform", "darwin")
    monkeypatch.setattr(
        launchd_installer,
        "keychain_credential_available",
        lambda variable, *, profile: False,
    )
    monkeypatch.setattr(
        launchd_installer.subprocess,
        "run",
        lambda arguments, **kwargs: invocations.append(arguments)
        or type("Result", (), {"returncode": 0})(),
    )

    launchd_installer.configure_keychain(profile)

    other_prefix = (
        "com.artfolio.engagement-audit."
        if profile == COLLECTOR_PROFILE
        else "com.artfolio.instagram-insights."
    )
    assert len(invocations) == len(variables)
    assert all(arguments[-1] == "-w" for arguments in invocations)
    assert all(service_prefix in arguments[-2] for arguments in invocations)
    assert all(other_prefix not in " ".join(arguments) for arguments in invocations)
