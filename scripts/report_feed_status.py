#!/usr/bin/env python3
"""Read-only combined Feed status; --notify explicitly writes a separate dedup key."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.feed_operations import FeedStatusNotifications, build_feed_operations  # noqa: E402
from src.feed_queue import PreparedFeedQueue  # noqa: E402
from src.feed_schedule import FeedScheduleManager  # noqa: E402
from src.history_tracker import load_history_with_etag  # noqa: E402
from src.insights_storage import InsightsStorage, parse_aware_timestamp  # noqa: E402
from src.r2_feed_queue import R2PreparedFeedQueue  # noqa: E402


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r2", action="store_true", help="Read schedule, private queue and analytics using environment credentials")
    parser.add_argument("--notify", action="store_true", help="Explicitly deduplicate changes in private R2 and append actionable events to Actions summary")
    parser.add_argument("--expected-sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    if args.notify and not args.r2:
        parser.error("--notify requires explicit --r2 mode")
    if args.r2 and any((args.schedule, args.history, args.snapshots, args.queue)):
        parser.error("Select R2 inputs or local files")
    if not args.r2 and not all((args.schedule, args.history, args.snapshots)):
        parser.error("Local mode requires --schedule, --history and --snapshots")
    timestamp = parse_aware_timestamp(args.now) if args.now else None
    if args.now and timestamp is None:
        parser.error("--now must have a timezone")
    try:
        with tempfile.TemporaryDirectory(prefix="artfolio-feed-status-") as directory:
            if args.r2:
                schedule = FeedScheduleManager().status(expected_sha=args.expected_sha, now=timestamp)
                history, _ = load_history_with_etag()
                snapshots = InsightsStorage().load_all_snapshots()
                queue_store = R2PreparedFeedQueue(directory)
                queue = queue_store.status()
            else:
                schedule = json.loads(args.schedule.read_text())
                history = json.loads(args.history.read_text())
                snapshots = json.loads(args.snapshots.read_text())
                queue = PreparedFeedQueue(args.queue).status() if args.queue else []
            report = build_feed_operations(schedule, queue, history, snapshots, now=timestamp)
            if args.notify:
                event = FeedStatusNotifications(queue_store).observe(report, now=timestamp)
                report["notification"] = event
                summary = os.environ.get("GITHUB_STEP_SUMMARY")
                if event is not None and summary:
                    with Path(summary).open("a", encoding="utf-8") as output:
                        output.write("### Feed status change\n\n```json\n" + json.dumps(event, sort_keys=True) + "\n```\n")
            print(json.dumps(report, indent=2, sort_keys=True))
    except Exception as error:
        print(f"Feed status failed ({type(error).__name__}); no publication requested", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
