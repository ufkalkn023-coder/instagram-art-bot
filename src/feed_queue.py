"""Bounded local Feed queue with atomic ownership and conservative consumption.

This store is for one host/shared filesystem. It is not a distributed R2 queue;
GitHub runners need an explicitly provisioned persistent store before enabling it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4

from src.feed_content import PreparedFeedContent
from src.insights_storage import parse_aware_timestamp, utc_timestamp
from src.instagram_image import inspect_instagram_image_publishability
from src.models import CONFIRMED_RIGHTS_STATUSES, require_canonical_artwork_id
from src.publication_state import canonical_bytes, payload_digest, seal

MAX_MANIFEST_BYTES = 2_000_000
MAX_QUEUE_PACKAGES = 5
MAX_PACKAGE_AGE_HOURS = 14 * 24
ARTWORK_FIELDS = frozenset({
    "id", "title", "artist", "date", "museum", "image_url", "artwork_url", "source",
    "is_public_domain", "rights_status", "license", "credit_line", "alt_text", "medium",
    "classification", "description", "period", "region", "quality_score", "measurement_coverage",
    "selection_score", "visual_category", "period_or_style", "style_or_period", "normalized_artist_key",
    "semantic_family", "published_orientation", "visual_tone", "luminance_bucket", "visual_color_family",
    "orientation", "dominant_color", "engagement_features", "engagement_applied", "learned_score",
    "engagement_confidence", "quality_component", "engagement_component", "diversity_component",
    "exploration_component", "exploration_selected",
})


@dataclass(frozen=True)
class QueueClaim:
    package_id: str
    content: PreparedFeedContent


def _now(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Queue time must be timezone-aware")
    return timestamp.astimezone(timezone.utc)


class PreparedFeedQueue:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory).resolve()
        self.manifest = self.directory / "queue.json"

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / ".queue.lock"
        if lock_path.is_symlink():
            raise RuntimeError("Queue lock must not be a symlink")
        with lock_path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Prepared queue is owned by another process") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> dict:
        if not self.manifest.exists():
            return {"schema_version": 1, "packages": []}
        if self.manifest.is_symlink() or self.manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise RuntimeError("Prepared queue manifest is unsafe")
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        if (not isinstance(document, dict) or document.get("schema_version") != 1
                or document.get("payload_sha256") != payload_digest(document)
                or not isinstance(document.get("packages"), list)
                or not 3 <= len(document["packages"]) <= MAX_QUEUE_PACKAGES):
            raise RuntimeError("Prepared queue manifest failed validation")
        seen = set()
        for package in document["packages"]:
            if not isinstance(package, dict):
                raise RuntimeError("Invalid prepared queue entry")
            identifier = package.get("id")
            try:
                valid_id = str(UUID(identifier, version=4)) == identifier
            except (ValueError, TypeError, AttributeError):
                valid_id = False
            if (not valid_id or identifier in seen
                    or package.get("state") not in {"READY", "CLAIMED", "CONSUMED", "QUARANTINED"}
                    or not isinstance(package.get("content"), dict)
                    or package.get("content_sha256") != hashlib.sha256(canonical_bytes(package["content"])).hexdigest()):
                raise RuntimeError("Invalid or conflicting prepared package")
            seen.add(identifier)
            if package["state"] in {"CLAIMED", "CONSUMED"} and not isinstance(package.get("owner"), str):
                raise RuntimeError("Prepared package has no recorded owner")
        return document

    def _write(self, document: dict) -> None:
        encoded = canonical_bytes(seal(document))
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("Prepared queue manifest exceeds its bound")
        fd, path = tempfile.mkstemp(prefix=".queue-write-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(path, self.manifest)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _asset_path(self, package_id: str, relative: str) -> Path:
        if not isinstance(relative, str):
            raise ValueError("Invalid prepared asset path")
        path = self.directory / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.parent != self.directory / package_id or path.is_symlink()
                or path.parent.is_symlink() or not path.resolve().is_relative_to(self.directory)):
            raise ValueError("Prepared asset escaped package ownership")
        return path

    def _content(self, package: dict) -> PreparedFeedContent:
        content = package["content"]
        assets = content.get("assets")
        if not isinstance(assets, list) or not 1 <= len(assets) <= 9:
            raise ValueError("Invalid prepared asset list")
        paths = []
        for asset in assets:
            path = self._asset_path(package["id"], asset["path"])
            if not path.is_file() or path.stat().st_size > 8_000_000:
                raise ValueError("Prepared media is missing or oversized")
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise ValueError("Prepared media digest mismatch")
            if not inspect_instagram_image_publishability(str(path)).publishable:
                raise ValueError("Prepared media is not Instagram-compatible")
            paths.append(str(path))
        result = PreparedFeedContent(
            content["publication_format"], tuple(content["artworks"]), tuple(paths), content["caption"],
            alt_text=content.get("alt_text"), publication_metadata=content.get("publication_metadata", {}),
            theme_id=content.get("theme_id"), theme_family=content.get("theme_family"),
            carousel_format=content.get("carousel_format"),
        )
        ids = [require_canonical_artwork_id(identifier) for identifier in result.publication_ids]
        if len(ids) != len(set(ids)):
            raise ValueError("Prepared content repeats an artwork")
        if any(art.get("is_public_domain") is not True or art.get("rights_status") not in CONFIRMED_RIGHTS_STATUSES
               for art in result.artworks):
            raise ValueError("Prepared content requires confirmed source rights")
        return result

    def build(self, *, target: int, first_format: str,
              prepare: Callable[[str, Path, set[str]], PreparedFeedContent],
              now: datetime | None = None, ttl_hours: int = MAX_PACKAGE_AGE_HOURS) -> None:
        timestamp = _now(now)
        if isinstance(target, bool) or not isinstance(target, int) or not 3 <= target <= MAX_QUEUE_PACKAGES:
            raise ValueError("Prepare between 3 and 5 Feed packages")
        if first_format not in {"single", "carousel"}:
            raise ValueError("Unsupported starting Feed format")
        if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, int) or not 1 <= ttl_hours <= MAX_PACKAGE_AGE_HOURS:
            raise ValueError("Prepared content expiry must be within 14 days")
        with self._lock():
            if self.manifest.exists():
                raise FileExistsError("Use a new directory; existing queue evidence is preserved")
            packages, excluded = [], set()
            format_name = first_format
            for _ in range(target):
                package_id = str(uuid4())
                workdir = self.directory / package_id
                workdir.mkdir()
                prepared = prepare(format_name, workdir, set(excluded))
                if prepared.publication_format != format_name:
                    raise ValueError("Preparation returned the wrong Feed format")
                assets = []
                for index, source in enumerate(prepared.media_paths):
                    path = workdir / f"media-{index}.jpg"
                    if Path(source).resolve() != path.resolve():
                        shutil.copyfile(source, path)
                    assets.append({"path": f"{package_id}/{path.name}",
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                data = {"publication_format": prepared.publication_format,
                        "artworks": [{key: value for key, value in art.items() if key in ARTWORK_FIELDS}
                                     for art in prepared.artworks],
                        "assets": assets, "caption": prepared.caption, "alt_text": prepared.alt_text,
                        "publication_metadata": prepared.publication_metadata,
                        "theme_id": prepared.theme_id, "theme_family": prepared.theme_family,
                        "carousel_format": prepared.carousel_format}
                package = {"id": package_id, "state": "READY", "owner": None, "reason": None,
                           "created_at": utc_timestamp(timestamp),
                           "expires_at": utc_timestamp(timestamp + timedelta(hours=ttl_hours)),
                           "content": data, "content_sha256": hashlib.sha256(canonical_bytes(data)).hexdigest()}
                validated = self._content(package)
                if any(identifier in excluded for identifier in validated.publication_ids):
                    raise ValueError("Prepared queue repeats an earlier artwork")
                excluded.update(validated.publication_ids)
                packages.append(package)
                format_name = "single" if format_name == "carousel" else "carousel"
            self._write({"schema_version": 1, "packages": packages})

    def status(self) -> list[dict]:
        return [{"id": package["id"], "publication_format": package["content"]["publication_format"],
                 "state": package["state"], "reason": package["reason"], "created_at": package["created_at"],
                 "expires_at": package["expires_at"]} for package in self._load()["packages"]]

    def claim(self, expected_format: str, *, protected_ids: set[str], owner: str,
              now: datetime | None = None) -> QueueClaim | None:
        timestamp = _now(now)
        if expected_format not in {"single", "carousel"} or not isinstance(owner, str) or not owner.strip():
            raise ValueError("Queue claim requires a Feed format and owner")
        with self._lock():
            document = self._load()
            changed = False
            for package in document["packages"]:
                if package["state"] != "READY" or package["content"]["publication_format"] != expected_format:
                    continue
                created, expiry = parse_aware_timestamp(package["created_at"]), parse_aware_timestamp(package["expires_at"])
                reason = None
                if created is None or expiry is None or created > timestamp or expiry <= created or expiry-created > timedelta(hours=MAX_PACKAGE_AGE_HOURS):
                    reason = "invalid_age"
                elif timestamp >= expiry:
                    reason = "expired"
                else:
                    try:
                        content = self._content(package)
                        if any(identifier in protected_ids for identifier in content.publication_ids):
                            reason = "protected_artwork"
                    except (OSError, ValueError, KeyError, TypeError):
                        reason = "invalid_content"
                if reason:
                    package.update(state="QUARANTINED", reason=reason)
                    changed = True
                    continue
                package.update(state="CLAIMED", owner=owner, reason=None)
                self._write(document)
                return QueueClaim(package["id"], content)
            if changed:
                self._write(document)
            return None

    def finish(self, package_id: str, *, owner: str, successful: bool) -> None:
        with self._lock():
            document = self._load()
            package = next((item for item in document["packages"] if item["id"] == package_id), None)
            if package is None or package["state"] != "CLAIMED" or package["owner"] != owner:
                raise RuntimeError("Prepared package ownership does not match")
            package.update(state="CONSUMED" if successful else "QUARANTINED",
                           reason="confirmed_publication" if successful else "unverified_outcome")
            self._write(document)
