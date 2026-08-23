"""Export an already-safe artwork into the Artfolio Remotion handoff contract.

This module is intentionally isolated from selection, publishing, Gemini, and
networking. Its input must already have passed the art bot's rights and secure
image-download gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import config
from PIL import Image

from src.models import NormalizedArtwork, normalize_image_dimensions


HANDOFF_RIGHTS_STATUS = "CONFIRMED_PUBLIC_DOMAIN"
DEFAULT_HANDOFF_DIRECTORY = Path(config.BASE_DIR) / "output" / "reel-handoffs"
_SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_PLACEHOLDER_METADATA = {
    "unknown",
    "unknown artist",
    "unknown date",
    "unknown medium",
    "unknown classification",
    "untitled",
}


class ReelHandoffExportError(ValueError):
    """Raised when an artwork cannot safely satisfy the Remotion handoff."""


def _safe_filename(canonical_id: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9_-]", "_", canonical_id)
    if not filename:
        raise ReelHandoffExportError("canonical artwork ID cannot produce an output filename")
    return filename


def _required_metadata(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise ReelHandoffExportError(f"missing required handoff metadata: {field}")
    normalized = value.strip()
    if not normalized or normalized.casefold() in _PLACEHOLDER_METADATA:
        raise ReelHandoffExportError(f"missing required handoff metadata: {field}")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measure_validated_image(path: Path, artwork: NormalizedArtwork) -> tuple[int, int]:
    if not path.is_file():
        raise ReelHandoffExportError(f"validated local image is missing: {path}")
    if path.suffix.casefold() not in _SUPPORTED_IMAGE_SUFFIXES:
        raise ReelHandoffExportError(
            f"validated local image format is not consumable by the Remotion planner: {path.suffix or '<none>'}"
        )
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            measured_dimensions = image.size
    except (OSError, ValueError) as error:
        raise ReelHandoffExportError(f"validated local image is unreadable: {path}") from error

    validated_dimensions = normalize_image_dimensions(artwork.image_width, artwork.image_height)
    if None in validated_dimensions:
        raise ReelHandoffExportError("validated image dimensions are unavailable on the selected artwork")
    if measured_dimensions != validated_dimensions:
        raise ReelHandoffExportError(
            "local image dimensions do not match the secure download measurement: "
            f"expected {validated_dimensions[0]}x{validated_dimensions[1]}, "
            f"found {measured_dimensions[0]}x{measured_dimensions[1]}"
        )
    return measured_dimensions


def _copy_asset_without_overwrite(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.is_file() or _sha256(source) != _sha256(destination):
            raise ReelHandoffExportError(f"refusing to overwrite existing handoff asset: {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".reel-handoff-", suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json_without_overwrite(destination: Path, handoff: dict[str, Any]) -> None:
    serialized = f"{json.dumps(handoff, indent=2, ensure_ascii=False)}\n"
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReelHandoffExportError(f"refusing to overwrite existing handoff JSON: {destination}") from error
        if existing != handoff:
            raise ReelHandoffExportError(f"refusing to overwrite existing handoff JSON: {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".reel-handoff-", suffix=".tmp", delete=False
    ) as temporary:
        temporary.write(serialized)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def export_reel_handoff(
    artwork: NormalizedArtwork,
    local_image_path: str | Path,
    output_directory: str | Path = DEFAULT_HANDOFF_DIRECTORY,
) -> Path:
    """Create a deterministic local handoff package for one safe, selected artwork."""
    if not artwork.is_public_domain or artwork.rights_status != HANDOFF_RIGHTS_STATUS:
        raise ReelHandoffExportError(
            "Reel handoff requires the existing CONFIRMED_PUBLIC_DOMAIN rights decision"
        )

    canonical_id = _required_metadata("canonicalId", artwork.canonical_id)
    source = _required_metadata("source", artwork.source)
    metadata = {
        "title": _required_metadata("title", artwork.title),
        "artist": _required_metadata("artist", artwork.artist_name),
        "date": _required_metadata("date", artwork.creation_date),
        "medium": _required_metadata("medium", artwork.medium),
        "museum": _required_metadata("museum", artwork.museum_name),
        "classification": _required_metadata("classification", artwork.classification),
    }
    source_image = Path(local_image_path).expanduser().resolve()
    image_width, image_height = _measure_validated_image(source_image, artwork)
    output_root = Path(output_directory).expanduser().resolve()
    filename = _safe_filename(canonical_id)
    asset_path = output_root / "assets" / f"{filename}{source_image.suffix.casefold()}"
    _copy_asset_without_overwrite(source_image, asset_path)

    # This is intentionally the exact required Remotion contract. Optional
    # sourceUrl is omitted so source URLs or their query credentials never leak.
    handoff = {
        "canonicalId": canonical_id,
        "source": source,
        **metadata,
        "imagePath": str(asset_path),
        "imageWidth": image_width,
        "imageHeight": image_height,
        "rightsStatus": HANDOFF_RIGHTS_STATUS,
    }
    destination = output_root / f"{filename}.json"
    _write_json_without_overwrite(destination, handoff)
    return destination


def _load_normalized_artwork(path: Path) -> NormalizedArtwork:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReelHandoffExportError(f"could not read normalized artwork JSON: {path}") from error
    try:
        return NormalizedArtwork.model_validate(raw)
    except ValueError as error:
        raise ReelHandoffExportError("input is not a valid NormalizedArtwork record") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a safe Artfolio Remotion artwork handoff")
    parser.add_argument("artwork_json", type=Path, help="JSON serialization of an already-normalized artwork")
    parser.add_argument("--local-image", required=True, type=Path, help="Already-validated local artwork image")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_HANDOFF_DIRECTORY)
    args = parser.parse_args()

    handoff_path = export_reel_handoff(
        _load_normalized_artwork(args.artwork_json), args.local_image, args.output_dir
    )
    print(handoff_path)


if __name__ == "__main__":
    main()
