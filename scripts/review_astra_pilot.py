"""Build or serve the local Artfolio Astra 3x3 profile preview pilot (never publishes)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.astra_catalog import list_usable_designs, load_design_catalog
from src.astra_pilot_workspace import (
    astra_pilot_server,
    build_default_nine_draft_specs,
    create_astra_pilot_workspace,
)

DEFAULT_ASSETS_DIR = Path.home() / "Downloads" / "ARTFOLIO_ASTRA" / "assets"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create_cmd = sub.add_parser("create", help="Build a local 9-draft 3x3 pilot workspace")
    create_cmd.add_argument("--output", type=Path, required=True)
    create_cmd.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    create_cmd.add_argument("--pin", action="append", default=[], help="Draft ID to pin (up to 3)")

    serve_cmd = sub.add_parser("serve", help="Serve the local 3x3 profile preview editor")
    serve_cmd.add_argument("--workspace", type=Path, required=True)
    serve_cmd.add_argument("--port", type=int, default=38145)

    sub.add_parser("list-designs", help="List registered and ready Astra designs")

    args = parser.parse_args(argv)
    try:
        if args.command == "list-designs":
            catalog = load_design_catalog()
            ready = list_usable_designs()
            print(f"Registered designs: {len(catalog.designs)} | Ready pilot templates: {len(ready)}")
            for item in ready:
                print(
                    f"  - {item.id}: {item.name} "
                    f"({item.family_name}, {item.required_sources} source(s))"
                )
            return 0

        if args.command == "create":
            drafts, history = build_default_nine_draft_specs(args.assets_dir)
            pins = tuple(args.pin) if args.pin else ("draft-01", "draft-05")
            create_astra_pilot_workspace(
                args.output,
                drafts,
                history_specs=history,
                pinned_ids=pins,
            )
            print(f"Astra pilot workspace ready: {args.output.resolve()}")
            print(f"Launch editor: python3 scripts/review_astra_pilot.py serve --workspace {args.output.resolve()}")
            return 0

        server = astra_pilot_server(args.workspace, port=args.port)
        print(f"Artfolio Astra 3x3 editor: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"Astra pilot error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
