"""Trusted intake for one deeply verified ``artfolio-reels`` release package."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Iterator, Mapping

from src.models import ReelReleaseIdentity, normalize_artwork_id


_MAX_VERIFIER_OUTPUT_BYTES = 2 * 1024 * 1024
_RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE_FILES = frozenset({
    "reel.mp4",
    "caption.txt",
    "metadata.json",
    "manifest.json",
    "qc/contact-sheet.png",
})
_HASHED_FILES = frozenset(_PACKAGE_FILES - {"manifest.json"})
_MANIFEST_FILES = {
    "video": "reel.mp4",
    "caption": "caption.txt",
    "metadata": "metadata.json",
    "qcContactSheet": "qc/contact-sheet.png",
}
_METADATA_REQUIRED_FIELDS = {
    "canonicalId",
    "reelId",
    "title",
    "template",
    "durationSeconds",
    "hook",
    "generatedAt",
}
_METADATA_OPTIONAL_FIELDS = {
    "artist",
    "artworkTitle",
    "date",
    "museum",
    "hookType",
    "musicTrackId",
    "musicSubfamily",
}


class ReleaseIntakeError(ValueError):
    """The supplied release did not cross the verified-package trust boundary."""


@dataclass(frozen=True)
class VerifiedReelRelease:
    release_directory: Path
    snapshot_directory: Path
    video_path: Path
    caption_path: Path
    caption: str
    artwork_id: str
    release_identity: ReelReleaseIdentity


def _fail(message: str) -> None:
    raise ReleaseIntakeError(message)


def _require_regular_nonempty(path: Path, *, label: str) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ReleaseIntakeError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(details.st_mode):
        _fail(f"{label} must not be a symlink")
    if not stat.S_ISREG(details.st_mode):
        _fail(f"{label} must be a regular file")
    if details.st_size <= 0:
        _fail(f"{label} must not be empty")
    return details


def _require_directory(path: Path, *, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ReleaseIntakeError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(details.st_mode):
        _fail(f"{label} must not be a symlink")
    if not stat.S_ISDIR(details.st_mode):
        _fail(f"{label} must be a directory")


def _require_no_symlink_components(path: Path, *, label: str) -> None:
    """Reject symlinks before resolving a selected path."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            details = current.lstat()
        except OSError as exc:
            raise ReleaseIntakeError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(details.st_mode):
            _fail(f"{label} must not contain symlink components")


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _resolve_release(release: str | Path, reels_repository: str | Path) -> tuple[Path, Path, str]:
    is_directory_input = (isinstance(release, Path) and release.is_absolute()) or (
        isinstance(release, str) and Path(release).is_absolute()
    )
    if not is_directory_input and (not isinstance(release, str) or not _RELEASE_ID_PATTERN.fullmatch(release)):
        _fail("release ID must be one safe component")
    repository = _absolute_path(reels_repository)
    _require_no_symlink_components(repository, label="reels repository")
    _require_directory(repository, label="reels repository")
    repository = repository.resolve(strict=True)

    if is_directory_input:
        candidate = _absolute_path(release)
        _require_no_symlink_components(candidate, label="release directory")
        _require_directory(candidate, label="release directory")
        release_directory = candidate.resolve(strict=True)
        verifier_target = str(release_directory)
    else:
        release_directory = repository / "output" / "releases" / release
        _require_no_symlink_components(release_directory, label="release directory")
        _require_directory(release_directory, label="release directory")
        verifier_target = release
    return repository, release_directory, verifier_target


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON object contains duplicate keys")
        result[key] = value
    return result


def _parse_json_object(raw: str, *, label: str) -> dict[str, object]:
    if not isinstance(raw, str) or not raw.strip():
        _fail(f"{label} must contain exactly one JSON object")
    try:
        decoder = json.JSONDecoder(
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite value")),
        )
        parsed, end = decoder.raw_decode(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseIntakeError(f"{label} must contain valid JSON") from exc
    if raw[end:].strip():
        _fail(f"{label} must contain exactly one JSON object")
    if not isinstance(parsed, dict):
        _fail(f"{label} must be a JSON object")
    return parsed


def _parse_verifier_output(raw: str) -> dict[str, object]:
    """Parse one verifier JSON object, permitting npm's command banner only."""
    if not isinstance(raw, str) or not raw.strip():
        _fail("deep verifier output must contain exactly one JSON object")
    lines = raw.splitlines()
    json_lines = [index for index, line in enumerate(lines) if line.startswith("{")]
    if len(json_lines) != 1:
        _fail("deep verifier output must contain exactly one JSON object")
    json_line = json_lines[0]
    preamble = lines[:json_line]
    if any(line and not line.startswith("> ") for line in preamble):
        _fail("deep verifier output contains unexpected non-JSON output")
    return _parse_json_object("\n".join(lines[json_line:]), label="deep verifier output")


def _read_regular_bytes(path: Path, *, label: str) -> bytes:
    _require_regular_nonempty(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseIntakeError(f"{label} could not be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
            _fail(f"{label} must be a nonempty regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, label: str) -> str:
    return hashlib.sha256(_read_regular_bytes(path, label=label)).hexdigest()


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        _fail(f"{label} has an invalid key set")


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(f"{label} must be nonempty text")
    return value


def _require_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(f"{label} must be a finite number")
    return float(value)


def _validate_verifier_output(output: dict[str, object], release_directory: Path) -> str:
    if "media" not in output:
        _fail("deep verifier media evidence is missing")
    _require_exact_keys(output, {"valid", "errors", "directory", "reelId", "media"}, label="deep verifier output")
    if output["valid"] is not True or output["errors"] != []:
        _fail("deep verifier did not report a valid release")
    if output["directory"] != str(release_directory):
        _fail("deep verifier directory does not match the selected release")
    verifier_reel_id = _require_text(output["reelId"], label="deep verifier reel ID")
    if normalize_artwork_id(verifier_reel_id) != normalize_artwork_id(release_directory.name):
        _fail("deep verifier reel ID does not match the selected release")

    media = output["media"]
    if not isinstance(media, dict):
        _fail("deep verifier media evidence is missing")
    if "audio" not in media:
        _fail("deep verifier audio evidence is missing")
    expected_media_keys = {"path", "sizeBytes", "durationSeconds", "deep", "video", "audio", "maxVolumeDb"}
    _require_exact_keys(media, expected_media_keys, label="deep verifier media")
    if media["path"] != str(release_directory / "reel.mp4"):
        _fail("deep verifier media path does not match reel.mp4")
    size_bytes = media["sizeBytes"]
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        _fail("deep verifier media size must be positive")
    if media["deep"] is not True:
        _fail("deep verifier media evidence must be deep")

    video = media["video"]
    if not isinstance(video, dict):
        _fail("deep verifier video evidence is missing")
    _require_exact_keys(video, {"codec", "width", "height", "fps"}, label="deep verifier video")
    if _require_text(video["codec"], label="video codec").casefold() != "h264":
        _fail("video codec must be H.264")
    if video["width"] != 1080 or video["height"] != 1920:
        _fail("video dimensions must be 1080x1920")
    if abs(_require_number(video["fps"], label="video fps") - 30) > 0.01:
        _fail("video fps must be 30")
    duration = _require_number(media["durationSeconds"], label="media duration")
    if not 0 < duration <= 60.5:
        _fail("video duration must be positive and at most 60.5 seconds")

    audio = media["audio"]
    if not isinstance(audio, dict):
        _fail("deep verifier audio evidence is missing")
    _require_exact_keys(audio, {"codec"}, label="deep verifier audio")
    _require_text(audio["codec"], label="audio codec")
    if _require_number(media["maxVolumeDb"], label="maxVolumeDb") <= -90:
        _fail("maxVolumeDb must be greater than -90")
    return normalize_artwork_id(verifier_reel_id)


def _validate_package_inventory(release_directory: Path) -> None:
    _require_directory(release_directory, label="release directory")
    root_entries = {entry.name: entry for entry in release_directory.iterdir()}
    if set(root_entries) != {"reel.mp4", "caption.txt", "metadata.json", "manifest.json", "qc"}:
        _fail("release package has unexpected or missing paths")
    _require_directory(root_entries["qc"], label="release qc directory")
    qc_entries = {entry.name: entry for entry in root_entries["qc"].iterdir()}
    if set(qc_entries) != {"contact-sheet.png"}:
        _fail("release package has unexpected or missing paths")
    for relative_path in _PACKAGE_FILES:
        _require_regular_nonempty(release_directory / relative_path, label=f"release {relative_path}")


def _parse_package_json(path: Path, *, label: str) -> dict[str, object]:
    raw = _read_regular_bytes(path, label=label)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseIntakeError(f"{label} must be UTF-8 JSON") from exc
    return _parse_json_object(decoded, label=label)


def _validate_manifest_and_metadata(release_directory: Path, verifier_reel_id: str) -> ReelReleaseIdentity:
    manifest_path = release_directory / "manifest.json"
    metadata_path = release_directory / "metadata.json"
    manifest = _parse_package_json(manifest_path, label="manifest.json")
    metadata = _parse_package_json(metadata_path, label="metadata.json")
    _require_exact_keys(manifest, {"version", "reelId", "createdAt", "files", "sha256"}, label="manifest.json")
    if not _METADATA_REQUIRED_FIELDS <= set(metadata) or not set(metadata) <= (_METADATA_REQUIRED_FIELDS | _METADATA_OPTIONAL_FIELDS):
        _fail("metadata.json has an invalid key set")
    if manifest["version"] != "artfolio-release-v1":
        _fail("manifest version is unsupported")
    manifest_reel_id = _require_text(manifest["reelId"], label="manifest reelId")
    if not _RELEASE_ID_PATTERN.fullmatch(manifest_reel_id):
        _fail("manifest reelId is unsafe")
    metadata_canonical_id = _require_text(metadata["canonicalId"], label="metadata canonicalId")
    metadata_reel_id = _require_text(metadata["reelId"], label="metadata reelId")
    for field in ("title", "template", "hook"):
        _require_text(metadata[field], label=f"metadata {field}")
    for field in _METADATA_OPTIONAL_FIELDS & set(metadata):
        _require_text(metadata[field], label=f"metadata {field}")
    if _require_number(metadata["durationSeconds"], label="metadata durationSeconds") <= 0:
        _fail("metadata durationSeconds must be positive")
    created_at = _require_text(manifest["createdAt"], label="manifest createdAt")
    if metadata["generatedAt"] != created_at:
        _fail("metadata generatedAt must equal manifest createdAt")

    artwork_id = normalize_artwork_id(manifest_reel_id)
    if {artwork_id, normalize_artwork_id(metadata_canonical_id), normalize_artwork_id(metadata_reel_id), verifier_reel_id} != {artwork_id}:
        _fail("release identities do not agree")
    if normalize_artwork_id(release_directory.name) != artwork_id:
        _fail("release directory and reel ID do not agree")

    files = manifest["files"]
    if files != _MANIFEST_FILES:
        _fail("manifest files must exactly match the release file contract")
    hashes = manifest["sha256"]
    if not isinstance(hashes, dict) or set(hashes) != _HASHED_FILES:
        _fail("manifest sha256 must contain exactly the required hashes")
    for relative_path, digest in hashes.items():
        if not isinstance(relative_path, str) or not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            _fail("manifest file hashes must be lowercase SHA-256 digests")
        candidate = release_directory / relative_path
        try:
            candidate.relative_to(release_directory)
        except ValueError as exc:
            raise ReleaseIntakeError("manifest path escapes the release directory") from exc
        if _sha256_file(candidate, label=f"release {relative_path}") != digest:
            _fail(f"release {relative_path} hash does not match manifest")

    manifest_digest = _sha256_file(manifest_path, label="release manifest.json")
    try:
        return ReelReleaseIdentity(
            version="artfolio-release-v1",
            reel_id=artwork_id,
            created_at=created_at,
            manifest_sha256=manifest_digest,
            files_sha256=dict(hashes),
        )
    except ValueError as exc:
        raise ReleaseIntakeError("release identity is invalid") from exc


def _copy_snapshot_file(source: Path, destination: Path, *, label: str) -> tuple[Path, bytes, str]:
    _require_regular_nonempty(source, label=label)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise ReleaseIntakeError(f"{label} could not be opened safely") from exc
    try:
        source_details = os.fstat(source_fd)
        if not stat.S_ISREG(source_details.st_mode) or source_details.st_size <= 0:
            _fail(f"{label} must be a nonempty regular file")
        try:
            destination_fd = os.open(destination, destination_flags, 0o400)
        except OSError as exc:
            raise ReleaseIntakeError("private snapshot could not be created") from exc
        digest = hashlib.sha256()
        copied = bytearray()
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                digest.update(chunk)
                copied.extend(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(destination_fd, remaining)
                    if written <= 0:
                        _fail("private snapshot could not be written completely")
                    remaining = remaining[written:]
        finally:
            os.close(destination_fd)
        if not copied:
            _fail(f"{label} must not be empty")
        os.chmod(destination, 0o400)
        return destination, bytes(copied), digest.hexdigest()
    finally:
        os.close(source_fd)


@contextmanager
def verified_reel_release_snapshot(
    release: str | Path,
    *,
    reels_repository: str | Path,
    snapshot_root: str | Path | None = None,
) -> Iterator[VerifiedReelRelease]:
    """Yield an immutable, deeply verified snapshot and remove it on exit."""
    repository, release_directory, verifier_target = _resolve_release(release, reels_repository)
    try:
        completed = subprocess.run(
            ["npm", "run", "reels:verify-release", "--", verifier_target, "--deep", "--json"],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseIntakeError("deep verifier could not be executed") from exc
    if len(completed.stdout) > _MAX_VERIFIER_OUTPUT_BYTES or len(completed.stderr) > _MAX_VERIFIER_OUTPUT_BYTES:
        _fail("deep verifier output exceeded the allowed size")
    if completed.returncode != 0:
        _fail("deep verifier failed")

    verifier_output = _parse_verifier_output(completed.stdout)
    verifier_reel_id = _validate_verifier_output(verifier_output, release_directory)
    _validate_package_inventory(release_directory)
    release_identity = _validate_manifest_and_metadata(release_directory, verifier_reel_id)

    if snapshot_root is None:
        snapshot_parent = None
    else:
        snapshot_parent = _absolute_path(snapshot_root)
        snapshot_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_no_symlink_components(snapshot_parent, label="snapshot root")
        _require_directory(snapshot_parent, label="snapshot root")
    snapshot_directory = Path(tempfile.mkdtemp(dir=snapshot_parent))
    os.chmod(snapshot_directory, 0o700)
    try:
        video_path, _, video_digest = _copy_snapshot_file(
            release_directory / "reel.mp4", snapshot_directory / "reel.mp4", label="release reel.mp4"
        )
        caption_path, caption_bytes, caption_digest = _copy_snapshot_file(
            release_directory / "caption.txt", snapshot_directory / "caption.txt", label="release caption.txt"
        )
        if video_digest != release_identity.files_sha256["reel.mp4"] or caption_digest != release_identity.files_sha256["caption.txt"]:
            _fail("release changed while creating the private snapshot")
        try:
            caption = caption_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseIntakeError("release caption.txt must be UTF-8") from exc
        if not caption.strip():
            _fail("release caption.txt must not be blank")
        yield VerifiedReelRelease(
            release_directory=release_directory,
            snapshot_directory=snapshot_directory,
            video_path=video_path,
            caption_path=caption_path,
            caption=caption,
            artwork_id=release_identity.reel_id,
            release_identity=release_identity,
        )
    finally:
        shutil.rmtree(snapshot_directory, ignore_errors=True)
