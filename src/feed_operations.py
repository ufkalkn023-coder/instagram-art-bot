"""Combined read-only operational report and opt-in meaningful-change ledger."""

from collections import Counter
from datetime import datetime, timedelta, timezone

from src.feed_analytics import build_feed_analytics_report
from src.insights_storage import parse_aware_timestamp, utc_timestamp
from src.publication_state import canonical_bytes, payload_digest, seal

STATUS_KEY = "feed-status/v1/notifications.json"
NORMAL_STATES = frozenset({"READY", "WAITING_COOLDOWN", "WAITING_FAILURE_BACKOFF", "WAITING_WINDOW"})


def build_feed_operations(schedule, queue, history, snapshots, *, now=None):
    timestamp = now or datetime.now(timezone.utc)
    analytics = build_feed_analytics_report(history, snapshots, now=timestamp)
    captures = [parse_aware_timestamp(row["captured_at"]) for row in analytics["windows"] if row["comparable"]]
    latest = max(captures) if captures else None
    counts = Counter()
    for row in queue:
        expiry = parse_aware_timestamp(row.get("expires_at"))
        state = row.get("state")
        if state == "READY":
            if expiry is None:
                counts["invalid_age"] += 1
            elif expiry <= timestamp:
                counts["expired"] += 1
            else:
                counts["ready"] += 1
                if row.get("publication_format") == schedule.get("next_format"):
                    counts["next_format_ready"] += 1
        elif state in {"CLAIMED", "CONSUMED", "QUARANTINED"}:
            counts[state.lower()] += 1
    freshness = ("fresh" if timestamp - latest <= timedelta(hours=48) else "stale") if latest else (
        "missing" if analytics["summary"]["feed_publications"] else "cold_start")
    status = schedule.get("status", "UNKNOWN")
    actionable = []
    if status not in NORMAL_STATES:
        actionable.append(f"schedule:{status}")
    if freshness == "stale":
        actionable.append("analytics:stale")
    return {"schema_version": 1, "generated_at": utc_timestamp(timestamp), "schedule": dict(schedule),
            "queue": {name: counts[name] for name in ("ready", "next_format_ready", "expired", "invalid_age", "claimed", "consumed", "quarantined")},
            "queue_count_basis": "Manifest freshness only; bytes, source rights and history require revalidation at consumption.",
            "analytics": {"freshness": freshness, "last_comparable_capture_at": utc_timestamp(latest) if latest else None,
                          **analytics["summary"]}, "actionable": actionable}


class FeedStatusNotifications:
    """Write only the separate dedup object; never safety, receipts or media."""

    def __init__(self, queue_store):
        self.store = queue_store

    def observe(self, report, *, now=None):
        timestamp = now or datetime.now(timezone.utc)
        data, etag = self.store._read_object(STATUS_KEY, 16_384, allow_missing=True)
        previous = None
        if data is not None:
            import json
            try:
                previous = json.loads(data)
                if (not isinstance(previous, dict) or previous.get("schema_version") != 1
                        or previous.get("payload_sha256") != payload_digest(previous)
                        or not isinstance(previous.get("actionable"), list)
                        or any(not isinstance(item, str) for item in previous["actionable"])):
                    raise ValueError("Invalid notification state")
            except (ValueError, TypeError) as error:
                raise RuntimeError("Feed notification state failed validation") from error
        current = {"actionable": list(report["actionable"]),
                   "last_completion_at": report["schedule"].get("last_successful_feed_at")}
        if previous is not None and all(previous.get(key) == value for key, value in current.items()):
            return None
        event = None
        if current["actionable"]:
            event = "blocked"
        elif previous is not None and previous["actionable"]:
            event = "recovered"
        elif previous is not None and current["last_completion_at"] != previous.get("last_completion_at"):
            event = "published"
        payload = seal({"schema_version": 1, **current, "observed_at": utc_timestamp(timestamp)})
        encoded = canonical_bytes(payload)
        self.store._put(STATUS_KEY, encoded, etag=etag, content_type="application/json")
        observed, _ = self.store._read_object(STATUS_KEY, 16_384)
        if observed != encoded:
            raise RuntimeError("Feed notification read-after-write is uncertain")
        if event is None:
            return None
        return {"event": event, **current}
