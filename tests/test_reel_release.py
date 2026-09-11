import hashlib
import json
import math
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import reel_release


FILES = {
    "reel.mp4": b"verified mp4 bytes",
    "caption.txt": b"A verified caption.\n",
    "metadata.json": None,
    "qc/contact-sheet.png": b"verified contact sheet bytes",
}
MANIFEST_FILES = {
    "video": "reel.mp4",
    "caption": "caption.txt",
    "metadata": "metadata.json",
    "qcContactSheet": "qc/contact-sheet.png",
}
CREATED_AT = "2026-09-11T11:55:00.000Z"
REEL_ID = "met_123"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_release(tmp_path: Path, *, release_id: str = REEL_ID) -> Path:
    release = tmp_path / "artfolio-reels" / "output" / "releases" / release_id
    (release / "qc").mkdir(parents=True)
    metadata = {
        "canonicalId": REEL_ID,
        "reelId": REEL_ID,
        "title": "The verified artwork",
        "template": "museum-reel-v1",
        "durationSeconds": 12.0,
        "hook": "A verified hook.",
        "generatedAt": CREATED_AT,
    }
    file_data = dict(FILES)
    file_data["metadata.json"] = (json.dumps(metadata, sort_keys=True) + "\n").encode()
    for relative_path, data in file_data.items():
        path = release / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "version": "artfolio-release-v1",
        "reelId": REEL_ID,
        "createdAt": CREATED_AT,
        "files": MANIFEST_FILES,
        "sha256": {
            relative_path: _sha256(data)
            for relative_path, data in file_data.items()
        },
    }
    (release / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return release


def verifier_output(release: Path) -> dict[str, object]:
    return {
        "valid": True,
        "errors": [],
        "directory": str(release.resolve()),
        "reelId": REEL_ID,
        "media": {
            "path": str((release / "reel.mp4").resolve()),
            "sizeBytes": (release / "reel.mp4").stat().st_size,
            "durationSeconds": 12.0,
            "deep": True,
            "video": {
                "codec": "h264",
                "width": 1080,
                "height": 1920,
                "fps": 30,
            },
            "audio": {"codec": "aac"},
            "maxVolumeDb": -12.5,
        },
    }


def install_verifier(monkeypatch: pytest.MonkeyPatch, result: object, *, returncode: int = 0) -> None:
    def fake_run(*args, **kwargs):
        assert args[0][0:4] == ["npm", "run", "reels:verify-release", "--"]
        assert args[0][-2:] == ["--deep", "--json"]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 300
        return subprocess.CompletedProcess(
            args[0],
            returncode,
            "> artfolio-reels@1.0.0 reels:verify-release\n> tsx scripts/verify-release.ts --deep --json\n\n"
            + json.dumps(result)
            + "\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_real_contract_package_and_verifier_result_yield_a_verified_release(monkeypatch, tmp_path):
    release = build_release(tmp_path)
    install_verifier(monkeypatch, verifier_output(release))

    with reel_release.verified_reel_release_snapshot(
        release,
        reels_repository=tmp_path / "artfolio-reels",
        snapshot_root=tmp_path / "snapshots",
    ) as verified:
        assert verified.artwork_id == REEL_ID
        assert verified.release_identity.reel_id == REEL_ID
        assert verified.release_identity.files_sha256["reel.mp4"] == _sha256(b"verified mp4 bytes")
        assert verified.release_identity.manifest_sha256 == _sha256((release / "manifest.json").read_bytes())
        assert verified.video_path.read_bytes() == b"verified mp4 bytes"
        assert verified.caption == "A verified caption.\n"
        assert verified.video_path.parent == verified.snapshot_directory
        assert stat.S_IMODE(verified.snapshot_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(verified.video_path.stat().st_mode) == 0o400
        release.joinpath("reel.mp4").write_bytes(b"mutated source bytes")
        assert verified.video_path.read_bytes() == b"verified mp4 bytes"

    assert not verified.snapshot_directory.exists()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda result: result.update(valid=False), "deep verifier"),
        (lambda result: result.update(errors=["decode failed"]), "deep verifier"),
        (lambda result: result.update(directory="/wrong/release"), "directory"),
        (lambda result: result.update(reelId="met_wrong"), "reel ID"),
        (lambda result: result.pop("media"), "media"),
        (lambda result: result["media"].update(deep=False), "deep"),
        (lambda result: result["media"].pop("audio"), "audio"),
        (lambda result: result["media"].update(maxVolumeDb=math.inf), "valid JSON"),
    ],
)
def test_invalid_deep_verifier_result_creates_no_snapshot(monkeypatch, tmp_path, mutate, match):
    release = build_release(tmp_path)
    result = verifier_output(release)
    mutate(result)
    install_verifier(monkeypatch, result)
    snapshots = tmp_path / "snapshots"

    with pytest.raises(reel_release.ReleaseIntakeError, match=match):
        with reel_release.verified_reel_release_snapshot(
            release, reels_repository=tmp_path / "artfolio-reels", snapshot_root=snapshots
        ):
            pytest.fail("invalid intake must not yield")

    assert not snapshots.exists() or not any(snapshots.iterdir())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda release: (release / "unexpected.txt").write_text("not allowed", encoding="utf-8"),
        lambda release: (release / "reel.mp4").unlink(),
        lambda release: (release / "caption.txt").write_bytes(b""),
        lambda release: (release / "qc" / "contact-sheet.png").unlink(),
        lambda release: os.symlink(release / "reel.mp4", release / "linked.mp4"),
    ],
)
def test_invalid_package_never_crosses_publication_boundaries(monkeypatch, tmp_path, mutation):
    release = build_release(tmp_path)
    result = verifier_output(release)
    mutation(release)
    install_verifier(monkeypatch, result)
    reserve_reel = Mock()
    stage_reel_mp4 = Mock()
    post_to_instagram = Mock()

    with pytest.raises(reel_release.ReleaseIntakeError):
        with reel_release.verified_reel_release_snapshot(
            release,
            reels_repository=tmp_path / "artfolio-reels",
            snapshot_root=tmp_path / "snapshots",
        ):
            pytest.fail("invalid package must not yield")

    reserve_reel.assert_not_called()
    stage_reel_mp4.assert_not_called()
    post_to_instagram.assert_not_called()


@pytest.mark.parametrize(
    "change_manifest",
    [
        lambda manifest: manifest.update(version="artfolio-release-v2"),
        lambda manifest: manifest["files"].pop("caption"),
        lambda manifest: manifest["files"].update({"extra": "extra.txt"}),
        lambda manifest: manifest["sha256"].update({"reel.mp4": "A" * 64}),
        lambda manifest: manifest.update(reelId="met_wrong"),
    ],
)
def test_manifest_contract_and_hashes_are_fail_closed(monkeypatch, tmp_path, change_manifest):
    release = build_release(tmp_path)
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    change_manifest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    install_verifier(monkeypatch, verifier_output(release))

    with pytest.raises(reel_release.ReleaseIntakeError):
        with reel_release.verified_reel_release_snapshot(
            release, reels_repository=tmp_path / "artfolio-reels", snapshot_root=tmp_path / "snapshots"
        ):
            pytest.fail("invalid manifest must not yield")


@pytest.mark.parametrize(
    "change_metadata",
    [
        lambda metadata: metadata.pop("title"),
        lambda metadata: metadata.update(unexpected="field"),
        lambda metadata: metadata.update(durationSeconds=0),
        lambda metadata: metadata.update(canonicalId="met_wrong"),
        lambda metadata: metadata.update(generatedAt="2026-09-11T11:56:00.000Z"),
    ],
)
def test_metadata_contract_and_identities_are_fail_closed(monkeypatch, tmp_path, change_metadata):
    release = build_release(tmp_path)
    metadata_path = release / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    change_metadata(metadata)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    install_verifier(monkeypatch, verifier_output(release))

    with pytest.raises(reel_release.ReleaseIntakeError):
        with reel_release.verified_reel_release_snapshot(
            release, reels_repository=tmp_path / "artfolio-reels", snapshot_root=tmp_path / "snapshots"
        ):
            pytest.fail("invalid metadata must not yield")


def test_release_id_selects_only_the_repositories_expected_release_directory(monkeypatch, tmp_path):
    release = build_release(tmp_path)
    install_verifier(monkeypatch, verifier_output(release))

    with reel_release.verified_reel_release_snapshot(
        REEL_ID, reels_repository=tmp_path / "artfolio-reels", snapshot_root=tmp_path / "snapshots"
    ) as verified:
        assert verified.release_directory == release.resolve()


def test_rejects_unsafe_release_id_before_invoking_verifier(monkeypatch, tmp_path):
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(reel_release.ReleaseIntakeError, match="release ID"):
        with reel_release.verified_reel_release_snapshot(
            "../escape", reels_repository=tmp_path / "artfolio-reels"
        ):
            pytest.fail("unsafe release IDs must not yield")

    run.assert_not_called()


def test_snapshot_remains_byte_identical_when_the_filesystem_partially_writes(monkeypatch, tmp_path):
    release = build_release(tmp_path)
    install_verifier(monkeypatch, verifier_output(release))
    real_write = os.write

    def partial_write(descriptor: int, data: bytes) -> int:
        partial_length = max(1, len(data) // 2)
        return real_write(descriptor, data[:partial_length])

    monkeypatch.setattr(reel_release.os, "write", partial_write)

    with reel_release.verified_reel_release_snapshot(
        release,
        reels_repository=tmp_path / "artfolio-reels",
        snapshot_root=tmp_path / "snapshots",
    ) as verified:
        assert verified.video_path.read_bytes() == b"verified mp4 bytes"
        assert verified.caption_path.read_bytes() == b"A verified caption.\n"
