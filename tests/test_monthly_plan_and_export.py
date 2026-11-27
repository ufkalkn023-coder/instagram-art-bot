"""Tests for Carousel+Reels ONLY 6/day planner (4 Carousels + 2 Reels/day), Reel studio, and Meta export."""

from __future__ import annotations

import csv
import json
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from src.meta_business_export import export_meta_business_suite_package
from src.monthly_planner import (
    adjust_monthly_plan_cadence,
    build_daily_cadence_schedule,
    build_monthly_post_specs,
    compute_profile_at_moment,
    create_monthly_plan_workspace,
    load_monthly_plan_state,
    monthly_plan_server,
    render_monthly_reel_video,
    update_monthly_post_design,
)
from src.reel_studio import GRID_H, REEL_H, REEL_W, verify_reel_mp4
from tests.test_editorial_project import make_editorial_project


def _make_museum_assets_dir(tmp_path: Path, count: int = 12) -> Path:
    assets_dir = tmp_path / "museum_assets"
    assets_dir.mkdir(parents=True)
    meta = {}
    colors = [
        ("#8b261d", "#e3a64b"), ("#19335c", "#d9d2b8"), ("#2d4a3e", "#f0d9a8"),
        ("#663344", "#e8c4b8"), ("#334455", "#c4d4e0"), ("#554422", "#e4d0a0"),
        ("#223344", "#99bbaa"), ("#773322", "#ddccaa"), ("#2a4d3c", "#d8b46c"),
        ("#4c2a3e", "#e2c9a6"), ("#1f3a52", "#c9d6df"), ("#5a3b2c", "#e8d5b7"),
    ]
    for idx in range(count):
        key = f"work_{idx + 1:02d}"
        bg, fg = colors[idx % len(colors)]
        im = Image.new("RGB", (900, 720), bg)
        ImageDraw.Draw(im).ellipse((160, 120, 740, 600), fill=fg)
        im.save(assets_dir / f"{key}.jpg", format="JPEG", quality=88)
        meta[key] = [
            f"Museum Artist {idx + 1}",
            f"Masterwork Number {idx + 1}",
            f"18{50 + idx * 5}",
            f"https://clevelandart.org/art/1950.{idx + 1}",
            f"1950.{idx + 1}",
            "Cleveland Museum of Art",
        ]
    (assets_dir / "art.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return assets_dir


def test_six_daily_schedule_produces_180_posts_in_30_days_with_center_reel_spine():
    start_at = datetime(2026, 12, 1, 6, 30, tzinfo=timezone.utc)
    slots = build_daily_cadence_schedule(start_at, days=30, carousels_per_day=4, reels_per_day=2)
    assert len(slots) == 180
    formats = [fmt for fmt, _ in slots]
    assert formats.count("carousel") == 120
    assert formats.count("reel") == 60
    assert "single" not in formats
    # Every 3-post row follows Carousel | Reel | Carousel so Reels form the center spine!
    for row_start in range(0, 180, 3):
        assert formats[row_start : row_start + 3] == ["carousel", "reel", "carousel"]


def test_carousel_and_reel_workspace_exceeds_12_cap_references_editorial_project_and_time_travels(tmp_path):
    assets_dir = _make_museum_assets_dir(tmp_path)
    proj_root = tmp_path / "existing_editorial_project"
    proj_root.mkdir()
    proj_dir = make_editorial_project(proj_root, detail=True)

    start_at = datetime(2026, 12, 1, 6, 30, tzinfo=timezone.utc)
    # 3 days x 6/day = 18 posts (12 carousels + 6 reels, 0 singles; exceeds 12-project cap!)
    posts, history, proj_slides = build_monthly_post_specs(
        assets_dir,
        days=3,
        carousels_per_day=4,
        reels_per_day=2,
        start_at=start_at,
        editorial_projects=[proj_dir],
    )
    assert len(posts) == 18
    assert all(p["post_format"] in ("carousel", "reel") for p in posts)
    assert any(p.get("editorial_project_dir") == str(proj_dir.resolve()) for p in posts)

    ws = tmp_path / "studio_ws"
    state = create_monthly_plan_workspace(
        ws,
        posts,
        days=3,
        carousels_per_day=4,
        reels_per_day=2,
        start_at=start_at,
        history_specs=history,
        pinned_ids=("post-001", "post-002", "post-006"),
        project_slides_map=proj_slides,
    )
    assert state.publication_ready is False
    assert state.carousel_count == 12 and state.reel_count == 6

    # Verify every Reel generated a real 1080x1920 9:16 cover wrapping the 1080x1350 4:5 cover
    first_reel = next(p for p in state.posts if p.post_format == "reel")
    assert first_reel.reel_cover_9x16_path
    with Image.open(ws / first_reel.reel_cover_9x16_path) as im_9x16:
        assert im_9x16.size == (REEL_W, REEL_H)
    with Image.open(ws / first_reel.slides[0]) as im_4x5:
        assert im_4x5.size == (REEL_W, GRID_H)

    # Time-travel moment 12: 3 pinned posts at top, zero duplication, newest non-pinned next
    m12 = compute_profile_at_moment(state, moment_index=12)
    assert [c["id"] for c in m12[:3]] == ["post-001", "post-002", "post-006"]
    assert len({c["id"] for c in m12}) == len(m12)
    assert m12[3]["id"] == "post-012"

    # Switch design on a Reel -> both 4:5 and 9:16 covers regenerate cleanly
    update_monthly_post_design(ws, first_reel.id, "neon-180")
    reloaded = load_monthly_plan_state(ws)
    updated_reel = next(p for p in reloaded.posts if p.id == first_reel.id)
    assert updated_reel.design_id == "neon-180"
    assert (ws / updated_reel.reel_cover_9x16_path).is_file()

    # Adjust cadence dynamically to 2 days x (3 carousels + 1 reel) = 8 posts
    adjusted = adjust_monthly_plan_cadence(ws, days=2, carousels_per_day=3, reels_per_day=1)
    assert len(adjusted.posts) == 8
    assert adjusted.carousel_count == 6 and adjusted.reel_count == 2


def test_real_ffmpeg_reel_render_and_meta_business_suite_export(tmp_path):
    assets_dir = _make_museum_assets_dir(tmp_path)
    posts, history, _ = build_monthly_post_specs(
        assets_dir, days=1, carousels_per_day=4, reels_per_day=2
    )
    # Approve all 6 posts of Day 1 (4 Carousels + 2 Reels)
    for p in posts:
        p["approval_status"] = "approved"
    # Keep last carousel as draft to confirm unapproved items are skipped
    posts[-1]["approval_status"] = "draft"

    ws = tmp_path / "ws_reel_export"
    state = create_monthly_plan_workspace(
        ws, posts, days=1, carousels_per_day=4, reels_per_day=2, history_specs=history
    )

    # Render a REAL 1080x1920 H.264 MP4 for the first Reel via FFmpeg; leave second Reel as pending draft!
    reels = [p for p in state.posts if p.post_format == "reel"]
    assert len(reels) == 2
    state_after, render_info = render_monthly_reel_video(ws, reels[0].id, preset="detail_to_full")
    assert render_info["width"] == REEL_W and render_info["height"] == REEL_H
    assert render_info["codec"] == "h264" and render_info["pix_fmt"] == "yuv420p"
    assert render_info["duration_sec"] >= 7.0
    assert next(p for p in state_after.posts if p.id == reels[0].id).reel_status == "video_ready"
    assert next(p for p in state_after.posts if p.id == reels[1].id).reel_status == "draft_awaiting_reel_video"

    export_dir = tmp_path / "meta-export"
    report = export_meta_business_suite_package(ws, export_dir)
    assert report["exported_count"] == 5
    assert report["skipped_unapproved_count"] == 1
    assert report["ready_count"] == 4  # 3 carousels + 1 rendered MP4 reel
    assert report["draft_reels_awaiting_video"] == 1  # 1 pending reel

    manifest = json.loads((export_dir / "export_manifest.json").read_text(encoding="utf-8"))
    exported_reels = [r for r in manifest["posts"] if r["post_format"] == "reel"]
    ready_reel = next(r for r in exported_reels if r["export_status"] == "ready_for_meta")
    draft_reel = next(r for r in exported_reels if r["export_status"] == "draft_awaiting_reel_video")

    assert (export_dir / ready_reel["reel_video"]).is_file()
    assert (export_dir / ready_reel["reel_cover_9x16"]).is_file()
    assert (export_dir / ready_reel["reel_cover_grid_4x5"]).is_file()
    probed = verify_reel_mp4(export_dir / ready_reel["reel_video"])
    assert probed["width"] == 1080 and probed["height"] == 1920

    draft_folder = (export_dir / draft_reel["caption_file"]).parent
    assert (draft_folder / "REEL_VIDEO_PENDING.txt").is_file()
    assert (draft_folder / "cover_reel_9x16.jpg").is_file()
    assert (draft_folder / "reel_storyboard.json").is_file()

    with (export_dir / "meta_schedule.csv").open("r", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 5
    assert "reel_cover_9x16" in rows[0]


def test_loopback_studio_api_supports_live_reel_mp4_render_and_replan(tmp_path):
    assets_dir = _make_museum_assets_dir(tmp_path)
    posts, history, _ = build_monthly_post_specs(
        assets_dir, days=1, carousels_per_day=2, reels_per_day=1
    )
    ws = tmp_path / "ws_api"
    create_monthly_plan_workspace(ws, posts, days=1, carousels_per_day=2, reels_per_day=1, history_specs=history)

    server = monthly_plan_server(ws, port=0)
    port = server.server_port
    token = server.editor_token  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def _post(route: str, body: dict) -> dict:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{route}",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{port}",
                    "X-Artfolio-Editor": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                assert resp.status == 200
                return json.loads(resp.read().decode("utf-8"))

        reel_id = next(p["id"] for p in posts if p["post_format"] == "reel")
        res_reel = _post("/api/render-reel", {"post_id": reel_id, "preset": "palette_reveal"})
        assert res_reel["reel_render"]["width"] == 1080 and res_reel["reel_render"]["height"] == 1920
        assert any(c["id"] == reel_id and c["reel_status"] == "video_ready" for c in res_reel["full_cells"])

        res_replan = _post("/api/replan", {"days": 1, "carousels_per_day": 4, "reels_per_day": 2})
        assert res_replan["carousel_count"] == 4 and res_replan["reel_count"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
