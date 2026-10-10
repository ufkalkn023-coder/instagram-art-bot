"""Read-only Feed coverage diagnostics from authoritative identities and attempts."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

from src.insights_collector import SLOT_WINDOWS_HOURS, _legacy_publication
from src.insights_snapshot import LEARNING_OUTCOME_METRICS, SNAPSHOT_COMPLETION_STATUSES, inferred_snapshot_status, learning_snapshot_components, usable_metric_number
from src.insights_storage import parse_aware_timestamp, utc_timestamp


def _feed_publications(history: Mapping[str, object], now: datetime, diagnostics: Counter) -> list[dict]:
    records = history.get("publications", [])
    if not isinstance(records, list):
        raise ValueError("History publications must be a list")
    publications = []
    for raw in records:
        if not isinstance(raw, dict) or raw.get("type") not in {"single", "carousel"}:
            diagnostics["excluded_non_feed_records"] += 1
            continue
        target = _legacy_publication(raw)
        if target is None:
            diagnostics["invalid_publications"] += 1
            continue
        posted_at = parse_aware_timestamp(target["published_at"])
        if posted_at > now:
            diagnostics["future_publications"] += 1
            continue
        delivery = raw.get("story_delivery")
        story = {}
        if isinstance(delivery, Mapping) and delivery.get("schema_version") == "artfolio-story-delivery-v1":
            controlled = {
                "narrative": {"single_study", "comparison", "thematic_selection"},
                "cover_variant": {"museum_journal", "artwork_first", "detail_study"},
                "headline_kind": {"source_title", "factual_collection", "ai_proposal", "user_edit"},
            }
            for field, allowed in controlled.items():
                value = delivery.get(field)
                if isinstance(value, str) and value.strip() in allowed:
                    story[field] = value.strip()
            pages = delivery.get("pages")
            if isinstance(pages, list) and 3 <= len(pages) <= 10:
                story["page_count"] = len(pages)
        publications.append({"publication_id": target["reel_id"],
                             "media_id": target["instagram_media_id"],
                             "publication_format": raw["type"], "posted_at": posted_at,
                             **({"story_delivery": story} if story else {})})
    ids = Counter(pub["publication_id"] for pub in publications)
    media = Counter(pub["media_id"] for pub in publications)
    valid = []
    for pub in publications:
        if ids[pub["publication_id"]] > 1 or media[pub["media_id"]] > 1:
            diagnostics["conflicting_publications"] += 1
        else:
            valid.append(pub)
    return sorted(valid, key=lambda pub: (pub["posted_at"], pub["publication_id"]))


def _snapshot_attempts(publications: list[dict], snapshots: Sequence[object], now: datetime,
                       diagnostics: Counter) -> dict[tuple[str, int], list[dict]]:
    by_id = {pub["publication_id"]: pub for pub in publications}
    by_media = {pub["media_id"]: pub["publication_id"] for pub in publications}
    attempts = defaultdict(list)
    seen = set()
    for raw in snapshots:
        if not isinstance(raw, Mapping):
            diagnostics["invalid_snapshots"] += 1
            continue
        publication_id, media_id = raw.get("publication_id"), raw.get("media_id")
        if not isinstance(publication_id, str) or not isinstance(media_id, str):
            diagnostics["invalid_snapshots"] += 1
            continue
        publication_id, media_id = publication_id.strip(), media_id.strip()
        pub = by_id.get(publication_id)
        if pub is None:
            diagnostics["snapshot_identity_mismatches" if media_id in by_media else "unmatched_snapshots"] += 1
            continue
        if media_id != pub["media_id"]:
            diagnostics["snapshot_identity_mismatches"] += 1
            continue
        target, metrics = raw.get("target_age_hours"), raw.get("metrics")
        explicit_status, failure = raw.get("completion_status"), raw.get("failure_category")
        captured = parse_aware_timestamp(raw.get("captured_at"))
        if (isinstance(target, bool) or not isinstance(target, int) or target not in SLOT_WINDOWS_HOURS
                or captured is None or not isinstance(metrics, Mapping)
                or (explicit_status is not None and (not isinstance(explicit_status, str)
                    or explicit_status not in SNAPSHOT_COMPLETION_STATUSES))
                or (failure is not None and not isinstance(failure, str))
                or any(not isinstance(key, str) or usable_metric_number(value) is None
                       for key, value in metrics.items())):
            diagnostics["invalid_snapshots"] += 1
            continue
        if captured > now:
            diagnostics["future_snapshots"] += 1
            continue
        age = (captured - pub["posted_at"]).total_seconds() / 3600
        if age < target:
            diagnostics["premature_snapshots"] += 1
            continue
        status = inferred_snapshot_status(raw)
        usable = learning_snapshot_components(metrics) is not None
        if (status == "learning_complete") != usable:
            diagnostics["invalid_snapshots"] += 1
            continue
        # Identical append-only versions do not create additional observations.
        identity = (publication_id, media_id, target, captured, tuple(sorted(metrics.items())),
                    status, raw.get("failure_category"))
        if identity in seen:
            diagnostics["duplicate_snapshot_attempts"] += 1
            continue
        seen.add(identity)
        attempts[(publication_id, target)].append({
            "captured_at": utc_timestamp(captured), "captured_age_hours": age,
            "metrics": dict(metrics), "completion_status": status,
            "failure_category": raw.get("failure_category"),
            "in_target_window": age < SLOT_WINDOWS_HOURS[target],
        })
    for values in attempts.values():
        values.sort(key=lambda item: (item["captured_at"], repr(sorted(item["metrics"].items()))))
    return attempts


def _window(pub: dict, target: int, attempts: list[dict], now: datetime) -> dict[str, Any]:
    age = (now - pub["posted_at"]).total_seconds() / 3600
    deadline = SLOT_WINDOWS_HOURS[target]
    complete = [item for item in attempts if item["completion_status"] == "learning_complete"]
    in_window_complete = [item for item in complete if item["in_target_window"]]
    terminal = [item for item in attempts if item["completion_status"] == "permanently_unavailable"]
    selected = None
    if in_window_complete:
        selected = in_window_complete[-1]
        status, reason, recoverable = "complete", "usable_snapshot", False
    elif complete:
        selected = complete[-1]
        status, reason, recoverable = "missed", "late_snapshot_outside_collection_window", False
    elif terminal:
        selected = terminal[-1]
        reason = selected["failure_category"] or "permanently_unavailable"
        status, recoverable = ("missed" if reason == "missed_collection_window" else "unavailable"), False
    elif age >= deadline:
        status, reason, recoverable = "missed", "missed_collection_window", False
        selected = attempts[-1] if attempts else None
    elif attempts:
        selected = attempts[-1]
        status, reason, recoverable = "partial", "missing_learning_metrics", True
    elif age >= target:
        status, reason, recoverable = "due", "no_snapshot", True
    else:
        status, reason, recoverable = "pending", "target_age_not_reached", False
    return {"publication_id": pub["publication_id"], "media_id": pub["media_id"],
            "publication_format": pub["publication_format"], "posted_at": utc_timestamp(pub["posted_at"]),
            **({"story_delivery": dict(pub["story_delivery"])} if pub.get("story_delivery") else {}),
            "publication_age_hours": age, "target_age_hours": target, "window_end_age_hours": deadline,
            "status": status, "reason": reason, "recoverable": recoverable,
            "attempt_count": len(attempts), "captured_at": selected["captured_at"] if selected else None,
            "captured_age_hours": selected["captured_age_hours"] if selected else None,
            "comparable": bool(status == "complete" and selected["in_target_window"]),
            "metrics": dict(selected["metrics"]) if selected else {}}


def _story_cohorts(windows: list[dict], minimum: int) -> list[dict]:
    cohorts = []
    for target in SLOT_WINDOWS_HOURS:
        for dimension in ("narrative", "cover_variant", "headline_kind"):
            values = sorted({row["story_delivery"][dimension] for row in windows
                             if row.get("story_delivery", {}).get(dimension)})
            for value in values:
                rows = [row for row in windows if row["target_age_hours"] == target
                        and row.get("story_delivery", {}).get(dimension) == value]
                usable = [row for row in rows if row["comparable"]]
                eligible = sum(row["publication_age_hours"] >= target for row in rows)
                rates = {}
                for metric, rate_name in LEARNING_OUTCOME_METRICS:
                    metric_rows = [row for row in usable if metric in row["metrics"]]
                    values_for_metric = [row["metrics"][metric] / row["metrics"]["reach"]
                                         for row in metric_rows]
                    rates[rate_name] = {
                        "observations": len(values_for_metric),
                        "coverage": len(values_for_metric) / eligible if eligible else None,
                        "mean_rate": mean(values_for_metric) if values_for_metric else None,
                        "median_rate": median(values_for_metric) if values_for_metric else None,
                        "weighted_rate": (sum(row["metrics"][metric] for row in metric_rows)
                                          / sum(row["metrics"]["reach"] for row in metric_rows)
                                          if metric_rows else None),
                    }
                cohorts.append({"dimension": dimension, "value": value,
                                "target_age_hours": target,
                                "window_end_age_hours": SLOT_WINDOWS_HOURS[target],
                                "eligible_publications": eligible,
                                "usable_publications": len(usable),
                                "coverage": len(usable) / eligible if eligible else None,
                                "minimum_cohort_size": minimum,
                                "minimum_cohort_met": len(usable) >= minimum,
                                "status": "descriptive_only" if len(usable) >= minimum else "insufficient_data",
                                "winner": None, "rates": rates})
    return cohorts


def _format_cohorts(windows: list[dict], minimum: int) -> tuple[list[dict], list[dict]]:
    cohorts, comparisons = [], []
    for target in SLOT_WINDOWS_HOURS:
        by_format = {}
        for format_name in ("carousel", "single"):
            rows = [row for row in windows if row["publication_format"] == format_name
                    and row["target_age_hours"] == target]
            usable = [row for row in rows if row["comparable"]]
            eligible = sum(row["publication_age_hours"] >= target for row in rows)
            ages = [row["captured_age_hours"] for row in usable]
            reach = [row["metrics"]["reach"] for row in usable]
            rates = {}
            for metric, rate_name in LEARNING_OUTCOME_METRICS:
                metric_rows = [row for row in usable if metric in row["metrics"]]
                values = [row["metrics"][metric] / row["metrics"]["reach"] for row in metric_rows]
                rates[rate_name] = {
                    "observations": len(values),
                    "coverage": len(values) / eligible if eligible else None,
                    "mean_rate": mean(values) if values else None,
                    "median_rate": median(values) if values else None,
                    "weighted_rate": (sum(row["metrics"][metric] for row in metric_rows)
                                      / sum(row["metrics"]["reach"] for row in metric_rows)
                                      if metric_rows else None),
                }
            cohort = {"publication_format": format_name, "target_age_hours": target,
                      "window_end_age_hours": SLOT_WINDOWS_HOURS[target],
                      "eligible_publications": eligible, "usable_publications": len(usable),
                      "coverage": len(usable) / eligible if eligible else None,
                      "capture_age_hours": {"minimum": min(ages) if ages else None,
                                            "maximum": max(ages) if ages else None,
                                            "median": median(ages) if ages else None},
                      "reach": {"observations": len(reach), "mean": mean(reach) if reach else None,
                                "median": median(reach) if reach else None}, "rates": rates}
            cohorts.append(cohort)
            by_format[format_name] = cohort
        left, right = by_format["carousel"], by_format["single"]
        sufficient = min(left["usable_publications"], right["usable_publications"]) >= minimum
        ages_left, ages_right = left["capture_age_hours"], right["capture_age_hours"]
        overlap = (max(ages_left["minimum"], ages_right["minimum"])
                   <= min(ages_left["maximum"], ages_right["maximum"])
                   if ages_left["minimum"] is not None and ages_right["minimum"] is not None else False)
        supported = [name for _, name in LEARNING_OUTCOME_METRICS
                     if min(left["rates"][name]["observations"], right["rates"][name]["observations"]) >= minimum]
        status = ("insufficient_data" if not sufficient or not supported else
                  "capture_age_mismatch" if not overlap else "descriptive_only")
        comparisons.append({"target_age_hours": target, "minimum_cohort_size": minimum,
                            "status": status, "capture_age_ranges_overlap": overlap,
                            "sufficient_metrics": supported, "winner": None})
    return cohorts, comparisons


def build_feed_analytics_report(history: Mapping[str, object], snapshots: Sequence[object], *,
                                now: datetime | None = None, minimum_cohort_size: int = 5) -> dict[str, Any]:
    """Explain each existing collector window without collecting or mutating it."""
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Report time must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc)
    if isinstance(minimum_cohort_size, bool) or not isinstance(minimum_cohort_size, int) or minimum_cohort_size < 2:
        raise ValueError("Minimum cohort size must be an integer of at least 2")
    if not isinstance(history, Mapping) or not isinstance(snapshots, (list, tuple)):
        raise ValueError("Report requires a history object and a snapshot array")
    diagnostics = Counter()
    publications = _feed_publications(history, timestamp, diagnostics)
    attempts = _snapshot_attempts(publications, snapshots, timestamp, diagnostics)
    windows = [_window(pub, target, attempts.get((pub["publication_id"], target), []), timestamp)
               for pub in publications for target in SLOT_WINDOWS_HOURS]
    counts = Counter(row["status"] for row in windows)
    cohorts, comparisons = _format_cohorts(windows, minimum_cohort_size)
    story_cohorts = _story_cohorts(windows, minimum_cohort_size)
    return {"schema_version": 1, "generated_at": utc_timestamp(timestamp),
            "comparison_basis": "Same target-age windows; actual capture-age ranges are disclosed. Descriptive observational results do not establish a winner or causality.",
            "cohorts": cohorts, "comparisons": comparisons, "story_cohorts": story_cohorts,
            "summary": {"feed_publications": len(publications), "total_windows": len(windows),
                        **{f"{name}_windows": counts[name]
                           for name in ("complete", "partial", "unavailable", "missed", "due", "pending")},
                        "publications_with_missed_windows": len({row["publication_id"] for row in windows
                                                                  if row["status"] == "missed"})},
            "diagnostics": dict(sorted(diagnostics.items())), "windows": windows}
