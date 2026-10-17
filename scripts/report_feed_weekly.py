#!/usr/bin/env python3
"""Create exclusive Markdown/JSON weekly reports from guarded stored inputs."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.report_feed_dashboard import load_dashboard_inputs, parse_dashboard_args  # noqa: E402
from src.feed_weekly_report import build_weekly_report, render_weekly_markdown  # noqa: E402


def run(argv=None) -> int:
    args, now = parse_dashboard_args(argv)
    try:
        markdown_path, json_path = args.output, args.output.with_suffix('.json')
        if markdown_path == json_path or markdown_path.exists() or json_path.exists():
            raise FileExistsError('Use new Markdown and JSON output paths')
        schedule, queue, history, snapshots, _ = load_dashboard_inputs(args, now=now, include_thumbnails=False)
        report = build_weekly_report(schedule, queue, history, snapshots, now=now)
        markdown = render_weekly_markdown(report)
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if max(len(markdown.encode()), len(encoded.encode())) > 16_000_000:
            raise ValueError('Weekly report exceeds output bound')
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        with markdown_path.open('x', encoding='utf-8') as destination:
            destination.write(markdown)
        with json_path.open('x', encoding='utf-8') as destination:
            destination.write(encoded + '\n')
        return 0
    except Exception as error:
        print(f'Weekly report failed ({type(error).__name__})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
