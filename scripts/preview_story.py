"""Create portable museum stories or edit one on localhost; no live AI/publishing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preview_editorial_v2 import load_detail_focus, load_sources
from src.story_editor import editor_server
from src.story_plan import build_story_plan
from src.story_project import approve_story, create_project, load_project, update_project
from src.story_feed import export_story_content


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--manifest", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument(
        "--narrative",
        choices=("single_study", "comparison", "thematic_selection"),
        required=True,
    )
    create.add_argument(
        "--artwork-ids", nargs="+", help="Canonical source identities in story order"
    )
    create.add_argument("--detail-focus", type=Path)
    create.add_argument(
        "--history", type=Path, help="JSON list of previous public titles"
    )
    create.add_argument(
        "--style-history",
        nargs="*",
        choices=("museum_journal", "artwork_first", "detail_study"),
    )
    render = commands.add_parser("render")
    render.add_argument("--project", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--project", type=Path, required=True)
    serve.add_argument("--port", type=int, default=38129)
    approve = commands.add_parser("approve")
    approve.add_argument("--project", type=Path, required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    export = commands.add_parser("export")
    export.add_argument("--project", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "serve":
        server = editor_server(args.project, port=args.port)
        print(f"ARTFOLIO editor: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    if args.command == "render":
        project = load_project(args.project)
        report = update_project(
            args.project,
            project.plan.model_dump(mode="json"),
            expected_revision=project.revision,
        )
    elif args.command == "approve":
        report = approve_story(args.project, expected_revision=args.expected_revision)
    elif args.command == "export":
        report = export_story_content(args.project, args.output)
    else:
        if args.manifest.stat().st_size > 2_000_000:
            parser.error("Manifest exceeds byte limit")
        count = len(json.loads(args.manifest.read_text()).get("artworks", []))
        sources = load_sources(args.manifest, count=count)
        registry = {s.artwork.canonical_id: s for s in sources}
        ids = (
            args.artwork_ids
            or list(registry)[
                : {"single_study": 1, "comparison": 2, "thematic_selection": 5}[
                    args.narrative
                ]
            ]
        )
        if len(ids) != len(set(ids)) or set(ids) - set(registry):
            parser.error(
                "Artwork identities must be unique and present in the manifest"
            )
        selected = [registry[i] for i in ids]
        focus = load_detail_focus(args.detail_focus) if args.detail_focus else {}
        if set(focus) - set(registry):
            parser.error("Detail focus refers to an unknown artwork")
        plan = build_story_plan(
            selected,
            args.narrative,
            detail_focus={i: [focus[i]] for i in ids if i in focus},
            style_history=args.style_history,
        )
        history = []
        if args.history:
            if args.history.stat().st_size > 100_000:
                parser.error("History exceeds byte limit")
            history = json.loads(args.history.read_text())
            if (
                not isinstance(history, list)
                or len(history) > 100
                or any(not isinstance(t, str) for t in history)
            ):
                parser.error("History must be a list of at most 100 titles")
        report = create_project(args.output, selected, plan, headline_history=history)
    if args.command == "export":
        print(json.dumps({"content_kind": report["content_kind"], "output": str(args.output)}, indent=2))
    elif args.command == "approve":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: report[k] for k in ("revision", "rendered_count", "reused_count", "quality")},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        raise SystemExit(f"Story command failed ({type(error).__name__}): {error}") from None
