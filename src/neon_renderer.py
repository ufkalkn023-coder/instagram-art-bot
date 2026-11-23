"""Unified dispatcher for all 180 Artfolio Neon design templates (neon-001..neon-180)."""

from __future__ import annotations

from typing import Sequence

from PIL import Image

from src.astra_catalog import DesignCatalogEntry
from src.astra_renderer import AstraArtworkInput, normalize_focus_point
from src.neon_boards_1_9 import render_neon_boards_1_to_9
from src.neon_boards_10_18 import render_neon_boards_10_to_18


def render_neon_image(
    entry: DesignCatalogEntry,
    sources: Sequence[AstraArtworkInput],
    editorial_title: str,
    *,
    detail_focuses: Sequence[tuple[float, float] | tuple[float, float, float, float]] | None = None,
) -> tuple[Image.Image, dict]:
    """Render any Neon template (neon-001..neon-180) deterministically to 1080x1350."""
    if entry.collection != "neon" or not 1 <= entry.number <= 180:
        raise ValueError(f"Unsupported Neon design id: {entry.id}")
    if len(sources) != entry.required_sources:
        raise ValueError(
            f"Design '{entry.id}' ({entry.name}) requires {entry.required_sources} "
            f"artwork source(s), received {len(sources)}"
        )
    focus = (
        normalize_focus_point(detail_focuses[0])
        if detail_focuses
        else sources[0].focal_point((0.5, 0.48))
    )
    if entry.number <= 90:
        image = render_neon_boards_1_to_9(entry.number, sources, editorial_title, focus)
    else:
        image = render_neon_boards_10_to_18(entry.number, sources, editorial_title, focus)

    metadata = {
        "design_id": entry.id,
        "design_name": entry.name,
        "collection": "neon",
        "family": entry.family,
        "family_name": entry.family_name,
        "width": image.width,
        "height": image.height,
        "source_count": len(sources),
        "focus": list(focus),
    }
    return image, metadata
