"""Tests for all 280 Artfolio Astra + Neon renderers, determinism, and zero prototype leakage."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.astra_catalog import load_design_catalog
from src.astra_renderer import AstraArtworkInput, render_astra_design, render_astra_image

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_artwork(tmp_path, name: str, size: tuple[int, int], colors: tuple[str, str, str], year: str = "1889") -> AstraArtworkInput:
    path = tmp_path / f"{name}.jpg"
    im = Image.new("RGB", size, colors[0])
    draw = ImageDraw.Draw(im)
    w, h = size
    draw.rectangle((0, 0, w // 2, h // 2), fill=colors[1])
    draw.ellipse((w // 4, h // 4, (w * 3) // 4, (h * 3) // 4), fill=colors[2])
    im.save(path, format="JPEG", quality=90)
    return AstraArtworkInput(
        image_path=path,
        title=f"Study {name}",
        artist=f"Artist {name}",
        date=year,
        institution="Test Museum",
        museum_url=f"https://example.org/art/{name}",
    )


def test_all_280_astra_and_neon_templates_render_1080x1350_cleanly(tmp_path):
    catalog = load_design_catalog()
    assert len(catalog.designs) == 280
    arts = [
        _make_artwork(tmp_path, "alpha", (900, 680), ("#8b261d", "#e3a64b", "#203647"), "1835"),
        _make_artwork(tmp_path, "beta", (680, 920), ("#19335c", "#d9d2b8", "#5b7a5e"), "1874"),
        _make_artwork(tmp_path, "gamma", (840, 840), ("#2b5c3f", "#f0d49c", "#6c3024"), "1901"),
        _make_artwork(tmp_path, "delta", (960, 640), ("#5e2b54", "#e6c8d8", "#2d3b4e"), "1645"),
    ]
    for entry in catalog.designs:
        sources = arts[: entry.required_sources]
        image, meta = render_astra_image(entry.id, sources, f"Editorial study for {entry.id}")
        assert image.size == (1080, 1350), f"{entry.id} wrong size: {image.size}"
        assert meta["design_id"] == entry.id
        assert meta["collection"] == entry.collection


@pytest.mark.parametrize(
    "design_id",
    [
        "astra-001", "astra-013", "astra-031", "astra-061", "astra-071", "astra-083", "astra-100",
        "neon-001", "neon-051", "neon-062", "neon-073", "neon-100", "neon-122", "neon-132",
        "neon-151", "neon-173", "neon-174", "neon-180",
    ],
)
def test_representative_astra_and_neon_templates_are_deterministic_and_artwork_sensitive(tmp_path, design_id):
    art_a = _make_artwork(tmp_path, "red_landscape", (1200, 800), ("#8b261d", "#e3a64b", "#203647"), "1834")
    art_b = _make_artwork(tmp_path, "blue_portrait", (700, 1100), ("#19335c", "#d9d2b8", "#5b7a5e"), "1912")

    res_a1 = render_astra_design(design_id, [art_a], "First editorial title", tmp_path / f"{design_id}_a1.jpg")
    res_a2 = render_astra_design(design_id, [art_a], "First editorial title", tmp_path / f"{design_id}_a2.jpg")
    res_b = render_astra_design(design_id, [art_b], "Second editorial title", tmp_path / f"{design_id}_b.jpg")

    assert res_a1["sha256"] == res_a2["sha256"]
    assert res_a1["sha256"] != res_b["sha256"]
    with Image.open(res_a1["output_path"]) as rendered:
        assert rendered.size == (1080, 1350)
        assert rendered.format == "JPEG"


def test_pigment_ledger_computes_palette_from_input_image(tmp_path):
    warm = _make_artwork(tmp_path, "warm", (900, 700), ("#c83c1c", "#f2c14e", "#6b1f12"))
    cool = _make_artwork(tmp_path, "cool", (900, 700), ("#123456", "#3a7ca5", "#d9e8f5"))

    res_warm = render_astra_design("astra-061", [warm], "Warm pigments", tmp_path / "warm.jpg")
    res_cool = render_astra_design("astra-061", [cool], "Cool pigments", tmp_path / "cool.jpg")

    assert len(res_warm["palette_hex"]) == 5
    assert len(res_cool["palette_hex"]) == 5
    assert res_warm["palette_hex"] != res_cool["palette_hex"]


def test_multi_source_templates_enforce_exact_source_count(tmp_path):
    arts = [
        _make_artwork(tmp_path, f"work_{i}", (900, 900), (c1, c2, c3))
        for i, (c1, c2, c3) in enumerate(
            [
                ("#7a2a22", "#e2c391", "#2b3b42"),
                ("#234232", "#c8a454", "#ede2c2"),
                ("#2a3d66", "#d9825b", "#f0e7d8"),
                ("#522b47", "#d4b483", "#364958"),
            ]
        )
    ]

    with pytest.raises(ValueError, match="requires 2"):
        render_astra_design("astra-021", arts[:1], "Too few", tmp_path / "bad2.jpg")
    with pytest.raises(ValueError, match="requires 3"):
        render_astra_design("astra-094", arts[:2], "Too few", tmp_path / "bad3.jpg")
    with pytest.raises(ValueError, match="requires 4"):
        render_astra_design("neon-160", arts[:3], "Too few", tmp_path / "bad4.jpg")

    r2 = render_astra_design("neon-060", arts[:2], "Kintsugi duet", tmp_path / "ok2.jpg")
    r3 = render_astra_design("astra-094", arts[:3], "Three rooms", tmp_path / "ok3.jpg")
    r4 = render_astra_design("neon-160", arts[:4], "Vote your room", tmp_path / "ok4.jpg")
    assert r2["width"] == r3["width"] == r4["width"] == 1080


def test_edge_cases_long_title_aspect_ratios_focus_and_missing_source(tmp_path):
    panoramic = _make_artwork(tmp_path, "panoramic", (1600, 400), ("#2b4c59", "#e0c097", "#7d3c2d"))
    tall = _make_artwork(tmp_path, "tall", (420, 1400), ("#4b3b47", "#d9c2a3", "#2b594c"))
    long_title = "An unusually long museum editorial headline that must wrap cleanly without clipping or overlapping"

    res_long = render_astra_design("astra-083", [panoramic], long_title, tmp_path / "long.jpg")
    res_tall = render_astra_design(
        "astra-013",
        [tall],
        long_title,
        tmp_path / "tall.jpg",
        detail_focuses=[(0.2, 0.25, 0.85, 0.75)],
    )
    assert res_long["width"] == 1080 and res_tall["width"] == 1080

    missing = AstraArtworkInput(image_path=tmp_path / "does_not_exist.jpg", title="Missing", artist="Nobody")
    with pytest.raises(FileNotFoundError):
        render_astra_design("astra-001", [missing], "Should fail", tmp_path / "missing.jpg")


def test_renderers_contain_zero_hardcoded_prototype_artworks_or_text():
    forbidden = (
        "aic_103139",
        "aic_14655",
        "rijksmuseum_SK-",
        "met_438001",
        "Claude Monet",
        "Walter Shirlaw",
        "Caspar Netscher",
        "David Dalhoff Neal",
        "WATERLOO",
        "SHIRLAW",
        "Who gets\\nthe light?",
    )
    renderer_files = [
        REPO_ROOT / "src" / "astra_renderer.py",
        REPO_ROOT / "src" / "neon_kit.py",
        REPO_ROOT / "src" / "neon_boards_1_9.py",
        REPO_ROOT / "src" / "neon_boards_10_18.py",
        REPO_ROOT / "src" / "neon_renderer.py",
    ]
    for path in renderer_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"Found prototype token {token!r} in {path.name}"
