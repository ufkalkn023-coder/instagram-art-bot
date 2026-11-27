"""Build, serve, batch-render Reels (parallel), analyze Insights CSV, or export Meta Business Suite packages."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.astra_catalog import list_usable_designs, load_design_catalog
from src.insights_feedback import analyze_instagram_insights_csv
from src.meta_business_export import export_meta_business_suite_package
from src.monthly_planner import (
    build_monthly_post_specs,
    create_monthly_plan_workspace,
    load_monthly_plan_state,
    monthly_plan_server,
    render_monthly_reel_video,
    update_monthly_approval,
)

DEFAULT_ASSETS_DIR = Path.home() / "Downloads" / "ARTFOLIO_ASTRA" / "assets"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list-designs", help="List all registered & ready Astra+Neon designs")
    list_cmd.add_argument("--collection", choices=["all", "astra", "neon"], default="all")

    create_cmd = sub.add_parser("create", help="Build a Carousel + Reels ONLY plan (default 4 Carousels + 2 Reels/day)")
    create_cmd.add_argument("--output", type=Path, required=True)
    create_cmd.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    create_cmd.add_argument("--days", type=int, default=30)
    create_cmd.add_argument("--carousels-per-day", type=int, default=4)
    create_cmd.add_argument("--reels-per-day", type=int, default=2)
    create_cmd.add_argument("--project-dir", action="append", type=Path, default=[])
    create_cmd.add_argument("--pin", action="append", default=[])
    create_cmd.add_argument("--reuse-ledger", type=Path, default=None)

    reel_cmd = sub.add_parser("render-reels", help="Batch-render real 1080x1920 H.264 MP4 Reels via parallel FFmpeg")
    reel_cmd.add_argument("--workspace", type=Path, required=True)
    reel_cmd.add_argument("--post-id", help="Render a specific Reel post ID")
    reel_cmd.add_argument("--approved-only", action="store_true")
    reel_cmd.add_argument("--limit", type=int, default=0, help="Max number of Reels to render (0 = all matching)")
    reel_cmd.add_argument("--workers", type=int, default=2, help="Parallel FFmpeg workers (default 2)")

    serve_cmd = sub.add_parser("serve", help="Serve the local Carousel+Reels studio")
    serve_cmd.add_argument("--workspace", type=Path, required=True)
    serve_cmd.add_argument("--port", type=int, default=38150)

    exp_cmd = sub.add_parser("export-meta", help="Export approved posts as a Meta Business Suite local package")
    exp_cmd.add_argument("--workspace", type=Path, required=True)
    exp_cmd.add_argument("--output", type=Path, required=True)
    exp_cmd.add_argument("--approve-all", action="store_true")
    exp_cmd.add_argument("--split-weeks", action="store_true", help="Organize posts into Week-01..Week-04 folders")
    exp_cmd.add_argument("--reuse-ledger", type=Path, default=None, help="Record exported artworks in 180-day reuse ledger")
    exp_cmd.add_argument("--overwrite", action="store_true")

    ins_cmd = sub.add_parser("analyze-insights", help="Analyze an exported Instagram Insights CSV into a design/format scorecard")
    ins_cmd.add_argument("--csv", type=Path, required=True)
    ins_cmd.add_argument("--out-json", type=Path, default=None)

    car_cmd = sub.add_parser(
        "build-carousel-delivery",
        help="Build portable weekly Carousel delivery package (auto-fetch pool top-up, 2-tier pairing, exact slide numbering)",
    )
    car_cmd.add_argument("--pool", type=Path, required=True, help="Verified museum pool_index.json path")
    car_cmd.add_argument("--output", type=Path, required=True, help="Destination directory for portable weekly package")
    car_cmd.add_argument("--from-rehearsal", type=Path, default=None, help="Optional existing v2 rehearsal dir to reuse unaffected covers")
    car_cmd.add_argument("--days", type=int, default=None, help="Override days (defaults to calendar days of --month-id, e.g. 31 for 2026-12)")
    car_cmd.add_argument("--carousels-per-day", type=int, default=4)
    car_cmd.add_argument("--reels-per-day", type=int, default=2)
    car_cmd.add_argument("--month-id", type=str, default=None, help="Stable month/plan ID (e.g. 2026-12); defaults to output folder name")
    car_cmd.add_argument(
        "--status",
        choices=["preview", "reserved", "published"],
        default="reserved",
        help="Reuse ledger lifecycle state: preview (non-blocking), reserved (default), or published",
    )
    car_cmd.add_argument(
        "--auto-fetch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically top up pool_index.json from working museum adapters when eligible Carousels fall short (default: enabled)",
    )
    car_cmd.add_argument(
        "--reuse-ledger",
        type=Path,
        default=None,
        help="180-day ArtworkReuseLedger JSON (defaults to <pool_dir>/carousel_reuse_ledger.json)",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "list-designs":
            cat = load_design_catalog()
            col = None if args.collection == "all" else args.collection
            ready = list_usable_designs(collection=col)
            print(
                f"Registered total: {len(cat.designs)} "
                f"(Astra: {sum(d.collection == 'astra' for d in cat.designs)}, "
                f"Neon: {sum(d.collection == 'neon' for d in cat.designs)}) | "
                f"Ready ({args.collection}): {len(ready)}"
            )
            return 0

        if args.command == "analyze-insights":
            scorecard = analyze_instagram_insights_csv(args.csv)
            if args.out_json:
                args.out_json.parent.mkdir(parents=True, exist_ok=True)
                args.out_json.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(scorecard, ensure_ascii=False, indent=2))
            return 0

        if args.command == "build-carousel-delivery":
            import tempfile
            from src.carousel_delivery import build_weekly_carousel_delivery
            from src.monthly_planner import resolve_month_schedule_window
            from src.museum_pool import (
                ArtworkReuseLedger,
                calculate_carousel_only_artwork_need,
                ensure_carousel_pool_supply,
            )
            from src.profile_rehearsal import build_real_museum_rehearsal

            ledger_path = (args.reuse_ledger or (args.pool.resolve().parent / "carousel_reuse_ledger.json")).resolve()
            month_id = args.month_id or args.output.resolve().name
            start_dt, resolved_days = resolve_month_schedule_window(month_id, explicit_days=args.days)
            supply_report = None

            if args.from_rehearsal is not None:
                res = build_weekly_carousel_delivery(
                    args.from_rehearsal,
                    args.pool,
                    args.output,
                    reuse_ledger_path=ledger_path,
                    plan_id=month_id,
                    ledger_status=args.status,
                    requested_days=resolved_days,
                    carousels_per_day=args.carousels_per_day,
                )
            else:
                need = calculate_carousel_only_artwork_need(
                    days=resolved_days,
                    carousels_per_day=args.carousels_per_day,
                    reels_per_day=args.reels_per_day,
                )
                if args.auto_fetch:
                    supply_report = ensure_carousel_pool_supply(
                        args.pool,
                        needed_fresh_artworks=need["carousel_gross_artwork_slots"],
                        reuse_ledger=ArtworkReuseLedger(ledger_path),
                        current_plan_id=month_id,
                    )
                pool_arts = json.loads(args.pool.read_text(encoding="utf-8"))["artworks"]
                with tempfile.TemporaryDirectory(prefix="artfolio-rehearsal-stage-") as stage:
                    build_real_museum_rehearsal(
                        pool_arts,
                        stage,
                        days=resolved_days,
                        carousels_per_day=args.carousels_per_day,
                        reels_per_day=args.reels_per_day,
                        start_at=start_dt,
                        render_sample_mp4_count=0,
                        reuse_ledger_path=ledger_path,
                        plan_id=month_id,
                        ledger_status=args.status,
                        record_ledger=False,
                        carousel_only_need=True,
                    )
                    res = build_weekly_carousel_delivery(
                        stage,
                        args.pool,
                        args.output,
                        reuse_ledger_path=ledger_path,
                        plan_id=month_id,
                        ledger_status=args.status,
                        supply_report=supply_report,
                        requested_days=resolved_days,
                        carousels_per_day=args.carousels_per_day,
                    )
            s = res["summary"]
            sup = s.get("auto_supply_report") or {}
            reel_c = s.get("reel_preview_consumption") or {}
            print(
                f"Carousel delivery ready: {res['html_path']} "
                f"(month={s.get('plan_id')}, status={s.get('ledger_status')}, "
                f"shareable={s['shareable_carousels']}/{s['expected_carousels']}, "
                f"carousel_unique_works={s.get('actual_unique_carousel_artworks_used')}, "
                f"reel_preview_covers={reel_c.get('reel_covers_total', 0)}, "
                f"auto_added={sup.get('added_total', 0)} {sup.get('added_by_museum', {})}, "
                f"rejected={sup.get('rejected_total', 0)}, "
                f"missing_days={len(s.get('missing_carousel_days', []))}, "
                f"ledger={s.get('reuse_ledger_path')}, "
                f"preflight_errors={s['preflight']['error_count']})"
            )
            return 0

        if args.command == "create":
            posts, history, proj_slides = build_monthly_post_specs(
                args.assets_dir,
                days=args.days,
                carousels_per_day=args.carousels_per_day,
                reels_per_day=args.reels_per_day,
                editorial_projects=args.project_dir,
                reuse_ledger_path=args.reuse_ledger,
            )
            pins = tuple(args.pin) if args.pin else tuple(p["id"] for p in posts[:2])
            create_monthly_plan_workspace(
                args.output,
                posts,
                days=args.days,
                carousels_per_day=args.carousels_per_day,
                reels_per_day=args.reels_per_day,
                history_specs=history,
                pinned_ids=pins,
                project_slides_map=proj_slides,
            )
            c_cnt = sum(p["post_format"] == "carousel" for p in posts)
            r_cnt = sum(p["post_format"] == "reel" for p in posts)
            print(
                f"Artfolio studio ready: {args.output.resolve()} "
                f"({args.days} days · {len(posts)} posts: {c_cnt} carousels + {r_cnt} reels, 0 singles)"
            )
            print(f"Launch editor: python3 scripts/review_monthly_plan.py serve --workspace {args.output.resolve()}")
            return 0

        if args.command == "render-reels":
            state = load_monthly_plan_state(args.workspace)
            post_map = {p.id: p for p in state.posts}
            targets = [
                post_map[pid]
                for pid in state.schedule_order
                if post_map[pid].post_format == "reel"
                and (not args.post_id or pid == args.post_id)
                and (not args.approved_only or post_map[pid].approval_status == "approved")
            ]
            if args.limit > 0:
                targets = targets[: args.limit]
            workers = max(1, min(4, int(args.workers)))
            if workers == 1 or len(targets) <= 1:
                for post in targets:
                    _, info = render_monthly_reel_video(args.workspace, post.id)
                    print(
                        f"Rendered Reel {post.id} ({info['preset']}): "
                        f"{info.get('width')}x{info.get('height')} {info.get('duration_sec')}s -> {info['path']}"
                    )
            else:
                from src.monthly_planner import _sources_from_post, save_monthly_plan_state
                from src.reel_studio import render_local_reel_mp4

                root = Path(args.workspace).resolve()
                results_by_id: dict[str, tuple[str, dict]] = {}

                def _job(post):
                    sources = _sources_from_post(root, post)
                    rel_video = f"videos/{post.id}-{post.reel_motion_preset}.mp4"
                    info = render_local_reel_mp4(
                        sources[0],
                        post.editorial_title,
                        root / rel_video,
                        preset=post.reel_motion_preset,
                        zoom_start=post.reel_zoom_start,
                        zoom_end=post.reel_zoom_end,
                        seamless_loop=True,
                    )
                    return post.id, rel_video, info

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(_job, p) for p in targets]
                    for fut in as_completed(futures):
                        pid, rel_v, info = fut.result()
                        results_by_id[pid] = (rel_v, info)
                        print(
                            f"Rendered Reel {pid} ({info['preset']}): "
                            f"{info.get('width')}x{info.get('height')} {info.get('duration_sec')}s -> {info['path']}"
                        )
                updated_posts = tuple(
                    p.model_copy(update={"reel_video_path": results_by_id[p.id][0], "reel_status": "video_ready"})
                    if p.id in results_by_id
                    else p
                    for p in state.posts
                )
                save_monthly_plan_state(root, state.model_copy(update={"posts": updated_posts}))
            print(f"Total Reels rendered: {len(targets)} (workers={workers})")
            return 0

        if args.command == "export-meta":
            if args.approve_all:
                update_monthly_approval(args.workspace, approval_status="approved", approve_all=True)
            report = export_meta_business_suite_package(
                args.workspace,
                args.output,
                overwrite=args.overwrite,
                split_weeks=args.split_weeks,
                reuse_ledger_path=args.reuse_ledger,
            )
            print(
                f"Meta Business Suite package exported: {report['export_dir']} "
                f"(exported={report['exported_count']}, ready={report['ready_count']}, "
                f"reel_drafts_awaiting_video={report['draft_reels_awaiting_video']}, "
                f"split_weeks={report['split_weeks']}, skipped_unapproved={report['skipped_unapproved_count']})"
            )
            return 0

        server = monthly_plan_server(args.workspace, port=args.port)
        print(f"Artfolio Carousel+Reels Studio: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print(f"Monthly planner error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
