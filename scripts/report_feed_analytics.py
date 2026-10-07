#!/usr/bin/env python3
"""Read-only Feed coverage and format report; no Instagram API requests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from scripts.audit_engagement_learning import _load_inputs  # noqa: E402
from src.feed_analytics import build_feed_analytics_report  # noqa: E402
from src.insights_storage import InsightsStorageError, parse_aware_timestamp  # noqa: E402
from src.local_credentials import ENGAGEMENT_AUDIT_PROFILE, credential_variables, load_keychain_credentials  # noqa: E402


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = ["# Feed analytics report", "", f"Generated at: {report['generated_at']}", "",
             f"Feed publications: {summary['feed_publications']}; complete windows: {summary['complete_windows']}; "
             f"missed windows: {summary['missed_windows']} across {summary['publications_with_missed_windows']} publications.",
             "", "Missed windows are measurement gaps, not missed publications. Expired windows cannot be reconstructed from current Insights.",
             "", "| Publication | Format | Target | Status | Reason | Recoverable now |", "| --- | --- | --- | --- | --- | --- |"]
    for row in report["windows"]:
        identifier = row["publication_id"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {identifier} | {row['publication_format']} | {row['target_age_hours']}h | "
                     f"{row['status']} | {row['reason']} | {row['recoverable']} |")
    lines.extend(["", "Diagnostics: " + json.dumps(report["diagnostics"], sort_keys=True)])
    lines.extend(["", "## Format cohorts", "", report["comparison_basis"], "",
                  "| Target | Format | Usable / eligible | Mean reach | Mean save rate | Save observations | Capture age range (hours) |",
                  "| --- | --- | --- | --- | --- | --- | --- |"])
    for cohort in report["cohorts"]:
        rate = cohort["rates"]["save_rate"]
        age = cohort["capture_age_hours"]
        save_text = "unavailable" if rate["mean_rate"] is None else f"{rate['mean_rate']:.2%}"
        lines.append(f"| {cohort['target_age_hours']}h | {cohort['publication_format']} | "
                     f"{cohort['usable_publications']} / {cohort['eligible_publications']} | "
                     f"{cohort['reach']['mean']} | {save_text} | {rate['observations']} | "
                     f"{age['minimum']}–{age['maximum']} |")
    lines.extend(["", "Comparisons: " + json.dumps(report["comparisons"], sort_keys=True)])
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--now", help="Aware ISO-8601 timestamp")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--minimum-cohort-size", type=int, default=5)
    parser.add_argument("--output", type=Path, help="Create a new output file; never overwrite inputs or an existing file")
    args = parser.parse_args(argv)
    timestamp = parse_aware_timestamp(args.now) if args.now else None
    if args.now and timestamp is None:
        parser.error("--now must have a timezone")
    try:
        if args.history is None or args.snapshots is None:
            status = load_keychain_credentials(ENGAGEMENT_AUDIT_PROFILE)
            missing = [name for name in credential_variables(ENGAGEMENT_AUDIT_PROFILE) if not status[name]]
            if missing:
                raise ValueError("Missing engagement-audit credentials: " + ", ".join(missing))
        history, snapshots, invalid_count = _load_inputs(args.history, args.snapshots)
        report = build_feed_analytics_report(history, snapshots, now=timestamp,
                                             minimum_cohort_size=args.minimum_cohort_size)
        report["diagnostics"]["invalid_loaded_snapshots"] = invalid_count
        rendered = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) if args.json else render_markdown(report)
        if args.output:
            with args.output.open("x", encoding="utf-8") as destination:
                destination.write(rendered + "\n")
        else:
            print(rendered)
    except (OSError, ValueError, InsightsStorageError, BotoCoreError, ClientError) as exc:
        parser.exit(1, f"Feed analytics report failed ({type(exc).__name__})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
