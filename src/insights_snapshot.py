"""Shared classification rules for persisted Instagram Insights snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping


LEARNING_OUTCOME_METRICS = (
    ("shares", "share_rate"),
    ("saved", "save_rate"),
    ("comments", "comment_rate"),
    ("likes", "like_rate"),
)
SNAPSHOT_COMPLETION_STATUSES = {
    "learning_complete",
    "partial",
    "permanently_unavailable",
}


def usable_metric_number(value: object) -> float | None:
    """Return a finite, non-negative metric value without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def learning_snapshot_components(
    metrics: Mapping[str, object],
) -> tuple[float, dict[str, float]] | None:
    """Return the exact reach/outcome inputs required by engagement learning."""
    reach = usable_metric_number(metrics.get("reach"))
    if reach is None or reach <= 0:
        return None
    components: dict[str, float] = {"reach_signal": reach}
    for metric, output_name in LEARNING_OUTCOME_METRICS:
        value = usable_metric_number(metrics.get(metric))
        if value is not None:
            components[output_name] = value / reach
    if len(components) == 1:
        return None
    return reach, components


def inferred_snapshot_status(snapshot: Mapping[str, object]) -> str:
    """Classify legacy snapshots while honoring explicit terminal/status records."""
    explicit = snapshot.get("completion_status")
    if explicit in SNAPSHOT_COMPLETION_STATUSES:
        return str(explicit)
    metrics = snapshot.get("metrics")
    if isinstance(metrics, Mapping) and learning_snapshot_components(metrics) is not None:
        return "learning_complete"
    return "partial"
