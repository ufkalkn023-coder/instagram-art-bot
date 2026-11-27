from __future__ import annotations

from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

from src.design_selector import (
    HEAVY_TYPO_FAMILIES,
    audit_design_catalog_eligibility,
    extract_artwork_visual_profile,
    select_profile_design,
)
from src.museum_pool import (
    calculate_schedule_artwork_need,
    compute_image_dhash,
    flag_visual_near_duplicates,
    normalize_artist_name,
)
from src.profile_rehearsal import build_real_museum_rehearsal


def _make_img(path: Path, size: tuple[int, int], bg: tuple[int, int, int], fg: tuple[int, int, int]) -> Path:
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle((size[0] // 6, size[1] // 6, size[0] * 5 // 6, size[1] * 5 // 6), fill=fg)
    img.save(path, format="JPEG", quality=92)
    return path


def test_design_catalog_eligibility_and_artwork_neighbor_selector(tmp_path: Path) -> None:
    audit = audit_design_catalog_eligibility()
    assert audit["total_usable"] == 280
    assert audit["carousel_1_source_eligible"] == 261
    assert audit["carousel_2_source_eligible"] == 15
    assert audit["carousel_3_source_eligible"] == 2
    assert audit["carousel_4_source_eligible"] == 2

    land = _make_img(tmp_path / "land.jpg", (1600, 1000), (40, 60, 90), (210, 180, 120))
    prof_long = extract_artwork_visual_profile(land, "A Very Long Museum Title Describing an Expansive River Valley at Dusk")
    assert prof_long["orientation"] == "landscape"
    assert prof_long["title_bucket"] == "long"

    # Long landscape title should avoid heavy typography families
    did, dec = select_profile_design(
        post_format="carousel",
        required_sources=1,
        lead_profile=prof_long,
        placed_posts=[{"design_id": "astra-001", "family": "typography", "orientation": "portrait"}],
        usage_counts=Counter(),
        family_counts=Counter(),
    )
    assert dec["family"] not in HEAVY_TYPO_FAMILIES
    assert did.startswith(("astra-", "neon-"))


def test_artist_normalization_dhash_flagging_and_exact_artwork_need(tmp_path: Path) -> None:
    assert normalize_artist_name("Paul Cézanne") == normalize_artist_name("Paul Cezanne")

    need = calculate_schedule_artwork_need(days=30, carousels_per_day=4, reels_per_day=2, reel_revisit_ratio=0.20)
    assert need["total_posts"] == 180
    assert need["total_carousels"] == 120
    assert need["total_reels"] == 60
    assert need["total_unique_artworks_needed"] == 208

    # Two visually identical images with distinct canonical_ids are FLAGGED, never auto-deleted
    p1 = _make_img(tmp_path / "dup1.jpg", (1000, 1200), (30, 40, 50), (200, 150, 90))
    p2 = _make_img(tmp_path / "dup2.jpg", (1000, 1200), (30, 40, 50), (200, 150, 90))
    h1, h2 = compute_image_dhash(p1), compute_image_dhash(p2)
    flags = flag_visual_near_duplicates(
        [
            {"canonical_id": "met_1", "title": "Print A", "dhash": h1},
            {"canonical_id": "rijks_2", "title": "Print B", "dhash": h2},
        ],
        max_distance=8,
    )
    assert len(flags) == 1
    assert flags[0]["action"] == "flagged_for_review_kept_both"


def test_rehearsal_never_pads_with_demo_and_generates_ab_3x3(tmp_path: Path) -> None:
    # Build a small 14-artwork verified pool -> cannot fill 30 days, must report honest shortfall (NO fake padding!)
    pool = []
    for i in range(1, 15):
        size = (1400, 1000) if i % 2 == 0 else (1000, 1400)
        p = _make_img(tmp_path / f"m{i:02d}.jpg", size, (25 * i % 240, 45, 75), (210, 175, 110))
        pool.append(
            {
                "canonical_id": f"cleveland_{i:04d}",
                "source_key": f"cleveland_{i:04d}",
                "artist": f"Master {i}",
                "artist_normalized": f"master {i}",
                "title": f"Gallery Study {i}",
                "date": str(1650 + i * 15),
                "institution": "Cleveland Museum of Art" if i % 2 == 0 else "Rijksmuseum",
                "museum_url": f"https://museum.example/{i}",
                "rights_status": "CONFIRMED_PUBLIC_DOMAIN",
                "rights_basis": "CC0",
                "image_path": str(p),
                "dhash": compute_image_dhash(p),
            }
        )

    out_dir = tmp_path / "rehearsal"
    res = build_real_museum_rehearsal(
        pool,
        out_dir,
        days=30,
        carousels_per_day=4,
        reels_per_day=2,
        render_sample_mp4_count=0,
    )
    s = res["summary"]
    assert s["shortfall_unique_artworks"] > 0
    assert s["completed_days"] < 30
    assert s["unique_artworks_used"] <= len(pool)
    assert Path(res["html_path"]).is_file()
    assert s["ab_comparison"]["generated"] is True
    assert Path(s["ab_comparison"]["combined_ab_path"]).is_file()
