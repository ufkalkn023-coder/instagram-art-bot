import plistlib
from pathlib import Path

from scripts.collect_insights import ROOT, _default_reels_root
from scripts.install_insights_launchd import DEFAULT_REELS_ROOT, launchd_payload, render_plist
from src.local_credentials import REQUIRED_CREDENTIALS, format_credential_status, load_keychain_credentials


def test_launchd_plist_is_hourly_absolute_and_contains_no_secret_values(tmp_path):
    secrets = {name: f"super-secret-{index}" for index, name in enumerate(REQUIRED_CREDENTIALS)}
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
        for variable in REQUIRED_CREDENTIALS
    }
    status = load_keychain_credentials(environment, reader=secret_values.get)
    output = format_credential_status(status)

    assert set(output.splitlines()) == {
        f"[insights] {variable}={'SET' if status[variable] else 'MISSING'}"
        for variable in REQUIRED_CREDENTIALS
    }
    assert "account-id" not in output
    assert "secret-INSTAGRAM_ACCESS_TOKEN" not in output


def test_instagram_insights_client_source_is_get_only():
    source = (ROOT / "src" / "instagram_insights.py").read_text(encoding="utf-8")
    for mutation in (".post(", ".put(", ".patch(", ".delete("):
        assert mutation not in source
