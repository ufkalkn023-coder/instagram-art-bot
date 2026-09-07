#!/usr/bin/env python3
"""Run a read-only temporal backtest of engagement-learning reach priors."""

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
from src.engagement_backtest import (  # noqa: E402
    BacktestReport,
    DatasetEvaluation,
    run_engagement_backtest,
)
from src.insights_storage import InsightsStorage, parse_aware_timestamp  # noqa: E402
from src.local_credentials import (  # noqa: E402
    ENGAGEMENT_AUDIT_PROFILE,
    credential_variables,
    load_keychain_credentials,
)


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
        invalid_loaded_snapshots = (
            storage.last_snapshot_load_diagnostics.invalid_snapshots
        )
    else:
        payload = _load_json(snapshots_path)
        snapshots = payload.get("snapshots") if isinstance(payload, dict) else payload
    if not isinstance(snapshots, list):
        raise ValueError("Snapshots input must be a JSON array or contain snapshots")
    return history, snapshots, invalid_loaded_snapshots


def _parse_priors(value: str) -> tuple[float, ...]:
    priors = []
    for item in value.split(","):
        normalized = item.strip().casefold()
        if not normalized:
            continue
        prior = 0.0 if normalized in {"none", "no_discount"} else float(normalized)
        if prior < 0:
            raise ValueError("reach priors cannot be negative")
        priors.append(prior)
    if not priors:
        raise ValueError("at least one reach prior is required")
    return tuple(dict.fromkeys(priors))


def _number(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _percent(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{100 * value:.{digits}f}%"


def _evaluation_table(dataset: DatasetEvaluation) -> list[str]:
    lines = [
        "| K | E | Confidence | Learned % | OOS score | Rank stability | Single-post risk | Verdict |",
        "| -: | -: | -: | -: | -: | -: | -: | :-- |",
    ]
    for item in dataset.evaluations:
        stability = item.stability.bootstrap_feature_top10_overlap
        lines.append(
            f"| {item.label} | {item.effective_observations:.4f} | "
            f"{_percent(item.confidence, 2)} | {_percent(item.learned_influence, 2)} | "
            f"{_number(item.performance.oos_score, 2)} | {_percent(stability)} | "
            f"{item.single_publication_risk.rating} | {item.verdict} |"
        )
    return lines


def _performance_table(dataset: DatasetEvaluation) -> list[str]:
    lines = [
        "| K | Folds | Spearman | Pearson | MAE | Calibration | Ordering | OOS 95% CI |",
        "| -: | -: | -: | -: | -: | -: | -: | :-- |",
    ]
    for item in dataset.evaluations:
        performance = item.performance
        interval = (
            "n/a"
            if performance.oos_score_ci_low is None
            else (
                f"[{performance.oos_score_ci_low:.2f}, "
                f"{performance.oos_score_ci_high:.2f}]"
            )
        )
        lines.append(
            f"| {item.label} | {performance.folds} | "
            f"{_number(performance.spearman)} | {_number(performance.pearson)} | "
            f"{_number(performance.mae)} | {_number(performance.calibration_error)} | "
            f"{_percent(performance.ordering_accuracy)} | {interval} |"
        )
    return lines


def _render_text(report: BacktestReport) -> str:
    all_data, mature = report.datasets
    reach = (
        f"min={_number(report.reach_minimum, 1)}, "
        f"median={_number(report.reach_median, 1)}, "
        f"mean={_number(report.reach_mean, 2)}, "
        f"max={_number(report.reach_maximum, 1)}"
    )
    lines = [
        "# Dataset",
        "",
        f"Total publications: {report.total_publications}",
        f"Carousel publications: {report.carousel_publications}",
        f"Usable publications: {report.usable_publications}",
        f"Mature / provisional: {report.mature_publications} / {report.provisional_publications}",
        f"Reach: {reach}",
        f"Invalid loaded snapshots: {report.invalid_loaded_snapshots}",
        "",
        "# Method",
        "",
        (
            "Expanding-window one-step-ahead evaluation; minimum train size "
            f"{report.minimum_training_observations}; seeded bootstrap "
            f"n={report.bootstrap_samples}."
        ),
        "Reach discount changes observation reliability weights only. Outcome-rate "
        "normalization and all other production calibration parameters remain fixed.",
        "Leakage controls: " + "; ".join(report.leakage_controls) + ".",
        "OOS score is an equal-weight composite of available rank/linear correlation, "
        "MAE, mean calibration error, and pairwise ordering accuracy.",
        "",
        "# Reach Prior Comparison",
        "",
        *_evaluation_table(all_data),
        "",
        "# Temporal OOS Results",
        "",
        *_performance_table(all_data),
        "",
        "Confidence growth curves (chronological folds):",
    ]
    for item in all_data.evaluations:
        curve = ", ".join(
            f"{100 * value:.2f}" for value in item.confidence_growth_curve
        )
        lines.append(f"- K={item.label}: [{curve}]%")
    lines.extend(
        [
            "",
            "# Mature-only Results",
            "",
            *_evaluation_table(mature),
            "",
            *_performance_table(mature),
            "",
            "# Stability",
            "",
            "| K | Theme agreement | Feature agreement | Bootstrap top-10 | "
            "Adjustment SD | Max adjustment | Sign flips |",
            "| -: | -: | -: | -: | -: | -: | -: |",
        ]
    )
    for item in all_data.evaluations:
        stability = item.stability
        lines.append(
            f"| {item.label} | {_percent(stability.theme_rank_agreement)} | "
            f"{_percent(stability.feature_rank_agreement)} | "
            f"{_percent(stability.bootstrap_feature_top10_overlap)} | "
            f"{stability.fold_prediction_adjustment_sd:.3f} | "
            f"{stability.maximum_learned_adjustment:.3f} | "
            f"{_percent(stability.sign_flip_rate)} "
            f"({stability.sign_flip_comparisons}) |"
        )
    lines.extend(
        [
            "",
            "# Overfitting Risk",
            "",
            "| K | Max single-publication feature change | Max weight share | Risk |",
            "| -: | -: | -: | :-- |",
        ]
    )
    for item in all_data.evaluations:
        risk = item.single_publication_risk
        lines.append(
            f"| {item.label} | {risk.maximum_feature_score_change:.3f} | "
            f"{_percent(risk.maximum_observation_weight_share)} | {risk.rating} |"
        )
    selected = next(
        (
            item
            for item in all_data.evaluations
            if report.recommendation.endswith(f"K={item.reach_prior:g}")
        ),
        next((item for item in all_data.evaluations if item.reach_prior == 750), None),
    )
    lines.extend(
        [
            "",
            "# Recommendation",
            "",
            report.recommendation,
            "",
            f"Recommended K: {report.recommendation}",
            f"Evidence strength: {report.evidence_strength}",
            (
                "Expected confidence: n/a"
                if selected is None
                else f"Expected confidence: {_percent(selected.confidence, 2)}"
            ),
            (
                "Expected learned influence: n/a"
                if selected is None
                else f"Expected learned influence: {_percent(selected.learned_influence, 2)}"
            ),
            "Production parameter changed: NO",
        ]
    )
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only temporal backtest for engagement-learning reach priors"
    )
    parser.add_argument("--history", type=Path, help="Local posted_history.json input")
    parser.add_argument("--snapshots", type=Path, help="Local snapshot JSON input")
    parser.add_argument(
        "--reach-priors",
        default="750,500,250,100,none",
        help="Comma-separated non-negative priors; use 'none' for no discount",
    )
    parser.add_argument("--minimum-train", type=int, default=8)
    parser.add_argument("--bootstrap-samples", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--now", help="Aware ISO-8601 evaluation timestamp")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now: datetime | None = None
    if args.now:
        now = parse_aware_timestamp(args.now)
        if now is None:
            parser.error("--now must be an ISO-8601 timestamp with a timezone")
    try:
        priors = _parse_priors(args.reach_priors)
        if args.history is None or args.snapshots is None:
            credential_status = load_keychain_credentials(ENGAGEMENT_AUDIT_PROFILE)
            missing = [
                variable
                for variable in credential_variables(ENGAGEMENT_AUDIT_PROFILE)
                if not credential_status[variable]
            ]
            if missing:
                raise ValueError(
                    "Missing engagement-audit credentials: " + ", ".join(missing)
                )
        history, snapshots, invalid_count = _load_inputs(args.history, args.snapshots)
        report = run_engagement_backtest(
            history,
            snapshots,
            reach_priors=priors,
            minimum_training_observations=args.minimum_train,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
            now=now,
            invalid_loaded_snapshots=invalid_count,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"engagement backtest failed: {exc}\n")
    print(
        json.dumps(report.as_dict(), indent=2, sort_keys=True)
        if args.json
        else _render_text(report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
