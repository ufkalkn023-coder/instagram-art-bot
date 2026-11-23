from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.artwork_intelligence import (
    audit_rendered_slide,
    detect_smart_focus,
    parse_dimensions_cm,
    should_show_monumental_scale_slide,
    trim_museum_scan_borders,
    validate_artwork_resolution,
)
from src.astra_renderer import AstraArtworkInput
from src.editorial_copy_rules import (
    build_targeted_hashtags,
    compose_curated_caption,
    compose_first_comment,
    lint_english_caption,
)
from src.grid_choreography import (
    SIGNATURE_DAYS,
    TEMPO_PROFILES,
    analyze_grid_rhythm,
    compute_cover_tone,
)
from src.insights_feedback import analyze_instagram_insights_csv, compute_design_weights
from src.meta_business_export import export_meta_business_suite_package
from src.monthly_planner import (
    adjust_monthly_plan_cadence,
    build_monthly_post_specs,
    create_monthly_plan_workspace,
    create_plan_snapshot,
    load_monthly_plan_state,
    mark_post_meta_uploaded,
    render_monthly_editor_html,
    reorder_post_slides,
    restore_latest_snapshot,
)
from src.museum_pool import ArtworkReuseLedger, infer_century, select_diverse_pool
from src.reel_studio import render_local_reel_mp4, verify_reel_mp4


def _make_sample_painting(path: Path, size: tuple[int, int] = (1400, 1800), with_border: bool = False) -> Path:
    img = Image.new("RGB", size, (34, 48, 62))
    draw = ImageDraw.Draw(img)
    draw.rectangle((120, 120, size[0] - 120, size[1] - 120), fill=(52, 68, 84))
    cx, cy = int(size[0] * 0.65), int(size[1] * 0.35)
    for r in range(180, 20, -16):
        color = (220 - r // 3, 175 - r // 4, 110) if (r // 16) % 2 == 0 else (30, 38, 48)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    if with_border:
        framed = Image.new("RGB", (size[0] + 100, size[1] + 100), (245, 245, 242))
        framed.paste(img, (50, 50))
        img = framed
    img.save(path, format="JPEG", quality=92)
    return path


def _make_mini_assets_dir(base: Path, count: int = 8) -> Path:
    assets_dir = base / "mini_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    museums = ["Rijksmuseum", "The Metropolitan Museum of Art", "Art Institute of Chicago", "National Gallery of Art"]
    meta = {}
    for i in range(1, count + 1):
        stem = f"art_{i:02d}"
        _make_sample_painting(assets_dir / f"{stem}.jpg", (1280, 1600))
        meta[stem] = {
            "title": f"Study {i}",
            "artist": f"Master {i}",
            "date": str(1600 + i * 25),
            "institution": museums[i % len(museums)],
            "museum_url": f"https://museum.example/work/{i}",
            "dimensions_cm": "363 × 437 cm" if i == 1 else "65 × 50 cm",
        }
    (assets_dir / "art.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return assets_dir


def test_artwork_intelligence_focus_border_resolution_and_monumental_gate(tmp_path: Path) -> None:
    p_hi = _make_sample_painting(tmp_path / "hi.jpg", (1400, 1800), with_border=True)
    p_lo = _make_sample_painting(tmp_path / "lo.jpg", (600, 800), with_border=False)

    # #40 Resolution gate
    res_hi = validate_artwork_resolution(p_hi, min_short_edge=1200)
    res_lo = validate_artwork_resolution(p_lo, min_short_edge=1200)
    assert res_hi["hd_ready"] is True and res_hi["width"] == 1500 and res_hi["height"] == 1900
    assert res_lo["hd_ready"] is False and res_lo["width"] == 600 and res_lo["height"] == 800

    # #43 Museum scan border trim
    with Image.open(p_hi) as raw:
        trimmed, trim_info = trim_museum_scan_borders(raw)
        assert trim_info["trimmed"] is True
        assert trimmed.width < raw.width and trimmed.height < raw.height
        # #09 Smart focus detector should stay in sane normalized bounds
        fx, fy = detect_smart_focus(trimmed)
        assert 0.15 <= fx <= 0.85 and 0.15 <= fy <= 0.85

    # #18 Monumental scale slide ONLY triggers for >= 180 cm works!
    assert parse_dimensions_cm("363 × 437 cm") == (363.0, 437.0)
    assert parse_dimensions_cm("100 x 80 cm") == (100.0, 80.0)
    assert parse_dimensions_cm("2.5 x 3.2 m") == (250.0, 320.0)
    assert should_show_monumental_scale_slide("363 × 437 cm")[0] is True
    assert should_show_monumental_scale_slide("180 x 120 cm")[0] is True
    assert should_show_monumental_scale_slide("179 × 120 cm")[0] is False
    assert should_show_monumental_scale_slide("60 × 45 cm")[0] is False
    assert should_show_monumental_scale_slide("")[0] is False

    # #19 Slide QA audit
    assert audit_rendered_slide(p_hi, expected_size=(1500, 1900))["passed"] is True
    blank = tmp_path / "blank.jpg"
    Image.new("RGB", (1080, 1350), (255, 255, 255)).save(blank)
    assert audit_rendered_slide(blank)["passed"] is False


def test_editorial_copy_rules_linter_hashtags_and_first_comment() -> None:
    # #33 Anti-AI cliché linter
    bad = "Step into a breathtaking masterpiece! Let's delve into this tapestry of timeless beauty."
    lint_bad = lint_english_caption(bad)
    assert lint_bad["passed"] is False
    assert len(lint_bad["found_cliches"]) >= 2

    # #29, #30, #32, #35, #36 Curated caption + 5 hashtags + first comment
    artworks = [
        {
            "title": "The Night Watch",
            "artist": "Rembrandt",
            "date": "1642",
            "institution": "Rijksmuseum",
            "dimensions_cm": "363 × 437 cm",
            "museum_url": "https://www.rijksmuseum.nl",
        }
    ]
    caption = compose_curated_caption(post_format="carousel", artworks=artworks, slot_index=0)
    lint_ok = lint_english_caption(caption)
    assert lint_ok["passed"] is True
    assert lint_ok["found_cliches"] == []

    tags_str = build_targeted_hashtags("Rembrandt", "Rijksmuseum", "carousel")
    assert len([t for t in tags_str.split() if t.startswith("#")]) == 5
    first_comment = compose_first_comment(artworks)
    assert "Rijksmuseum" in first_comment
    assert "Rembrandt" in first_comment


def test_grid_choreography_tempo_signature_days_and_rhythm(tmp_path: Path) -> None:
    assert set(TEMPO_PROFILES.keys()) == {"2_per_day", "4_per_day", "6_per_day"}
    assert len(SIGNATURE_DAYS) == 7

    dark_img = tmp_path / "dark.jpg"
    light_img = tmp_path / "light.jpg"
    Image.new("RGB", (200, 250), (20, 24, 30)).save(dark_img)
    Image.new("RGB", (200, 250), (235, 228, 210)).save(light_img)
    assert compute_cover_tone(dark_img)["tone_band"] == "dark"
    assert compute_cover_tone(light_img)["tone_band"] == "light"

    cells = [
        {"post_format": "carousel", "tone_band": "dark", "design_family": "minimal"},
        {"post_format": "reel", "tone_band": "dark", "design_family": "minimal"},
        {"post_format": "carousel", "tone_band": "dark", "design_family": "minimal"},
    ]
    report = analyze_grid_rhythm(cells)
    assert report["center_reel_ratio"] == 1.0
    assert report["balanced"] is False
    assert len(report["tone_clump_rows"]) == 1


def test_museum_pool_diversity_and_180_day_reuse_ledger(tmp_path: Path) -> None:
    ledger = ArtworkReuseLedger(tmp_path / "ledger.json")
    assert ledger.is_blocked("aw-1", cooldown_days=180) is False
    ledger.mark_used(["aw-1"])
    ledger.save()
    assert ledger.is_blocked("aw-1", cooldown_days=180) is True
    assert infer_century("1642") == 17
    assert infer_century("c. 1889") == 19

    hi_img = _make_sample_painting(tmp_path / "pool.jpg", (1400, 1800))
    candidates = []
    for i in range(12):
        candidates.append(
            {
                "source_key": f"aw-{i}",
                "title": f"Work {i}",
                "artist": f"Artist {i // 3}",
                "institution": "The Met" if i < 8 else "Rijksmuseum",
                "date": f"{1600 + i * 30}",
                "image_path": str(hi_img),
            }
        )
    selected, stats = select_diverse_pool(
        candidates,
        target_count=6,
        reuse_ledger=ledger,
        max_per_artist=2,
        max_museum_share=0.5,
        min_short_edge=1200,
    )
    assert len(selected) == 6
    assert all(item["source_key"] != "aw-1" for item in selected)
    assert stats["unique_eligible"] == 11


def test_insights_csv_analyzer_english_and_turkish_headers(tmp_path: Path) -> None:
    en_csv = tmp_path / "en_insights.csv"
    en_csv.write_text(
        "Post ID,Post type,Description,Publish time,Views,Reach,Likes,Comments,Saves,Shares\n"
        "1,IG carousel,Carousel A?,08/10/2026 14:30,1000,900,120,10,80,30\n"
        "2,IG reel,Reel B #art,08/10/2026 20:00,2500,2200,180,5,60,90\n",
        encoding="utf-8",
    )
    sc_en = analyze_instagram_insights_csv(en_csv)
    assert sc_en["total_posts"] == 2
    assert "IG carousel" in sc_en["by_format"]
    assert "IG reel" in sc_en["by_format"]

    tr_csv = tmp_path / "tr_insights.csv"
    tr_csv.write_text(
        '"Gönderi Kodu","Gönderi türü",Açıklama,"Yayınlanma zamanı",Görüntülemeler,Erişim,Beğenmeler,Yorumlar,Kaydedilenler,Paylaşımlar\n'
        '10,IG döngüsü,"Which detail?",08/10/2026 17:30,3000,2600,200,12,110,40\n',
        encoding="utf-8",
    )
    sc_tr = analyze_instagram_insights_csv(tr_csv)
    assert sc_tr["total_posts"] == 1
    assert sc_tr["by_format"]["IG döngüsü"]["avg_reach"] == 2600.0
    assert sc_tr["by_format"]["IG döngüsü"]["total_saves"] == 110

    weights = compute_design_weights(sc_en, ["astra-001", "neon-010", "neon-151"], exploration_quota=0.25)
    assert len(weights) == 3
    assert abs(sum(weights.values()) - 1.0) < 1e-5


def test_reel_studio_custom_zoom_path_and_seamless_loop(tmp_path: Path) -> None:
    src = _make_sample_painting(tmp_path / "reel_src.jpg", (1400, 1800))
    out_mp4 = tmp_path / "loop.mp4"
    artwork = AstraArtworkInput(
        image_path=src,
        title="Nocturne Study",
        artist="Test Master",
        date="1665",
        institution="Rijksmuseum",
    )
    info = render_local_reel_mp4(
        artwork,
        "Nocturne Study",
        out_mp4,
        preset="detail_to_full",
        fps=15,
        zoom_start=(0.65, 0.35),
        zoom_end=(0.50, 0.50),
        seamless_loop=True,
    )
    assert out_mp4.is_file()
    assert info["seamless_loop"] is True
    assert info["zoom_start"] == [0.65, 0.35]
    probe = verify_reel_mp4(out_mp4)
    assert probe["width"] == 1080 and probe["height"] == 1920
    assert probe["codec"] == "h264"
    assert probe["pix_fmt"] == "yuv420p"


def test_end_to_end_workspace_slides_reorder_upload_snapshot_and_weekly_export(tmp_path: Path) -> None:
    mini_assets = _make_mini_assets_dir(tmp_path, count=8)
    ws = tmp_path / "studio"
    specs, history, proj_map = build_monthly_post_specs(
        mini_assets,
        days=2,
        carousels_per_day=2,
        reels_per_day=1,
    )
    state = create_monthly_plan_workspace(
        ws,
        specs,
        days=2,
        carousels_per_day=2,
        reels_per_day=1,
        history_specs=history,
        project_slides_map=proj_map,
    )
    assert len(state.posts) == 6

    # Check UI HTML renders all new controls (#20, #24, #27, #31, #45, #46, #49, #50)
    html = render_monthly_editor_html(state, editor_token="tok")
    for needle in [
        "tab-today",
        "btn-heatmap",
        "btn-mobile-preview",
        "sel-tempo-profile",
        "btn-snapshot",
        "btn-restore",
        "chk-split-weeks",
        "btn-slide-left",
        "chk-seamless-loop",
        "btn-uploaded",
        "ins-first-comment",
    ]:
        assert needle in html

    # #20 Reorder carousel slides
    carousel = next(p for p in state.posts if p.post_format == "carousel" and len(p.slides) >= 2)
    orig_slides = list(carousel.slides)
    swapped = [orig_slides[0], orig_slides[-1], *orig_slides[1:-1]]
    reordered_state = reorder_post_slides(ws, carousel.id, swapped)
    updated_c = next(p for p in reordered_state.posts if p.id == carousel.id)
    assert list(updated_c.slides) == swapped

    # #45 Mark post as Meta uploaded
    up_state = mark_post_meta_uploaded(ws, carousel.id, True)
    assert next(p for p in up_state.posts if p.id == carousel.id).meta_uploaded is True

    # #49 Snapshot & restore around replan
    create_plan_snapshot(ws, label="before_test")
    adjust_monthly_plan_cadence(ws, days=1, carousels_per_day=1, reels_per_day=1)
    assert len(load_monthly_plan_state(ws).posts) == 2
    restored = restore_latest_snapshot(ws)
    assert len(restored.posts) == 6

    # #46 Weekly split export + #32 first_comment_en.txt + #42 reuse ledger
    export_dir = tmp_path / "meta-export"
    ledger_path = tmp_path / "reuse_ledger.json"
    report = export_meta_business_suite_package(
        ws,
        export_dir,
        overwrite=True,
        split_weeks=True,
        reuse_ledger_path=ledger_path,
    )
    assert report["split_weeks"] is True
    assert (export_dir / "weeks" / "Week-01").is_dir()
    assert ledger_path.is_file()
    first_comments = list((export_dir / "weeks").glob("Week-*/*/first_comment_en.txt"))
    assert len(first_comments) == 6
    assert first_comments[0].read_text(encoding="utf-8").strip() != ""
