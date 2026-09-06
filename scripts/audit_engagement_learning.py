#!/usr/bin/env python3
"""Explain the engagement-learning funnel without mutating any external state."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.history_tracker as history_tracker  # noqa: E402
from src.engagement_learning import (  # noqa: E402
    EngagementAudit,
    analyze_engagement_learning,
)
from src.insights_storage import InsightsStorage, parse_aware_timestamp  # noqa: E402


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _load_inputs(
    history_path: Path | None,
    snapshots_path: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    if history_path is None:
        history, _ = history_tracker.load_history_with_etag()
    else:
        history = _load_json(history_path)
    if not isinstance(history, dict):
        raise ValueError("History input must be a JSON object")

    invalid_loaded_snapshots = 0
    if snapshots_path is None:
        storage = InsightsStorage()
        snapshots = storage.load_all_snapshots()
        invalid_loaded_snapshots = storage.last_snapshot_load_diagnostics.invalid_snapshots
    else:
        payload = _load_json(snapshots_path)
        snapshots = payload.get("snapshots") if isinstance(payload, dict) else payload
    if not isinstance(snapshots, list):
        raise ValueError("Snapshots input must be a JSON array or an object containing snapshots")
    return history, snapshots, invalid_loaded_snapshots


def _slot_text(values: dict[int, int] | Any) -> str:
    return ",".join(f"{slot}h={values.get(slot, 0)}" for slot in (24, 72, 168))


def _render_text(
    audit: EngagementAudit,
    *,
    invalid_loaded_snapshots: int,
    verbose: bool,
) -> str:
    reach_text = (
        "unavailable"
        if audit.average_reach is None
        else (
            f"avg={audit.average_reach!r},min={audit.minimum_reach!r},"
            f"max={audit.maximum_reach!r}"
        )
    )
    exclusions = ",".join(
        f"{reason}={count}" for reason, count in audit.excluded_by_reason.items()
    ) or "none"
    lines = [
        "# Engagement Learning Audit",
        f"total_publication_records={audit.total_publication_records}",
        f"carousel_publications={audit.carousel_publications}",
        f"single_publications={audit.single_publications}",
        f"valid_publication_media_identities={audit.valid_publication_media_identities}",
        f"publications_with_snapshots={audit.publications_with_snapshots}",
        f"snapshot_slot_publications={_slot_text(audit.snapshot_slot_publications)}",
        f"eligible_learning_observations={audit.eligible_learning_observations}",
        f"excluded_by_reason={exclusions}",
        f"selected_snapshot_slots={_slot_text(audit.selected_snapshot_slot_counts)}",
        f"reach={reach_text}",
        f"mature_observations={audit.mature_observations}",
        f"provisional_observations={audit.provisional_observations}",
        f"effective_observations={audit.effective_observations!r}",
        f"global_confidence={audit.global_confidence!r}",
        f"invalid_loaded_snapshots={invalid_loaded_snapshots}",
    ]
    if verbose:
        lines.append("observations:")
        for item in audit.observations:
            lines.append(
                "  "
                f"publication={item.publication_identifier} "
                f"slot={item.selected_target_age_hours}h reach={item.reach!r} "
                f"maturity={item.maturity_factor!r} recency={item.recency_factor!r} "
                f"reach_confidence={item.reach_confidence_factor!r} "
                f"metric_coverage={item.metric_coverage_factor!r} "
                f"weight={item.final_observation_weight!r}"
            )
        lines.append(
            "verbose_weight_sum="
            f"{sum(item.final_observation_weight for item in audit.observations)!r}"
        )
    return "\n".join(lines)


def _render_json(audit: EngagementAudit, invalid_loaded_snapshots: int) -> str:
    payload = {
        "total_publication_records": audit.total_publication_records,
        "carousel_publications": audit.carousel_publications,
        "single_publications": audit.single_publications,
        "valid_publication_media_identities": audit.valid_publication_media_identities,
        "publications_with_snapshots": audit.publications_with_snapshots,
        "snapshot_slot_publications": audit.snapshot_slot_publications,
        "eligible_learning_observations": audit.eligible_learning_observations,
        "excluded_by_reason": audit.excluded_by_reason,
        "selected_snapshot_slot_counts": audit.selected_snapshot_slot_counts,
        "average_reach": audit.average_reach,
        "minimum_reach": audit.minimum_reach,
        "maximum_reach": audit.maximum_reach,
        "mature_observations": audit.mature_observations,
        "provisional_observations": audit.provisional_observations,
        "effective_observations": audit.effective_observations,
        "global_confidence": audit.global_confidence,
        "invalid_loaded_snapshots": invalid_loaded_snapshots,
        "observations": [item.__dict__ for item in audit.observations],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only engagement-learning funnel and weight audit"
    )
    parser.add_argument("--history", type=Path, help="Local posted_history.json input")
    parser.add_argument("--snapshots", type=Path, help="Local snapshot JSON input")
    parser.add_argument("--now", help="Aware ISO-8601 evaluation timestamp")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now: datetime | None = None
    if args.now:
        now = parse_aware_timestamp(args.now)
        if now is None:
            parser.error("--now must be an ISO-8601 timestamp with a timezone")
    try:
        history, snapshots, invalid_count = _load_inputs(args.history, args.snapshots)
        audit = analyze_engagement_learning(history, snapshots, now=now)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"engagement audit failed: {exc}\n")
    print(
        _render_json(audit, invalid_count)
        if args.json
        else _render_text(audit, invalid_loaded_snapshots=invalid_count, verbose=args.verbose)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
