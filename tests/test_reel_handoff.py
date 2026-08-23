import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from src.models import NormalizedArtwork
from src.reel_handoff import ReelHandoffExportError, export_reel_handoff


def _safe_artwork(**overrides) -> NormalizedArtwork:
    values = {
        "source": "aic",
        "source_id": "84774",
        "title": "A Sunday on La Grande Jatte",
        "artist_name": "Georges Seurat",
        "creation_date": "1884–1886",
        "medium": "Oil on canvas",
        "museum_name": "Art Institute of Chicago",
        "classification": "Painting",
        "artwork_url": "https://www.artic.edu/artworks/27992?api_key=never-export-this",
        "is_public_domain": True,
        "rights_status": "CONFIRMED_PUBLIC_DOMAIN",
        "image_width": 1800,
        "image_height": 1200,
    }
    values.update(overrides)
    return NormalizedArtwork(**values)


def _validated_image(tmp_path: Path, size: tuple[int, int] = (1800, 1200)) -> Path:
    path = tmp_path / "validated-artwork.jpg"
    Image.new("RGB", size, color=(35, 75, 120)).save(path, "JPEG")
    return path


def _read_handoff(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_exports_exact_remotion_handoff_from_confirmed_artwork(tmp_path):
    artwork = _safe_artwork()
    destination = export_reel_handoff(artwork, _validated_image(tmp_path), tmp_path / "output")

    handoff = _read_handoff(destination)
    assert set(handoff) == {
        "canonicalId", "source", "title", "artist", "date", "medium", "museum",
        "classification", "imagePath", "imageWidth", "imageHeight", "rightsStatus",
    }
    assert handoff["canonicalId"] == "aic_84774"
    assert handoff["source"] == artwork.source
    assert handoff["title"] == artwork.title
    assert handoff["artist"] == artwork.artist_name
    assert handoff["date"] == artwork.creation_date
    assert handoff["medium"] == artwork.medium
    assert handoff["museum"] == artwork.museum_name
    assert handoff["classification"] == artwork.classification
    assert handoff["rightsStatus"] == "CONFIRMED_PUBLIC_DOMAIN"
    assert (handoff["imageWidth"], handoff["imageHeight"]) == (1800, 1200)
    assert Path(handoff["imagePath"]).is_file()
    assert "never-export-this" not in destination.read_text(encoding="utf-8")


def test_rejects_unconfirmed_or_open_access_rights_without_export(tmp_path):
    image = _validated_image(tmp_path)
    for artwork in (
        _safe_artwork(rights_status=None),
        _safe_artwork(rights_status="CONFIRMED_OPEN_ACCESS"),
        _safe_artwork(is_public_domain=False),
    ):
        with pytest.raises(ReelHandoffExportError, match="CONFIRMED_PUBLIC_DOMAIN"):
            export_reel_handoff(artwork, image, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_uses_actual_validated_image_dimensions_not_stale_metadata(tmp_path):
    artwork = _safe_artwork(image_width=1600, image_height=1200)
    with pytest.raises(ReelHandoffExportError, match="do not match"):
        export_reel_handoff(artwork, _validated_image(tmp_path), tmp_path / "output")


def test_rejects_missing_metadata_and_missing_local_image_before_export(tmp_path):
    with pytest.raises(ReelHandoffExportError, match="medium"):
        export_reel_handoff(_safe_artwork(medium=None), _validated_image(tmp_path), tmp_path / "output")
    with pytest.raises(ReelHandoffExportError, match="missing"):
        export_reel_handoff(_safe_artwork(), tmp_path / "not-present.jpg", tmp_path / "output")
    invalid_image = tmp_path / "invalid.jpg"
    invalid_image.write_text("not an image", encoding="utf-8")
    with pytest.raises(ReelHandoffExportError, match="unreadable"):
        export_reel_handoff(_safe_artwork(), invalid_image, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_refuses_to_overwrite_a_different_existing_handoff_asset(tmp_path):
    artwork = _safe_artwork()
    output = tmp_path / "output"
    destination = export_reel_handoff(artwork, _validated_image(tmp_path), output)
    asset = Path(_read_handoff(destination)["imagePath"])
    asset.write_bytes(b"different bytes")

    with pytest.raises(ReelHandoffExportError, match="refusing to overwrite"):
        export_reel_handoff(artwork, _validated_image(tmp_path), output)


def test_exported_handoff_passes_actual_remotion_schema(tmp_path):
    destination = export_reel_handoff(_safe_artwork(), _validated_image(tmp_path), tmp_path / "output")
    art_bot_root = Path(__file__).resolve().parents[1]
    remotion_root = art_bot_root.parent / "Remotion İnstagram Reels" / "artfolio-reels"
    tsx = remotion_root / "node_modules" / ".bin" / "tsx"
    assert tsx.is_file(), "local Remotion contract runtime is required for this integration test"
    script = (
        "import {readFileSync} from 'node:fs';"
        "import {ArtworkHandoffSchema} from './src/planner/handoff.ts';"
        f"ArtworkHandoffSchema.parse(JSON.parse(readFileSync({json.dumps(str(destination))}, 'utf8')));"
    )
    result = subprocess.run([str(tsx), "-e", script], cwd=remotion_root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
