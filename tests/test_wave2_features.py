from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from src.art_calendar import find_anniversaries_for_date, render_split_screen_comparison
from src.astra_renderer import AstraArtworkInput
from src.editorial_copy_rules import compose_seo_alt_text
from src.grid_choreography import sort_schedule_by_color_bridge
from src.meta_business_export import export_meta_business_suite_package
from src.monthly_planner import (
    apply_split_comparison_mode,
    build_monthly_post_specs,
    create_monthly_plan_workspace,
    render_monthly_editor_html,
    reorder_plan_by_color_bridge,
)
from src.reel_studio import render_local_reel_mp4, verify_reel_mp4

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


def _paint(path: Path, bg: tuple[int, int, int], accent: tuple[int, int, int]) -> Path:
    img = Image.new("RGB", (1200, 1500), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle((140, 180, 1060, 1320), fill=accent)
    img.save(path, format="JPEG", quality=92)
    return path


def test_on_this_day_calendar_and_prime_time_reel_spotlight(tmp_path: Path) -> None:
    nov14 = find_anniversaries_for_date(date(2026, 11, 14))
    assert len(nov14) == 1
    assert "Monet" in nov14[0].artist

    assets = tmp_path / "assets"
    assets.mkdir()
    meta = {}
    artists = [
        ("Claude Monet", "Water Lilies"),
        ("Rembrandt", "Night Study"),
        ("Berthe Morisot", "Reading"),
        ("Edgar Degas", "Rehearsal"),
        ("Gustav Klimt", "Gold Study"),
        ("Johannes Vermeer", "Delft Interior"),
        ("Mary Cassatt", "Tea"),
        ("J. M. W. Turner", "Harbor"),
    ]
    for i, (artist, title) in enumerate(artists, 1):
        stem = f"w{i:02d}"
        _paint(assets / f"{stem}.jpg", (20 + i * 20, 40 + i * 15, 70), (180, 140, 90))
        meta[stem] = [artist, title, str(1840 + i * 5), f"https://museum.example/{stem}", f"ACC-{i}", "Rijksmuseum"]
    (assets / "art.json").write_text(json.dumps(meta), encoding="utf-8")

    # Start on Nov 14, 2026 (Monet's birthday!)
    start_dt = datetime(2026, 11, 14, 9, 30, tzinfo=ISTANBUL_TZ)
    specs, history, proj_map = build_monthly_post_specs(
        assets, days=1, carousels_per_day=4, reels_per_day=2, start_at=start_dt
    )
    prime_reels = [s for s in specs if s["post_format"] == "reel" and s.get("anniversary_note")]
    assert len(prime_reels) == 1
    assert "Monet" in prime_reels[0]["anniversary_note"]
    assert "Monet" in prime_reels[0]["artworks"][0]["artist"]


def test_split_screen_before_after_comparison_renderer(tmp_path: Path) -> None:
    p1 = _paint(tmp_path / "study.jpg", (80, 74, 65), (150, 140, 125))
    p2 = _paint(tmp_path / "final.jpg", (28, 48, 86), (218, 174, 88))
    a1 = AstraArtworkInput(image_path=p1, title="Preliminary Drawing", artist="Leonardo da Vinci", date="1495", institution="Louvre")
    a2 = AstraArtworkInput(image_path=p2, title="Final Oil Canvas", artist="Leonardo da Vinci", date="1498", institution="Louvre")

    out_study = render_split_screen_comparison(a1, a2, tmp_path / "split_study.jpg", mode="study_vs_final")
    out_restore = render_split_screen_comparison(a1, a2, tmp_path / "split_restore.jpg", mode="before_after_restoration")
    for p in (out_study, out_restore):
        assert p.is_file()
        with Image.open(p) as im:
            assert im.size == (1080, 1350)


def test_reel_3sec_kinetic_caption_overlay(tmp_path: Path) -> None:
    src = _paint(tmp_path / "reel.jpg", (24, 30, 42), (204, 166, 98))
    aw = AstraArtworkInput(image_path=src, title="The Mirror Study", artist="Jan van Eyck", date="1434", institution="National Gallery")
    out_mp4 = tmp_path / "kinetic.mp4"
    info = render_local_reel_mp4(
        aw,
        "Look Behind",
        out_mp4,
        preset="detail_to_full",
        fps=15,
        seamless_loop=True,
        kinetic_hook="Look at the convex mirror in the back.",
    )
    assert info["kinetic_hook"] == "Look at the convex mirror in the back."
    probe = verify_reel_mp4(out_mp4)
    assert probe["width"] == 1080 and probe["height"] == 1920
    assert probe["codec"] == "h264" and probe["pix_fmt"] == "yuv420p"


def test_seo_alt_text_and_color_bridge_preserving_reel_spine(tmp_path: Path) -> None:
    alt = compose_seo_alt_text(
        [{"title": "Reading", "artist": "Berthe Morisot", "date": "1873", "institution": "Cleveland Museum of Art"}],
        post_format="carousel",
        tone_band="light",
        palette_hex=["#3A513F", "#B1AEA6", "#F2ECE1"],
    )
    assert "Berthe Morisot" in alt and "Reading" in alt and "1873" in alt
    assert "#" not in alt and len(alt) <= 260

    # Color-Bridge must chain pigments AND keep format slots [C, R, C, C, R, C] intact
    entries = [
        {"id": "p1", "post_format": "carousel", "primary_rgb": (10, 10, 10), "secondary_rgb": (240, 220, 180)},
        {"id": "p2", "post_format": "reel", "primary_rgb": (20, 30, 90), "secondary_rgb": (20, 30, 90)},
        {"id": "p3", "post_format": "carousel", "primary_rgb": (20, 35, 95), "secondary_rgb": (200, 60, 40)},
        {"id": "p4", "post_format": "carousel", "primary_rgb": (205, 55, 38), "secondary_rgb": (50, 120, 60)},
        {"id": "p5", "post_format": "reel", "primary_rgb": (235, 215, 175), "secondary_rgb": (25, 35, 92)},
        {"id": "p6", "post_format": "carousel", "primary_rgb": (48, 118, 58), "secondary_rgb": (12, 12, 12)},
    ]
    ordered = sort_schedule_by_color_bridge(entries, preserve_format_slots=True)
    fmt_by_id = {e["id"]: e["post_format"] for e in entries}
    assert [fmt_by_id[pid] for pid in ordered] == ["carousel", "reel", "carousel", "carousel", "reel", "carousel"]
    # p1's secondary (240,220,180) should pick p5 (235,215,175) as the first Reel!
    assert ordered[1] == "p5"

    # End-to-end workspace: Alt-Text export + Color-Bridge + Split-Screen UI check
    assets = tmp_path / "mini"
    assets.mkdir()
    meta = {}
    for i in range(1, 9):
        stem = f"a{i:02d}"
        _paint(assets / f"{stem}.jpg", (25 * i % 240, 50, 90), (200, 150, 100))
        meta[stem] = [f"Artist {i}", f"Canvas {i}", f"{1600 + i * 20}", f"https://met.example/{i}", f"ACC-{i}", "The Met"]
    (assets / "art.json").write_text(json.dumps(meta), encoding="utf-8")
    ws = tmp_path / "ws"
    specs, history, proj_map = build_monthly_post_specs(assets, days=2, carousels_per_day=4, reels_per_day=2)
    state = create_monthly_plan_workspace(ws, specs, days=2, carousels_per_day=4, reels_per_day=2, history_specs=history, project_slides_map=proj_map)

    # Apply split-screen comparison on a 2-artwork carousel
    two_art = next(p for p in state.posts if p.post_format == "carousel" and len(p.artworks) == 2)
    split_state = apply_split_comparison_mode(ws, two_art.id, "study_vs_final")
    assert next(p for p in split_state.posts if p.id == two_art.id).comparison_mode == "study_vs_final"

    # Reorder by Color-Bridge & verify Reels stay at indices 1, 4, 7, 10
    cb_state = reorder_plan_by_color_bridge(ws)
    pmap = {p.id: p for p in cb_state.posts}
    assert [pmap[pid].post_format for pid in cb_state.schedule_order[:6]] == ["carousel", "reel", "carousel", "carousel", "reel", "carousel"]

    # Check UI HTML exposes all 5 Wave-2 controls
    html = render_monthly_editor_html(cb_state, editor_token="tok")
    for needle in ["btn-color-bridge", "ann-box", "in-kinetic-hook", "btn-split-study", "btn-split-restore", "ins-alt-text", "btn-copy-alt"]:
        assert needle in html

    # Export Meta Business Suite package and confirm alt_text_en.txt exists for every approved post
    exp_dir = tmp_path / "exp"
    export_meta_business_suite_package(ws, exp_dir, overwrite=True)
    alt_files = list((exp_dir / "posts").glob("*/alt_text_en.txt"))
    assert len(alt_files) == 6
    assert alt_files[0].read_text(encoding="utf-8").strip() != ""
