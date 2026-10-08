"""Descriptive equal-age caption experiment cohorts; no automated promotion."""

from collections import defaultdict
from math import sqrt
from statistics import mean, stdev

from pydantic import ValidationError

from src.feed_analytics import build_feed_analytics_report
from src.insights_snapshot import LEARNING_OUTCOME_METRICS
from src.models import ControlledCaptionExperiment


def build_experiment_report(history, snapshots, *, now=None, minimum_cohort_size=5):
    report = build_feed_analytics_report(history, snapshots, now=now, minimum_cohort_size=minimum_cohort_size)
    eligible_ids = {row["publication_id"] for row in report["windows"]}
    assignments = {}
    for pub in history.get("publications", []):
        if not isinstance(pub, dict) or pub.get("id") not in eligible_ids or pub.get("type") != "carousel":
            continue
        try:
            assignment = ControlledCaptionExperiment.model_validate(pub.get("controlled_experiment"))
        except ValidationError:
            continue
        if (pub.get("carousel_theme") != assignment.theme_id or pub.get("caption_hook_type") != assignment.variant
                or pub.get("cover_variant") != assignment.cover_variant):
            continue
        assignments[pub["id"]] = assignment
    groups = defaultdict(lambda: defaultdict(list))
    for row in report["windows"]:
        assignment = assignments.get(row["publication_id"])
        if assignment is not None and row["publication_age_hours"] >= row["target_age_hours"]:
            key = (assignment.experiment_id, assignment.theme_id, assignment.cover_variant, row["target_age_hours"])
            groups[key][assignment.variant].append(row)
    comparisons = []
    for (experiment_id, theme_id, cover, target), variants in sorted(groups.items()):
        arms = {}
        sufficient = False
        for variant in ("question", "visual_detail"):
            rows = variants[variant]
            rates = {}
            for metric, rate in LEARNING_OUTCOME_METRICS:
                values = [row["metrics"][metric] / row["metrics"]["reach"] for row in rows
                          if row["comparable"] and metric in row["metrics"]]
                rates[rate] = {"observations": len(values), "eligible_publications": len(rows),
                               "coverage": len(values) / len(rows) if rows else None,
                               "mean_rate": mean(values) if values else None,
                               "standard_error": stdev(values) / sqrt(len(values)) if len(values) > 1 else None,
                               "observed_range": [min(values), max(values)] if values else None}
            arms[variant] = rates
        metric_status = {}
        for _, rate in LEARNING_OUTCOME_METRICS:
            comparable = all(arms[variant][rate]["observations"] >= minimum_cohort_size for variant in arms)
            metric_status[rate] = "descriptive_comparison" if comparable else "insufficient_evidence"
            sufficient = sufficient or comparable
        comparisons.append({"experiment_id": experiment_id, "publication_format": "carousel",
                            "theme_id": theme_id, "cover_variant": cover, "target_age_hours": target,
                            "status": "descriptive_comparison" if sufficient else "insufficient_evidence",
                            "metric_status": metric_status, "arms": arms, "winner": None})
    return {"schema_version": 1, "generated_at": report["generated_at"], "auto_promote": False,
            "comparisons": comparisons,
            "limitations": "Observational cohorts. Standard errors describe sample variation; theme, timing and audience confounding prevent causal winner claims."}
