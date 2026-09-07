"""Read-only temporal evaluation for engagement-learning calibration."""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.engagement_learning import (
    EngagementModel,
    LearningConfig,
    _fit_outcome_calibration,
    _model_from_scored,
    _raw_observations,
    _RawObservation,
    _score_observations,
)
from src.insights_storage import parse_aware_timestamp


MATURE_SNAPSHOT_SLOTS = frozenset({72, 168})
SINGLE_POST_FEATURE_PREFIXES = (
    "artist:",
    "theme:",
    "museum:",
    "dominant_color:",
    "period_or_style:",
)


@dataclass(frozen=True)
class FoldResult:
    test_index: int
    test_posted_at: str
    training_observations: int
    predicted_score: float
    actual_score: float
    absolute_error: float
    model_confidence: float
    learned_influence: float


@dataclass(frozen=True)
class PerformanceMetrics:
    folds: int
    spearman: float | None
    pearson: float | None
    mae: float | None
    calibration_error: float | None
    ordering_accuracy: float | None
    oos_score: float | None
    oos_score_ci_low: float | None
    oos_score_ci_high: float | None


@dataclass(frozen=True)
class StabilityMetrics:
    theme_rank_agreement: float | None
    feature_rank_agreement: float | None
    bootstrap_feature_top10_overlap: float | None
    fold_prediction_adjustment_sd: float
    maximum_learned_adjustment: float
    sign_flip_rate: float
    sign_flip_comparisons: int


@dataclass(frozen=True)
class SinglePublicationRisk:
    maximum_feature_score_change: float
    maximum_observation_weight_share: float
    rating: str


@dataclass
class PriorEvaluation:
    reach_prior: float
    effective_observations: float
    confidence: float
    learned_influence: float
    confidence_growth_curve: tuple[float, ...]
    folds: tuple[FoldResult, ...]
    performance: PerformanceMetrics
    stability: StabilityMetrics
    single_publication_risk: SinglePublicationRisk
    verdict: str = "INSUFFICIENT DATA"

    @property
    def label(self) -> str:
        return "none" if self.reach_prior == 0 else f"{self.reach_prior:g}"


@dataclass(frozen=True)
class DatasetEvaluation:
    name: str
    usable_observations: int
    temporal_folds: int
    evaluations: tuple[PriorEvaluation, ...]


@dataclass(frozen=True)
class BacktestReport:
    generated_at: str
    total_publications: int
    carousel_publications: int
    usable_publications: int
    mature_publications: int
    provisional_publications: int
    reach_minimum: float | None
    reach_median: float | None
    reach_mean: float | None
    reach_maximum: float | None
    minimum_training_observations: int
    bootstrap_samples: int
    invalid_loaded_snapshots: int
    datasets: tuple[DatasetEvaluation, ...]
    recommendation: str
    evidence_strength: str
    leakage_controls: tuple[str, ...] = field(
        default=(
            "chronological expanding-window folds",
            "training publications strictly precede the held-out publication",
            "training snapshots must be captured by the held-out publication time",
            "outcome normalization is fitted on training observations only",
        )
    )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _TemporalFold:
    test: _RawObservation
    train: tuple[_RawObservation, ...]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _rank(values: Sequence[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    offset = 0
    while offset < len(ordered):
        end = offset + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[offset]]:
            end += 1
        average_rank = (offset + 1 + end) / 2
        for position in range(offset, end):
            result[ordered[position]] = average_rank
        offset = end
    return result


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_variance = sum((value - left_mean) ** 2 for value in left)
    right_variance = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator == 0:
        return None
    return max(-1.0, min(1.0, numerator / denominator))


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    return _pearson(_rank(left), _rank(right))


def _ordering_accuracy(
    predicted: Sequence[float], actual: Sequence[float]
) -> float | None:
    correct = 0.0
    comparisons = 0
    for first in range(len(actual)):
        for second in range(first + 1, len(actual)):
            actual_delta = actual[first] - actual[second]
            predicted_delta = predicted[first] - predicted[second]
            if actual_delta == 0:
                continue
            comparisons += 1
            if predicted_delta == 0:
                correct += 0.5
            elif (actual_delta > 0) == (predicted_delta > 0):
                correct += 1.0
    return correct / comparisons if comparisons else None


def _performance(folds: Sequence[FoldResult]) -> PerformanceMetrics:
    if not folds:
        return PerformanceMetrics(0, None, None, None, None, None, None, None, None)
    predicted = [fold.predicted_score for fold in folds]
    actual = [fold.actual_score for fold in folds]
    spearman = _spearman(predicted, actual)
    pearson = _pearson(predicted, actual)
    mae = _mean([abs(left - right) for left, right in zip(predicted, actual)])
    calibration_error = abs(_mean(predicted) - _mean(actual))
    ordering = _ordering_accuracy(predicted, actual)
    components = [
        max(0.0, 1.0 - mae / 50.0),
        max(0.0, 1.0 - calibration_error / 50.0),
        (spearman + 1.0) / 2.0 if spearman is not None else 0.5,
        (pearson + 1.0) / 2.0 if pearson is not None else 0.5,
        ordering if ordering is not None else 0.5,
    ]
    score = 100.0 * _mean(components)
    return PerformanceMetrics(
        folds=len(folds),
        spearman=spearman,
        pearson=pearson,
        mae=mae,
        calibration_error=calibration_error,
        ordering_accuracy=ordering,
        oos_score=score,
        oos_score_ci_low=None,
        oos_score_ci_high=None,
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _performance_with_bootstrap(
    folds: Sequence[FoldResult],
    *,
    samples: int,
    rng: random.Random,
) -> PerformanceMetrics:
    base = _performance(folds)
    if samples <= 0 or len(folds) < 3:
        return base
    scores = []
    for _ in range(samples):
        sampled = [rng.choice(folds) for _ in folds]
        score = _performance(sampled).oos_score
        if score is not None and math.isfinite(score):
            scores.append(score)
    if not scores:
        return base
    return PerformanceMetrics(
        **{
            **asdict(base),
            "oos_score_ci_low": _percentile(scores, 0.025),
            "oos_score_ci_high": _percentile(scores, 0.975),
        }
    )


def _history_before(
    history: Mapping[str, object], cutoff: datetime
) -> dict[str, object]:
    publications = history.get("publications", [])
    selected = (
        [
            publication
            for publication in publications
            if isinstance(publication, Mapping)
            and (posted_at := parse_aware_timestamp(publication.get("posted_at")))
            is not None
            and posted_at < cutoff
        ]
        if isinstance(publications, list)
        else []
    )
    publication_ids = {
        publication.get("id")
        for publication in selected
        if isinstance(publication.get("id"), str)
    }
    artworks = history.get("posted_artworks", [])
    selected_artworks = (
        [
            artwork
            for artwork in artworks
            if isinstance(artwork, Mapping)
            and artwork.get("publication_id") in publication_ids
        ]
        if isinstance(artworks, list)
        else []
    )
    return {**history, "publications": selected, "posted_artworks": selected_artworks}


def _snapshots_available_by(
    snapshots: Sequence[Mapping[str, object]], cutoff: datetime
) -> list[Mapping[str, object]]:
    return [
        snapshot
        for snapshot in snapshots
        if (captured_at := parse_aware_timestamp(snapshot.get("captured_at")))
        is not None
        and captured_at <= cutoff
    ]


def _temporal_folds(
    history: Mapping[str, object],
    snapshots: Sequence[Mapping[str, object]],
    labels: Sequence[_RawObservation],
    *,
    minimum_training_observations: int,
    mature_only: bool,
) -> tuple[_TemporalFold, ...]:
    folds = []
    for test in labels:
        train_history = _history_before(history, test.posted_at)
        available_snapshots = _snapshots_available_by(snapshots, test.posted_at)
        train = _raw_observations(
            train_history,
            available_snapshots,
            test.posted_at,
        ).observations
        if mature_only:
            train = tuple(
                item for item in train if item.target_age_hours in MATURE_SNAPSHOT_SLOTS
            )
        if len(train) >= minimum_training_observations:
            folds.append(_TemporalFold(test=test, train=tuple(train)))
    return tuple(folds)


def _ranked_keys(model: EngagementModel, prefix: str | None = None) -> list[str]:
    return [
        key
        for key, _ in sorted(
            (
                (key, estimate.score)
                for key, estimate in model.feature_estimates.items()
                if prefix is None or key.startswith(prefix)
            ),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _rank_agreement(left: Sequence[str], right: Sequence[str]) -> float | None:
    common = sorted(set(left) & set(right))
    if len(common) < 2:
        return None
    left_positions = {key: index for index, key in enumerate(left)}
    right_positions = {key: index for index, key in enumerate(right)}
    correct = 0
    comparisons = 0
    for first in range(len(common)):
        for second in range(first + 1, len(common)):
            left_delta = left_positions[common[first]] - left_positions[common[second]]
            right_delta = (
                right_positions[common[first]] - right_positions[common[second]]
            )
            comparisons += 1
            if (left_delta > 0) == (right_delta > 0):
                correct += 1
    return correct / comparisons if comparisons else None


def _average_optional(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return _mean(present) if present else None


def _top_overlap(
    left: Sequence[str], right: Sequence[str], limit: int = 10
) -> float | None:
    left_top = set(left[:limit])
    right_top = set(right[:limit])
    denominator = min(limit, len(left_top))
    return len(left_top & right_top) / denominator if denominator else None


def _bootstrap_feature_stability(
    scored: Sequence[Any],
    baseline_model: EngagementModel,
    config: LearningConfig,
    *,
    samples: int,
    rng: random.Random,
) -> float | None:
    baseline = _ranked_keys(baseline_model)
    if samples <= 0 or len(scored) < 2 or not baseline:
        return None
    overlaps = []
    for _ in range(samples):
        sampled = [rng.choice(scored) for _ in scored]
        model = _model_from_scored(sampled, config)
        overlap = _top_overlap(baseline, _ranked_keys(model))
        if overlap is not None:
            overlaps.append(overlap)
    return _mean(overlaps) if overlaps else None


def _stability(
    models: Sequence[EngagementModel],
    predictions: Sequence[float],
    scored: Sequence[Any],
    full_model: EngagementModel,
    config: LearningConfig,
    *,
    bootstrap_samples: int,
    rng: random.Random,
) -> StabilityMetrics:
    feature_agreements = []
    theme_agreements = []
    sign_flips = 0
    sign_comparisons = 0
    adjustments = []
    for model in (*models, full_model):
        adjustments.extend(
            abs(estimate.score - model.global_score)
            for estimate in model.feature_estimates.values()
        )
    for previous, current in zip(models, models[1:]):
        feature_agreements.append(
            _rank_agreement(_ranked_keys(previous), _ranked_keys(current))
        )
        theme_agreements.append(
            _rank_agreement(
                _ranked_keys(previous, "theme:"),
                _ranked_keys(current, "theme:"),
            )
        )
        common = set(previous.feature_estimates) & set(current.feature_estimates)
        for key in common:
            before = previous.feature_estimates[key].score - previous.global_score
            after = current.feature_estimates[key].score - current.global_score
            if abs(before) < 1e-9 or abs(after) < 1e-9:
                continue
            sign_comparisons += 1
            sign_flips += (before > 0) != (after > 0)
    prediction_adjustments = [
        prediction - model.global_score
        for prediction, model in zip(predictions, models)
    ]
    return StabilityMetrics(
        theme_rank_agreement=_average_optional(theme_agreements),
        feature_rank_agreement=_average_optional(feature_agreements),
        bootstrap_feature_top10_overlap=_bootstrap_feature_stability(
            scored,
            full_model,
            config,
            samples=bootstrap_samples,
            rng=rng,
        ),
        fold_prediction_adjustment_sd=(
            statistics.pstdev(prediction_adjustments)
            if len(prediction_adjustments) > 1
            else 0.0
        ),
        maximum_learned_adjustment=max(adjustments, default=0.0),
        sign_flip_rate=sign_flips / sign_comparisons if sign_comparisons else 0.0,
        sign_flip_comparisons=sign_comparisons,
    )


def _single_publication_risk(
    scored: Sequence[Any],
    full_model: EngagementModel,
    config: LearningConfig,
) -> SinglePublicationRisk:
    total_weight = sum(item.weight for item in scored)
    maximum_share = max((item.weight / total_weight for item in scored), default=0.0)
    maximum_change = 0.0
    for index, removed in enumerate(scored):
        reduced = [item for position, item in enumerate(scored) if position != index]
        reduced_model = _model_from_scored(reduced, config)
        for key in removed.feature_keys:
            if not key.startswith(SINGLE_POST_FEATURE_PREFIXES):
                continue
            baseline = full_model.feature_estimates.get(key)
            if baseline is None:
                continue
            comparison = reduced_model.feature_estimates.get(key)
            comparison_score = (
                comparison.score
                if comparison is not None
                else reduced_model.global_score
            )
            maximum_change = max(maximum_change, abs(baseline.score - comparison_score))
    if maximum_change >= 5.0 or maximum_share >= 0.25:
        rating = "HIGH"
    elif maximum_change >= 2.5 or maximum_share >= 0.15:
        rating = "MODERATE"
    else:
        rating = "LOW"
    return SinglePublicationRisk(maximum_change, maximum_share, rating)


def _seed_for(seed: int, dataset_name: str, reach_prior: float) -> int:
    material = f"{seed}:{dataset_name}:{reach_prior:g}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _evaluate_prior(
    observations: Sequence[_RawObservation],
    folds: Sequence[_TemporalFold],
    *,
    dataset_name: str,
    reach_prior: float,
    now: datetime,
    bootstrap_samples: int,
    seed: int,
) -> PriorEvaluation:
    config = LearningConfig(reach_confidence_prior=reach_prior)
    calibration = _fit_outcome_calibration(observations)
    scored = _score_observations(
        observations,
        now=now,
        config=config,
        calibration=calibration,
    )
    full_model = _model_from_scored(scored, config)
    fold_results = []
    fold_models = []
    for index, fold in enumerate(folds):
        fold_calibration = _fit_outcome_calibration(fold.train)
        train_scored = _score_observations(
            fold.train,
            now=fold.test.posted_at,
            config=config,
            calibration=fold_calibration,
        )
        model = _model_from_scored(train_scored, config)
        test_scored = _score_observations(
            [fold.test],
            now=fold.test.posted_at,
            config=config,
            calibration=fold_calibration,
        )
        if not test_scored:
            continue
        prediction = model.score_features(fold.test.feature_keys).score
        actual = test_scored[0].score
        fold_results.append(
            FoldResult(
                test_index=index,
                test_posted_at=fold.test.posted_at.isoformat(),
                training_observations=len(train_scored),
                predicted_score=prediction,
                actual_score=actual,
                absolute_error=abs(prediction - actual),
                model_confidence=model.confidence,
                learned_influence=model.config.mature_engagement_weight
                * model.confidence,
            )
        )
        fold_models.append(model)
    rng = random.Random(_seed_for(seed, dataset_name, reach_prior))
    performance = _performance_with_bootstrap(
        fold_results,
        samples=bootstrap_samples,
        rng=rng,
    )
    predictions = [fold.predicted_score for fold in fold_results]
    return PriorEvaluation(
        reach_prior=reach_prior,
        effective_observations=full_model.effective_observations,
        confidence=full_model.confidence,
        learned_influence=full_model.config.mature_engagement_weight
        * full_model.confidence,
        confidence_growth_curve=tuple(fold.model_confidence for fold in fold_results),
        folds=tuple(fold_results),
        performance=performance,
        stability=_stability(
            fold_models,
            predictions,
            scored,
            full_model,
            config,
            bootstrap_samples=bootstrap_samples,
            rng=rng,
        ),
        single_publication_risk=_single_publication_risk(scored, full_model, config),
    )


def _intervals_overlap(left: PerformanceMetrics, right: PerformanceMetrics) -> bool:
    if None in (
        left.oos_score_ci_low,
        left.oos_score_ci_high,
        right.oos_score_ci_low,
        right.oos_score_ci_high,
    ):
        return True
    return not (
        left.oos_score_ci_high < right.oos_score_ci_low
        or right.oos_score_ci_high < left.oos_score_ci_low
    )


def _apply_verdicts(datasets: Sequence[DatasetEvaluation]) -> tuple[str, str]:
    all_data = datasets[0]
    mature = datasets[1]
    baseline = next(
        (item for item in all_data.evaluations if item.reach_prior == 750),
        None,
    )
    mature_by_prior = {item.reach_prior: item for item in mature.evaluations}
    if baseline is None:
        return "INSUFFICIENT DATA", "WEAK"
    baseline.verdict = "KEEP"
    promising = []
    for candidate in all_data.evaluations:
        if candidate is baseline:
            continue
        if candidate.reach_prior == 0:
            candidate.verdict = "REJECT"
            continue
        mature_candidate = mature_by_prior.get(candidate.reach_prior)
        mature_baseline = mature_by_prior.get(750)
        scores_present = (
            candidate.performance.oos_score is not None
            and baseline.performance.oos_score is not None
            and mature_candidate is not None
            and mature_baseline is not None
            and mature_candidate.performance.oos_score is not None
            and mature_baseline.performance.oos_score is not None
        )
        enough_folds = (
            candidate.performance.folds >= 8
            and mature_candidate is not None
            and mature_candidate.performance.folds >= 5
        )
        if not scores_present or not enough_folds:
            candidate.verdict = "INSUFFICIENT DATA"
            continue
        delta = candidate.performance.oos_score - baseline.performance.oos_score
        mature_delta = (
            mature_candidate.performance.oos_score
            - mature_baseline.performance.oos_score
        )
        stable = (candidate.stability.bootstrap_feature_top10_overlap or 0.0) >= (
            baseline.stability.bootstrap_feature_top10_overlap or 0.0
        ) - 0.05
        safe = candidate.single_publication_risk.rating != "HIGH"
        intervals_overlap = _intervals_overlap(
            candidate.performance, baseline.performance
        )
        if (
            delta > 1.0
            and mature_delta > 0
            and stable
            and safe
            and not intervals_overlap
        ):
            candidate.verdict = "PROMISING"
            promising.append(candidate)
        elif delta < -1.0 or mature_delta < -1.0 or not safe:
            candidate.verdict = "REJECT"
        else:
            candidate.verdict = "INSUFFICIENT DATA"
    verdicts = {item.reach_prior: item.verdict for item in all_data.evaluations}
    for item in mature.evaluations:
        item.verdict = verdicts.get(item.reach_prior, "INSUFFICIENT DATA")
    if promising:
        best = max(promising, key=lambda item: item.performance.oos_score or -math.inf)
        return f"CHANGE TO K={best.reach_prior:g}", "MODERATE"
    if all_data.temporal_folds < 12 or mature.temporal_folds < 8:
        return "INSUFFICIENT DATA", "WEAK"
    if any(
        item.verdict == "INSUFFICIENT DATA"
        for item in all_data.evaluations
        if item.reach_prior not in {0, 750}
    ):
        return "INSUFFICIENT DATA", "WEAK"
    return "KEEP K=750", "MODERATE"


def run_engagement_backtest(
    history: Mapping[str, object],
    snapshots: Sequence[Mapping[str, object]],
    *,
    reach_priors: Sequence[float] = (750, 500, 250, 100, 0),
    minimum_training_observations: int = 8,
    bootstrap_samples: int = 500,
    seed: int = 20260907,
    now: datetime | None = None,
    invalid_loaded_snapshots: int = 0,
) -> BacktestReport:
    """Evaluate reach priors without mutating history, snapshots, or external state."""
    if minimum_training_observations < 2:
        raise ValueError("minimum_training_observations must be at least 2")
    if bootstrap_samples < 0:
        raise ValueError("bootstrap_samples cannot be negative")
    if not reach_priors or any(
        value < 0 or not math.isfinite(value) for value in reach_priors
    ):
        raise ValueError("reach_priors must contain finite non-negative values")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Backtest time must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc)
    current_snapshots = [
        snapshot
        for snapshot in snapshots
        if (captured_at := parse_aware_timestamp(snapshot.get("captured_at")))
        is not None
        and captured_at <= timestamp
    ]
    raw = _raw_observations(history, current_snapshots, timestamp)
    all_observations = raw.observations
    mature_observations = tuple(
        item
        for item in all_observations
        if item.target_age_hours in MATURE_SNAPSHOT_SLOTS
    )
    dataset_specs = (
        ("all_usable", all_observations, False),
        ("mature_only", mature_observations, True),
    )
    datasets = []
    for name, observations, mature_only in dataset_specs:
        folds = _temporal_folds(
            history,
            current_snapshots,
            observations,
            minimum_training_observations=minimum_training_observations,
            mature_only=mature_only,
        )
        evaluations = tuple(
            _evaluate_prior(
                observations,
                folds,
                dataset_name=name,
                reach_prior=reach_prior,
                now=timestamp,
                bootstrap_samples=bootstrap_samples,
                seed=seed,
            )
            for reach_prior in dict.fromkeys(float(value) for value in reach_priors)
        )
        datasets.append(
            DatasetEvaluation(
                name=name,
                usable_observations=len(observations),
                temporal_folds=len(folds),
                evaluations=evaluations,
            )
        )
    recommendation, evidence_strength = _apply_verdicts(datasets)
    reaches = [item.reach for item in all_observations]
    return BacktestReport(
        generated_at=timestamp.isoformat(),
        total_publications=raw.total_publication_records,
        carousel_publications=raw.carousel_publications,
        usable_publications=len(all_observations),
        mature_publications=len(mature_observations),
        provisional_publications=len(all_observations) - len(mature_observations),
        reach_minimum=min(reaches) if reaches else None,
        reach_median=statistics.median(reaches) if reaches else None,
        reach_mean=_mean(reaches) if reaches else None,
        reach_maximum=max(reaches) if reaches else None,
        minimum_training_observations=minimum_training_observations,
        bootstrap_samples=bootstrap_samples,
        invalid_loaded_snapshots=invalid_loaded_snapshots,
        datasets=tuple(datasets),
        recommendation=recommendation,
        evidence_strength=evidence_strength,
    )
