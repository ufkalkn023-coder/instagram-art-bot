"""Secret-free local health state derived from collector logs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path

LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}\b")
FAILURE_MARKER = "Insights collector could not start:"
SUCCESS_MARKER = "[insights] media_discovered="


@dataclass(frozen=True)
class CollectorRunHealth:
    last_success: datetime | None = None
    last_failure: datetime | None = None
    consecutive_failures: int = 0


def _timestamp(line: str, local_timezone: tzinfo) -> datetime | None:
    match = LOG_TIMESTAMP.match(line)
    if match is None:
        return None
    parsed = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=local_timezone).astimezone(timezone.utc)


def inspect_collector_log(
    path: Path,
    *,
    local_timezone: tzinfo | None = None,
) -> CollectorRunHealth:
    """Read only success/failure markers; error details are never returned."""
    zone = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return CollectorRunHealth()

    pending_timestamp: datetime | None = None
    events: list[tuple[str, datetime | None]] = []
    for line in lines:
        parsed = _timestamp(line, zone)
        if parsed is not None:
            pending_timestamp = parsed
        if FAILURE_MARKER in line:
            events.append(("failure", parsed or pending_timestamp))
        elif SUCCESS_MARKER in line:
            events.append(("success", parsed or pending_timestamp))

    last_success = next(
        (timestamp for kind, timestamp in reversed(events) if kind == "success"),
        None,
    )
    last_failure = next(
        (timestamp for kind, timestamp in reversed(events) if kind == "failure"),
        None,
    )
    consecutive_failures = 0
    for kind, _ in reversed(events):
        if kind != "failure":
            break
        consecutive_failures += 1
    return CollectorRunHealth(last_success, last_failure, consecutive_failures)


def freshness_state(
    last_success: datetime | None,
    *,
    now: datetime | None = None,
    consecutive_failures: int = 0,
) -> tuple[str, float | None]:
    """Classify an hourly collector using 2h stale and 6h critical limits."""
    if last_success is None:
        return ("CRITICAL" if consecutive_failures else "UNKNOWN"), None
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Health-check time must be timezone-aware")
    age_hours = max(
        0.0,
        (timestamp.astimezone(timezone.utc) - last_success.astimezone(timezone.utc)).total_seconds()
        / 3600,
    )
    if age_hours <= 2:
        return "HEALTHY", age_hours
    if age_hours <= 6:
        return "STALE", age_hours
    return "CRITICAL", age_hours
