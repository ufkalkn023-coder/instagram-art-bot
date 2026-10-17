#!/usr/bin/env python3
"""Serve the read-only Feed panel locally with manual refresh."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.report_feed_dashboard import load_dashboard_inputs, parse_dashboard_args  # noqa: E402
from src.feed_dashboard import render_feed_dashboard  # noqa: E402
from src.feed_dashboard_server import dashboard_server  # noqa: E402


def run(argv=None) -> int:
    args, fixed_time = parse_dashboard_args(argv, serving=True)

    def load_page():
        now = fixed_time if args.now else datetime.now().astimezone()
        schedule, queue, history, snapshots, stories = load_dashboard_inputs(args, now=now)
        return render_feed_dashboard(schedule, queue, history, snapshots, now=now, stories=stories)

    try:
        with dashboard_server(load_page, port=args.port) as server:
            print(f'ARTFOLIO dashboard: http://127.0.0.1:{server.server_port}/', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except Exception as error:
        print(f'Feed dashboard failed ({type(error).__name__})', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
