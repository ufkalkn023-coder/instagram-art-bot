"""Meta Business Suite local handoff package exporter (Carousel + Reels, never auto-publishes)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.reel_studio import GRID_H, GRID_TOP_OFFSET, REEL_H, REEL_MOTION_PRESETS, REEL_W

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _format_istanbul(iso_str: str) -> tuple[str, str]:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist = dt.astimezone(ISTANBUL_TZ)
    utc = dt.astimezone(timezone.utc)
    return ist.strftime("%Y-%m-%d %H:%M (%Z)"), utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug_stamp(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ISTANBUL_TZ).strftime("%Y-%m-%d_%H%M")


def export_meta_business_suite_package(
    workspace_dir: str | Path,
    export_dir: str | Path,
    *,
    overwrite: bool = False,
    split_weeks: bool = False,
    reuse_ledger_path: str | Path | None = None,
) -> dict:
    """Export approved posts (ordered carousel slides, Reel MP4 + 9:16/4:5 covers, EN captions,
    and suggested Istanbul/UTC timestamps) into a clean local Meta Business Suite upload package.
    Reels without a real video file are explicitly flagged as 'draft_awaiting_reel_video'.
    """
    from src.monthly_planner import load_monthly_plan_state

    ws_root = Path(workspace_dir).resolve()
    state = load_monthly_plan_state(ws_root)
    destination = Path(export_dir).resolve()
    if destination.exists() or destination.is_symlink():
        if not overwrite:
            raise FileExistsError(f"Export directory already exists: {destination}")
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        else:
            shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    post_map = {item.id: item for item in state.posts}
    ordered_posts = [post_map[pid] for pid in state.schedule_order]

    exported_rows: list[dict] = []
    skipped_unapproved: list[str] = []
    ready_count = 0
    draft_reel_count = 0

    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".meta-export-") as tmp:
        stage = Path(tmp)
        posts_dir = stage / "posts"
        posts_dir.mkdir(parents=True, exist_ok=True)

        export_idx = 0
        start_dt_ref = datetime.fromisoformat(state.start_at).astimezone(ISTANBUL_TZ)
        exported_artwork_keys: list[str] = []
        for post in ordered_posts:
            if post.approval_status != "approved":
                skipped_unapproved.append(post.id)
                continue

            export_idx += 1
            stamp = _slug_stamp(post.scheduled_at)
            folder_name = f"{export_idx:03d}_{stamp}_{post.post_format}_{post.id}"
            if split_weeks:
                post_dt = datetime.fromisoformat(post.scheduled_at).astimezone(ISTANBUL_TZ)
                week_num = max(1, ((post_dt.date() - start_dt_ref.date()).days // 7) + 1)
                item_dir = stage / "weeks" / f"Week-{week_num:02d}" / folder_name
            else:
                item_dir = posts_dir / folder_name
            item_dir.mkdir(parents=True, exist_ok=True)
            exported_artwork_keys.extend(a.source_key for a in post.artworks)

            ist_label, utc_label = _format_istanbul(post.scheduled_at)
            exported_media: list[dict] = []
            reel_cover_rel = ""
            reel_cover_9x16_rel = ""
            reel_video_rel = ""
            export_status = "ready_for_meta"
            reel_note = ""

            if post.post_format == "carousel":
                for s_idx, rel_slide in enumerate(post.slides, 1):
                    src_slide = (ws_root / rel_slide).resolve()
                    if not src_slide.is_file():
                        raise FileNotFoundError(f"Missing carousel slide {s_idx} for {post.id}: {src_slide}")
                    out_slide = item_dir / f"{s_idx:02d}_slide.jpg"
                    shutil.copyfile(src_slide, out_slide)
                    exported_media.append(
                        {
                            "role": "carousel_cover" if s_idx == 1 else f"carousel_slide_{s_idx:02d}",
                            "slide_index": s_idx,
                            "relative_path": out_slide.relative_to(stage).as_posix(),
                            "sha256": _sha256(out_slide),
                        }
                    )
            else:  # reel
                src_grid_cover = (ws_root / post.slides[0]).resolve()
                if not src_grid_cover.is_file():
                    raise FileNotFoundError(f"Missing Reel 4:5 cover for {post.id}: {src_grid_cover}")
                out_grid_cover = item_dir / "cover_grid_4x5.jpg"
                shutil.copyfile(src_grid_cover, out_grid_cover)
                reel_cover_rel = out_grid_cover.relative_to(stage).as_posix()
                exported_media.append(
                    {
                        "role": "reel_cover_grid_4x5",
                        "relative_path": reel_cover_rel,
                        "sha256": _sha256(out_grid_cover),
                    }
                )

                if post.reel_cover_9x16_path:
                    src_9x16 = (ws_root / post.reel_cover_9x16_path).resolve()
                    if src_9x16.is_file():
                        out_9x16 = item_dir / "cover_reel_9x16.jpg"
                        shutil.copyfile(src_9x16, out_9x16)
                        reel_cover_9x16_rel = out_9x16.relative_to(stage).as_posix()
                        exported_media.append(
                            {
                                "role": "reel_cover_9x16",
                                "relative_path": reel_cover_9x16_rel,
                                "sha256": _sha256(out_9x16),
                            }
                        )

                video_candidate = None
                if post.reel_video_path:
                    vp = Path(post.reel_video_path)
                    video_candidate = vp if vp.is_absolute() else (ws_root / vp).resolve()
                if video_candidate is not None and video_candidate.is_file():
                    ext = video_candidate.suffix.lower() or ".mp4"
                    out_video = item_dir / f"video{ext}"
                    shutil.copyfile(video_candidate, out_video)
                    reel_video_rel = out_video.relative_to(stage).as_posix()
                    exported_media.append(
                        {
                            "role": "reel_video",
                            "relative_path": reel_video_rel,
                            "sha256": _sha256(out_video),
                        }
                    )
                    reel_note = "video_attached"
                else:
                    export_status = "draft_awaiting_reel_video"
                    reel_note = "awaiting_real_reel_video_file"
                    (item_dir / "REEL_VIDEO_PENDING.txt").write_text(
                        (
                            f"DRAFT STATUS: Reel '{post.id}' ({post.editorial_title})\n"
                            f"Motion preset: {post.reel_motion_preset}\n"
                            "Real Reel video file (.mp4/.mov) is not rendered or attached yet.\n"
                            "Both 4:5 profile cover (cover_grid_4x5.jpg) and 9:16 Reel cover (cover_reel_9x16.jpg) are ready.\n"
                            "Run `review_monthly_plan.py render-reels` or attach an external MP4 before uploading to Meta Business Suite.\n"
                        ),
                        encoding="utf-8",
                    )

                preset_meta = REEL_MOTION_PRESETS.get(post.reel_motion_preset, {})
                storyboard = {
                    "post_id": post.id,
                    "motion_preset": post.reel_motion_preset,
                    "preset_label": preset_meta.get("label", ""),
                    "target_duration_sec": preset_meta.get("duration_sec", 9),
                    "beats": preset_meta.get("beats", ""),
                    "reel_dimensions": [REEL_W, REEL_H],
                    "profile_grid_crop_y": [GRID_TOP_OFFSET, GRID_TOP_OFFSET + GRID_H],
                    "reel_video_status": reel_note,
                }
                (item_dir / "reel_storyboard.json").write_text(
                    json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            if export_status == "ready_for_meta":
                ready_count += 1
            else:
                draft_reel_count += 1

            caption_file = item_dir / "caption_en.txt"
            caption_file.write_text(post.caption_en.strip() + "\n", encoding="utf-8")
            first_comment_file = item_dir / "first_comment_en.txt"
            first_comment_text = getattr(post, "first_comment_en", "") or ""
            if not first_comment_text:
                from src.editorial_copy_rules import compose_first_comment
                first_comment_text = compose_first_comment([a.model_dump(mode="json") for a in post.artworks])
            first_comment_file.write_text(first_comment_text.strip() + "\n", encoding="utf-8")

            alt_text_file = item_dir / "alt_text_en.txt"
            alt_text_val = getattr(post, "alt_text_en", "") or ""
            if not alt_text_val:
                from src.editorial_copy_rules import compose_seo_alt_text
                alt_text_val = compose_seo_alt_text(
                    [a.model_dump(mode="json") for a in post.artworks],
                    post_format=post.post_format,
                    tone_band=getattr(post, "tone_band", "mid"),
                )
            alt_text_file.write_text(alt_text_val.strip() + "\n", encoding="utf-8")

            post_info = {
                "export_index": export_idx,
                "post_id": post.id,
                "post_format": post.post_format,
                "export_status": export_status,
                "design_id": post.design_id,
                "editorial_title": post.editorial_title,
                "anniversary_note": getattr(post, "anniversary_note", ""),
                "alt_text_file": alt_text_file.relative_to(stage).as_posix(),
                "alt_text_en": alt_text_val.strip(),
                "scheduled_at_istanbul": ist_label,
                "scheduled_at_utc": utc_label,
                "pinned": post.id in state.pinned_ids,
                "slide_count": len(post.slides),
                "reel_motion_preset": post.reel_motion_preset if post.post_format == "reel" else "",
                "reel_video_status": reel_note or "not_applicable",
                "reel_cover_grid_4x5": reel_cover_rel,
                "reel_cover_9x16": reel_cover_9x16_rel,
                "reel_video": reel_video_rel,
                "editorial_project_dir": post.editorial_project_dir,
                "artworks": [art.model_dump(mode="json") for art in post.artworks],
                "media_files": exported_media,
                "caption_file": caption_file.relative_to(stage).as_posix(),
                "caption_en": post.caption_en,
            }
            (item_dir / "post_info.json").write_text(
                json.dumps(post_info, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            exported_rows.append(post_info)

        csv_path = stage / "meta_schedule.csv"
        fieldnames = [
            "export_index",
            "post_id",
            "scheduled_at_istanbul",
            "scheduled_at_utc",
            "post_format",
            "export_status",
            "pinned",
            "design_id",
            "editorial_title",
            "slide_count",
            "reel_motion_preset",
            "media_files",
            "reel_cover_9x16",
            "reel_cover_grid_4x5",
            "reel_video_status",
            "museum_urls",
            "caption_en",
        ]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in exported_rows:
                writer.writerow(
                    {
                        "export_index": row["export_index"],
                        "post_id": row["post_id"],
                        "scheduled_at_istanbul": row["scheduled_at_istanbul"],
                        "scheduled_at_utc": row["scheduled_at_utc"],
                        "post_format": row["post_format"],
                        "export_status": row["export_status"],
                        "pinned": "YES" if row["pinned"] else "NO",
                        "design_id": row["design_id"],
                        "editorial_title": row["editorial_title"],
                        "slide_count": row["slide_count"],
                        "reel_motion_preset": row["reel_motion_preset"],
                        "media_files": " | ".join(m["relative_path"] for m in row["media_files"]),
                        "reel_cover_9x16": row["reel_cover_9x16"],
                        "reel_cover_grid_4x5": row["reel_cover_grid_4x5"],
                        "reel_video_status": row["reel_video_status"],
                        "museum_urls": " | ".join(
                            a["museum_url"] for a in row["artworks"] if a.get("museum_url")
                        ),
                        "caption_en": row["caption_en"],
                    }
                )

        md_lines = [
            f"# Artfolio Meta Business Suite Yerel Paketi ({state.profile_handle})",
            "",
            f"- **Dışa aktarım zamanı (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
            f"- **Tempo:** Günde {state.carousels_per_day + state.reels_per_day} gönderi ({state.carousels_per_day} Kaydırmalı + {state.reels_per_day} Reels, 0 Tekli)",
            f"- **Plan süresi:** {state.days} gün",
            f"- **Toplam onaylı dışa aktarılan:** {len(exported_rows)}",
            f"- **Yüklemeye tam hazır:** {ready_count}",
            f"- **Gerçek Reels videosu bekleyen taslak:** {draft_reel_count}",
            f"- **Onaylanmadığı için atlanan:** {len(skipped_unapproved)}",
            "- **Not:** Yalnızca yerel hazırlıktır; canlı Instagram/Meta API'sine otomatik gönderim yapmaz.",
            "",
            "## Planlanan Yayın Listesi (Meta Business Suite Sırası)",
            "",
            "| # | Önerilen Zaman (Türkiye) | Tür | Durum | Tasarım | Slayt/Preset | Başlık | Klasör |",
            "| :-: | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for row in exported_rows:
            status_badge = (
                "Hazır"
                if row["export_status"] == "ready_for_meta"
                else "TASLAK (Reels videosu bekleniyor)"
            )
            detail_col = (
                f"{row['slide_count']} slayt"
                if row["post_format"] == "carousel"
                else (row["reel_motion_preset"] or "reel")
            )
            folder = Path(row["caption_file"]).parent.as_posix()
            md_lines.append(
                f"| {row['export_index']:02d} | {row['scheduled_at_istanbul']} | "
                f"{row['post_format']} | {status_badge} | `{row['design_id']}` | "
                f"{detail_col} | {row['editorial_title']} | `{folder}` |"
            )
        (stage / "README_META_BUSINESS_SUITE.md").write_text(
            "\n".join(md_lines) + "\n", encoding="utf-8"
        )

        manifest = {
            "schema_version": "artfolio-meta-business-suite-export-v2",
            "profile_handle": state.profile_handle,
            "publication_ready": False,
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "days": state.days,
            "carousels_per_day": state.carousels_per_day,
            "reels_per_day": state.reels_per_day,
            "exported_count": len(exported_rows),
            "ready_count": ready_count,
            "draft_reels_awaiting_video": draft_reel_count,
            "skipped_unapproved_ids": skipped_unapproved,
            "pinned_ids": list(state.pinned_ids),
            "posts": exported_rows,
        }
        (stage / "export_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stage.rename(destination)

    if reuse_ledger_path is not None and exported_artwork_keys:
        from src.museum_pool import ArtworkReuseLedger
        ArtworkReuseLedger(reuse_ledger_path).mark_used(exported_artwork_keys)

    return {
        "export_dir": str(destination),
        "split_weeks": split_weeks,
        "exported_count": len(exported_rows),
        "ready_count": ready_count,
        "draft_reels_awaiting_video": draft_reel_count,
        "skipped_unapproved_count": len(skipped_unapproved),
        "manifest_path": str(destination / "export_manifest.json"),
        "csv_path": str(destination / "meta_schedule.csv"),
        "readme_path": str(destination / "README_META_BUSINESS_SUITE.md"),
    }
