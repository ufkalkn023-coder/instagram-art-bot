import json

from scripts.audit_engagement_learning import run


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
    assert "selected_snapshot_slots=24h=0,72h=1,168h=0" in output
    assert "verbose_weight_sum=" in output
    assert "sensitive-publication-id" not in output
    assert history_path.read_bytes() == history_before
    assert snapshots_path.read_bytes() == snapshots_before
