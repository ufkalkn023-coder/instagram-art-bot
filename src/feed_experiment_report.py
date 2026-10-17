"""Descriptive equal-age caption experiment cohorts; no automated promotion."""

from collections import defaultdict
from math import sqrt
from statistics import mean, stdev

from pydantic import ValidationError

from src.feed_analytics import build_feed_analytics_report
from src.insights_snapshot import LEARNING_OUTCOME_METRICS
from src.models import ControlledCaptionExperiment, PublicationRecord


def build_experiment_report(history, snapshots, *, now=None, minimum_cohort_size=5):
    report = build_feed_analytics_report(history, snapshots, now=now, minimum_cohort_size=minimum_cohort_size)
    eligible_ids = {row["publication_id"] for row in report["windows"]}
    assignments = {}
    for pub in history.get("publications", []):
        if not isinstance(pub, dict) or pub.get("id") not in eligible_ids or pub.get("type") != "carousel":
            continue
        try:
            validated = PublicationRecord.model_validate(pub)
            assignment = validated.controlled_experiment or validated.controlled_cover_experiment
        except ValidationError:
            continue
        if assignment is None:
            continue
        assignments[pub["id"]] = assignment
    groups = defaultdict(lambda: defaultdict(list))
    for row in report["windows"]:
        assignment = assignments.get(row["publication_id"])
        if assignment is not None and row["publication_age_hours"] >= row["target_age_hours"]:
            fixed = assignment.cover_variant if isinstance(assignment, ControlledCaptionExperiment) else assignment.caption_hook_type
            key = (assignment.experiment_id, assignment.variable, assignment.theme_id, fixed, row["target_age_hours"])
            groups[key][assignment.variant].append(row)
    comparisons = []
    for (experiment_id, variable, theme_id, fixed, target), variants in sorted(groups.items()):
        arms = {}
        sufficient = False
        choices = ("question", "visual_detail") if variable == "caption_hook_type" else ("editorial", "editorial_panel")
        for variant in choices:
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
                            "theme_id": theme_id, "variable": variable, "target_age_hours": target,
                            **({"cover_variant": fixed} if variable == "caption_hook_type" else {"caption_hook_type": fixed}),
                            "status": "descriptive_comparison" if sufficient else "insufficient_evidence",
                            "metric_status": metric_status, "arms": arms, "winner": None})
    return {"schema_version": 1, "generated_at": report["generated_at"], "auto_promote": False,
            "comparisons": [row for row in comparisons if row["variable"] == "caption_hook_type"],
            "cover_comparisons": [row for row in comparisons if row["variable"] == "cover_variant"],
            "limitations": "Observational cohorts. Standard errors describe sample variation; artwork, theme, timing and audience confounding prevent causal winner claims."}
