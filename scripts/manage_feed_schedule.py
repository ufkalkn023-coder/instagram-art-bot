#!/usr/bin/env python3
"""Operator-only schedule control. Inspect by default; no publication or migration."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.feed_schedule import (  # noqa: E402
    FEED_REPOSITORY,
    FeedScheduleError,
    FeedScheduleManager,
)
from src.models import FeedSchedulePermit, parse_receipt_occurrence  # noqa: E402
from src.publication_state import StateConflictError  # noqa: E402


class GitHubEvidence:
    """Authenticated GETs only. Incomplete or truncated evidence rejects rearming."""

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise FeedScheduleError("SCHEDULE_REVIEW_GITHUB_TOKEN_REQUIRED")
        response = requests.get(
            f"https://api.github.com/repos/{FEED_REPOSITORY}/{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params=params,
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        value = response.json()
        if response.status_code != 200 or not isinstance(value, dict):
            raise FeedScheduleError("SCHEDULE_GITHUB_EVIDENCE_INVALID")
        return value

    def main_sha(self) -> str:
        value = self._get("git/ref/heads/main")
        if (
            value.get("ref") != "refs/heads/main"
            or value.get("object", {}).get("type") != "commit"
        ):
            raise FeedScheduleError("SCHEDULE_MAIN_SHA_LOOKUP_INVALID")
        return value["object"]["sha"]

    def audit_runs(self, permit: FeedSchedulePermit) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        total: int | None = None
        retrieved = 0
        # GitHub caps filtered run searches at 1000; reaching the cap is unsafe.
        for page in range(1, 11):
            value = self._get(
                "actions/workflows/instagram_bot.yml/runs",
                {
                    "event": "schedule",
                    "created": f"{permit.slot_at}..{permit.expires_at}",
                    "per_page": 100,
                    "page": page,
                },
            )
            count = value.get("total_count")
            runs = value.get("workflow_runs")
            if (
                type(count) is not int
                or count < 0
                or count >= 1000
                or total is not None
                and total != count
                or not isinstance(runs, list)
                or any(not isinstance(r, dict) for r in runs)
            ):
                raise FeedScheduleError("SCHEDULE_RUN_AUDIT_INCOMPLETE")
            total = count
            retrieved += len(runs)
            # The REST range includes expiry; such a run cannot use this permit.
            rows.extend(
                r
                for r in runs
                if parse_receipt_occurrence(r["created_at"])
                < parse_receipt_occurrence(permit.expires_at)
            )
            if page * 100 >= count:
                # Check the unfiltered count as well, including the exclusive end.
                if retrieved != count:
                    raise FeedScheduleError("SCHEDULE_RUN_AUDIT_INCOMPLETE")
                return rows
        raise FeedScheduleError("SCHEDULE_RUN_AUDIT_INCOMPLETE")


def main(
    argv: list[str] | None = None,
    *,
    manager: FeedScheduleManager | None = None,
    github: GitHubEvidence | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("inspect")
    for command in ("arm", "pause", "revoke", "acknowledge"):
        sub = commands.add_parser(command)
        sub.add_argument("--apply", action="store_true", required=True)
        sub.add_argument("--expected-generation", type=int, required=True)
        if command in {"pause", "revoke"}:
            sub.add_argument("--reason", required=True)
        else:
            sub.add_argument("--evidence-ref", required=True)
        if command == "arm":
            sub.add_argument("--approved-sha", required=True)
            sub.add_argument("--slot", required=True)
            sub.add_argument("--expires-at", required=True)
    args = parser.parse_args(argv)
    try:
        manager = manager or FeedScheduleManager()
        github = github or GitHubEvidence()
        if args.command in {None, "inspect"}:
            print(json.dumps(manager.inspect(now=now), sort_keys=True, indent=2))
        elif args.command == "arm":
            permit_id = manager.arm(
                expected_generation=args.expected_generation,
                approved_sha=args.approved_sha,
                main_sha=github.main_sha(),
                slot=parse_receipt_occurrence(args.slot),
                expires_at=parse_receipt_occurrence(args.expires_at),
                review_ref=args.evidence_ref,
                now=now,
            )
            print(json.dumps({"armed_permit_id": permit_id}, sort_keys=True))
        elif args.command == "acknowledge":
            manager.acknowledge(
                expected_generation=args.expected_generation,
                evidence_ref=args.evidence_ref,
                audit_runs=github.audit_runs,
                now=now,
            )
        else:
            operation = manager.pause if args.command == "pause" else manager.revoke
            operation(
                expected_generation=args.expected_generation,
                reason=args.reason,
                now=now,
            )
        return 0
    except Exception as error:
        # Do not include SDK/request exception details that could carry credentials.
        detail = (
            str(error)
            if isinstance(error, (FeedScheduleError, StateConflictError))
            else type(error).__name__
        )
        print(f"SCHEDULE_CONTROL_FAILED: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
