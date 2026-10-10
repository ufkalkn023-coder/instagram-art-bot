#!/usr/bin/env python3
"""Write an offline HTML snapshot of Artfolio Feed state."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.feed_dashboard import render_feed_dashboard  # noqa: E402
from src.feed_queue import PreparedFeedQueue  # noqa: E402
from src.feed_schedule import FeedScheduleManager  # noqa: E402
from src.insights_storage import InsightsStorage, parse_aware_timestamp  # noqa: E402
from src.r2_feed_queue import R2PreparedFeedQueue  # noqa: E402

MAX_INPUT_BYTES = 16_000_000
ALLOWED_READS = frozenset({"get_object", "head_object", "list_objects_v2"})


class ReadOnlyClient:
    """Refuse every storage method except the dashboard's list of reads."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        if name not in ALLOWED_READS:
            raise RuntimeError("Dashboard storage access is read-only")
        return getattr(self._client, name)


def _read_json(path: Path):
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input exceeds byte limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    return value


def _story_summary(directory: Path) -> dict:
    from src.story_feed import prepare_story_content
    from src.story_project import load_project

    project = load_project(directory)
    content = prepare_story_content(directory)
    delivery = content.publication_metadata["story_delivery"]
    return {"title": project.plan.public_title, "approved": True,
            "source_count": len(delivery["source_ids"]), "page_count": len(delivery["pages"])}


def _queue_details(packages):
    rows = []
    for package in packages:
        content = package.get("content", {})
        story = content.get("story_delivery", {})
        sources = story.get("source_ids") or content.get("source_ids")
        if not isinstance(sources, list):
            artworks = content.get("artworks", [])
            sources = [item.get("id") for item in artworks if isinstance(item, dict) and item.get("id")]
        assets = content.get("assets")
        caption = content.get("caption", "")
        rows.append({"id": package.get("id"), "state": package.get("state"),
                     "publication_format": content.get("publication_format"),
                     "expires_at": package.get("expires_at"), "reason": package.get("reason"),
                     "title": story.get("public_title") or (caption.splitlines()[0] if isinstance(caption, str) and caption else ""),
                     "source_count": len(sources) if isinstance(sources, list) else None,
                     "page_count": len(assets) if isinstance(assets, list) else None})
    return rows


def _queue_report_rows(document):
    if isinstance(document, dict):
        document = document.get("packages", document.get("queue"))
    if not isinstance(document, list) or len(document) > 500:
        raise ValueError("Queue report must be a bounded package array")
    allowed_states = {"READY", "CLAIMED", "CONSUMED", "QUARANTINED"}
    allowed_formats = {"single", "carousel"}
    rows = []
    for row in document:
        if (not isinstance(row, dict) or row.get("state") not in allowed_states
                or row.get("publication_format") not in allowed_formats
                or not isinstance(row.get("id"), str) or not row["id"]):
            raise ValueError("Queue report contains an invalid package summary")
        source_ids = row.get("source_ids")
        count = len(source_ids) if isinstance(source_ids, list) else row.get("source_count")
        page_count = row.get("page_count")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("Queue report source count is invalid")
        if page_count is not None and (type(page_count) is not int or page_count < 0):
            raise ValueError("Queue report page count is invalid")
        rows.append({"id": row["id"], "state": row["state"],
                     "publication_format": row["publication_format"],
                     "expires_at": row.get("expires_at"), "source_count": count,
                     "page_count": page_count,
                     "title": row.get("title") or row.get("public_title") or ""})
    return rows


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r2", action="store_true", help="Read the existing caller-configured R2 state")
    parser.add_argument("--expected-sha", help="Expected workflow SHA for schedule readiness")
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--queue-report", type=Path, help="Use an existing bounded queue status snapshot")
    parser.add_argument("--story-project", type=Path, action="append", default=[])
    parser.add_argument("--now", help="Timezone-aware report time")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.queue and args.queue_report:
        parser.error("Select one local queue source")
    if args.r2 and args.expected_sha is None:
        parser.error("R2 mode requires explicit --expected-sha")
    local_inputs = (args.schedule, args.history, args.snapshots, args.queue, args.queue_report)
    if args.r2 and any(local_inputs):
        parser.error("Select R2 inputs or local files")
    if not args.r2 and not all((args.schedule, args.history, args.snapshots)):
        parser.error("Local mode requires --schedule, --history and --snapshots")
    timestamp = parse_aware_timestamp(args.now) if args.now else None
    if args.now and timestamp is None:
        parser.error("--now must include a valid timezone")
    timestamp = timestamp or datetime.now().astimezone()

    try:
        if args.r2:
            schedule_manager = FeedScheduleManager()
            schedule_manager.store.client = ReadOnlyClient(schedule_manager.store.client)
            from src import r2_media
            media_client_factory = r2_media._get_s3_client
            with patch.object(r2_media, "_get_s3_client",
                              side_effect=lambda config: ReadOnlyClient(media_client_factory(config))):
                schedule = schedule_manager.status(expected_sha=args.expected_sha, now=timestamp)
            from src import publication_state
            safety_state = schedule_manager.store.load_safety()[0]
            if (schedule.get("generation") is not None
                    and schedule["generation"] != safety_state.generation):
                raise RuntimeError("Schedule and publication-state snapshots changed during read")
            history = publication_state.history_view(safety_state)
            media_configuration = r2_media._load_configuration(require_public_url=False)
            read_client = ReadOnlyClient(r2_media._get_s3_client(media_configuration))
            insights = InsightsStorage(read_client, media_configuration.bucket_name)
            snapshots = insights.load_all_snapshots()
            with tempfile.TemporaryDirectory(prefix="artfolio-feed-dashboard-") as directory:
                queue_store = R2PreparedFeedQueue(directory)
                queue_store.client = ReadOnlyClient(queue_store.client)
                document = queue_store._read_manifest()[0]
                queue = _queue_details(document["packages"])
        else:
            schedule = _read_json(args.schedule)
            history = _read_json(args.history)
            snapshots = _read_json(args.snapshots)
            if args.queue_report:
                queue = _queue_report_rows(_read_json(args.queue_report))
            elif args.queue:
                queue_store = PreparedFeedQueue(args.queue)
                document = queue_store._load()
                queue = _queue_details(document["packages"])
            else:
                queue = []

        stories = []
        for project_dir in args.story_project:
            try:
                stories.append(_story_summary(project_dir))
            except Exception as error:
                stories.append({"title": project_dir.name, "approved": False,
                                "error": type(error).__name__})
        page = render_feed_dashboard(schedule, queue, history, snapshots,
                                     now=timestamp, stories=stories)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(page)
            output.write("\n")
    except Exception as error:
        print(f"Feed dashboard failed ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
