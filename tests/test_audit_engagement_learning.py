import json
from pathlib import Path

import scripts.audit_engagement_learning as audit_script
from scripts.audit_engagement_learning import run
from src.local_credentials import (
    ENGAGEMENT_AUDIT_CREDENTIALS,
    ENGAGEMENT_AUDIT_PROFILE,
)


def test_audit_command_uses_local_inputs_read_only_and_anonymizes_verbose_ids(
    tmp_path,
    capsys,
):
    history_path = tmp_path / "history.json"
    snapshots_path = tmp_path / "snapshots.json"
    history_path.write_text(json.dumps({
        "publications": [{
            "id": "sensitive-publication-id",
            "type": "carousel",
            "media_id": "media-1",
            "artwork_ids": ["cover", "work"],
            "posted_at": "2026-08-20T12:00:00Z",
        }],
        "posted_artworks": [],
    }))
    snapshots_path.write_text(json.dumps([{
        "publication_id": "sensitive-publication-id",
        "media_id": "media-1",
        "target_age_hours": 72,
        "captured_at": "2026-08-23T12:00:00Z",
        "metrics": {"reach": 10, "likes": 0},
    }]))
    history_before = history_path.read_bytes()
    snapshots_before = snapshots_path.read_bytes()

    result = run([
        "--history",
        str(history_path),
        "--snapshots",
        str(snapshots_path),
        "--now",
        "2026-08-24T12:00:00Z",
        "--verbose",
    ])

    output = capsys.readouterr().out
    assert result == 0
    assert "eligible_learning_observations=1" in output
    assert "usable_publications=1" in output
    assert "current_learned_influence=" in output
    assert "selected_snapshot_slots=24h=0,72h=1,168h=0" in output
    assert "verbose_weight_sum=" in output
    assert "sensitive-publication-id" not in output
    assert history_path.read_bytes() == history_before
    assert snapshots_path.read_bytes() == snapshots_before


def test_audit_with_local_inputs_does_not_load_any_keychain_profile(
    tmp_path,
    monkeypatch,
):
    history_path = tmp_path / "history.json"
    snapshots_path = tmp_path / "snapshots.json"
    history_path.write_text('{"posted_artworks": []}', encoding="utf-8")
    snapshots_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        audit_script,
        "load_keychain_credentials",
        lambda profile: (_ for _ in ()).throw(AssertionError("unexpected Keychain read")),
    )

    assert run(["--history", str(history_path), "--snapshots", str(snapshots_path)]) == 0


def test_production_audit_explicitly_loads_only_engagement_audit_profile(
    monkeypatch,
):
    requested_profiles = []

    def fake_load(profile):
        requested_profiles.append(profile)
        return {variable: True for variable in ENGAGEMENT_AUDIT_CREDENTIALS}

    monkeypatch.setattr(audit_script, "load_keychain_credentials", fake_load)
    monkeypatch.setattr(
        audit_script,
        "_load_inputs",
        lambda history_path, snapshots_path: ({"posted_artworks": []}, [], 0),
    )

    assert run([]) == 0
    assert requested_profiles == [ENGAGEMENT_AUDIT_PROFILE]


def test_missing_audit_profile_fails_before_r2_access(monkeypatch, capsys):
    monkeypatch.setattr(
        audit_script,
        "load_keychain_credentials",
        lambda profile: {
            variable: variable != "CLOUDFLARE_R2_SECRET_ACCESS_KEY"
            for variable in ENGAGEMENT_AUDIT_CREDENTIALS
        },
    )
    monkeypatch.setattr(
        audit_script,
        "_load_inputs",
        lambda history_path, snapshots_path: (_ for _ in ()).throw(
            AssertionError("R2 access was attempted")
        ),
    )

    try:
        run([])
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("audit did not fail on missing profile credentials")

    output = capsys.readouterr()
    assert "Missing engagement-audit credentials" in output.err
    assert "CLOUDFLARE_R2_SECRET_ACCESS_KEY" in output.err


def test_audit_production_path_exposes_only_read_operations():
    source = Path(audit_script.__file__).read_text(encoding="utf-8")

    assert "load_history_with_etag()" in source
    assert "load_all_snapshots()" in source
    for mutation in (
        "put_object",
        "delete_object",
        "copy_object",
        "upload_file",
        "upload_fileobj",
        "_upload_history",
        "save_snapshots",
        "save_associations",
    ):
        assert mutation not in source
