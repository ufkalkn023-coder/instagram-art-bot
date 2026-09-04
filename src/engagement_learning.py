"""Explainable, deterministic learning from carousel-level Instagram Insights.

Instagram exposes post-level outcomes, not slide-level attribution. This module
therefore learns only repeated publication and artwork-set features and shrinks
every estimate toward the account baseline.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from src.insights_storage import parse_aware_timestamp


MODEL_VERSION = "engagement_rates_v1"
MATURE_SNAPSHOT_PREFERENCE = (72, 168, 24)
SNAPSHOT_CONFIDENCE = {24: 0.45, 72: 1.0, 168: 1.0}
OUTCOME_WEIGHTS = {
    "share_rate": 0.40,
    "save_rate": 0.30,
    "comment_rate": 0.15,
    "like_rate": 0.10,
    "reach_signal": 0.05,
}


@dataclass(frozen=True)
class LearningConfig:
    prior_effective_observations: float = 4.0
    global_confidence_observations: float = 8.0
    minimum_feature_observations: int = 2
    reach_confidence_prior: float = 750.0
    recency_half_life_days: float = 180.0
    exploration_rate: float = 0.10
    mature_engagement_weight: float = 0.45
    exploration_weight: float = 0.10


@dataclass(frozen=True)
class FeatureEstimate:
    score: float
    observed_score: float
    confidence: float
    observations: int
    effective_observations: float


@dataclass(frozen=True)
class FeatureContribution:
    key: str
    score: float
    confidence: float
    observations: int


@dataclass(frozen=True)
class EngagementPrediction:
    score: float
    confidence: float
    contributions: tuple[FeatureContribution, ...] = ()


@dataclass(frozen=True)
class SelectionComponents:
    final_score: float
    quality_component: float
    engagement_component: float
    diversity_component: float
    exploration_component: float
    learned_score: float
    engagement_confidence: float


@dataclass(frozen=True)
class _RawObservation:
    publication_id: str
    posted_at: datetime
    reach: float
    components: Mapping[str, float]
    maturity_weight: float
    feature_keys: tuple[str, ...]


@dataclass(frozen=True)
class _ScoredObservation:
    score: float
    weight: float
    feature_keys: tuple[str, ...]


@dataclass(frozen=True)
class EngagementModel:
    global_score: float = 50.0
    confidence: float = 0.0
    useful_publications: int = 0
    effective_observations: float = 0.0
    feature_estimates: Mapping[str, FeatureEstimate] = field(default_factory=dict)
    config: LearningConfig = field(default_factory=LearningConfig)
    version: str = MODEL_VERSION

    @classmethod
    def cold_start(cls, config: LearningConfig | None = None) -> "EngagementModel":
        return cls(config=config or LearningConfig())

    def score_features(self, feature_keys: Iterable[str]) -> EngagementPrediction:
        estimates = [
            (key, self.feature_estimates[key])
            for key in sorted(set(feature_keys))
            if key in self.feature_estimates
        ]
        if not estimates:
            return EngagementPrediction(self.global_score, 0.0)
        # Posterior scores already include global shrinkage. Confidence weights
        # decide how much each repeated feature may influence the prediction.
        total_weight = sum(max(0.05, estimate.confidence) for _, estimate in estimates)
        score = sum(
            estimate.score * max(0.05, estimate.confidence)
            for _, estimate in estimates
        ) / total_weight
        confidence = min(
            self.confidence,
            sum(estimate.confidence for _, estimate in estimates) / len(estimates),
        )
        contributions = tuple(
            FeatureContribution(
                key=key,
                score=estimate.score,
                confidence=estimate.confidence,
                observations=estimate.observations,
            )
            for key, estimate in sorted(
                estimates,
                key=lambda item: (-item[1].confidence, item[0]),
            )[:12]
        )
        return EngagementPrediction(round(score, 4), round(confidence, 4), contributions)

    def score_candidate(
        self,
        artwork: Mapping[str, object],
        context: Mapping[str, object],
    ) -> EngagementPrediction:
        return self.score_features(
            (*candidate_feature_keys(artwork), *context_feature_keys(context))
        )

    def score_set(
        self,
        artworks: Sequence[Mapping[str, object]],
        context: Mapping[str, object],
    ) -> EngagementPrediction:
        enriched_context = {**context, "featured_count": len(artworks)}
        keys = list(context_feature_keys(enriched_context))
        for artwork in artworks:
            keys.extend(candidate_feature_keys(artwork))
        return self.score_features(keys)

    def exploration_selected(self, run_seed: str) -> bool:
        material = f"{run_seed}\x1fengagement_exploration".encode()
        sample = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / 2**64
        return sample < self.config.exploration_rate

    def blend_candidate_score(
        self,
        *,
        quality_editorial_score: float,
        prediction: EngagementPrediction,
        exploration_selected: bool,
        diversity_component: float = 0.0,
    ) -> SelectionComponents:
        engagement_weight = self.config.mature_engagement_weight * self.confidence
        exploration_weight = self.config.exploration_weight if exploration_selected else 0.0
        quality_weight = max(0.0, 1.0 - engagement_weight - exploration_weight)
        novelty = 100.0 * (1.0 - prediction.confidence)
        quality_component = quality_weight * _bounded(quality_editorial_score)
        engagement_component = engagement_weight * _bounded(prediction.score)
        exploration_component = exploration_weight * novelty
        bounded_diversity = max(-5.0, min(5.0, diversity_component))
        final = quality_component + engagement_component + exploration_component + bounded_diversity
        return SelectionComponents(
            final_score=round(_bounded(final), 4),
            quality_component=round(quality_component, 4),
            engagement_component=round(engagement_component, 4),
            diversity_component=round(bounded_diversity, 4),
            exploration_component=round(exploration_component, 4),
            learned_score=round(prediction.score, 4),
            engagement_confidence=round(prediction.confidence, 4),
        )

    def rank_themes(
        self,
        themes: Sequence[object],
        *,
        base_scores: Mapping[str, float],
        context: Mapping[str, object],
        run_seed: str,
        exploration_selected: bool,
    ) -> tuple[object, ...]:
        if self.confidence <= 0:
            return tuple(themes)
        ranked = []
        for original_index, theme in enumerate(themes):
            theme_id = str(getattr(theme, "id"))
            score = self.theme_score(
                theme,
                base_score=base_scores.get(theme_id, 50.0),
                context=context,
                exploration_selected=exploration_selected,
            )
            tie = _stable_unit(run_seed, f"theme-learning:{theme_id}")
            ranked.append((-score, -tie, original_index, theme))
        return tuple(item[3] for item in sorted(ranked))

    def theme_score(
        self,
        theme: object,
        *,
        base_score: float,
        context: Mapping[str, object],
        exploration_selected: bool,
    ) -> float:
        """Expose the unchanged engagement-adjusted score for attempt planning."""
        theme_id = str(getattr(theme, "id"))
        theme_format = getattr(getattr(theme, "format", None), "value", None)
        prediction = self.score_features(
            context_feature_keys(
                {
                    **context,
                    "carousel_theme": theme_id,
                    "carousel_format": theme_format,
                }
            )
        )
        engagement_weight = self.config.mature_engagement_weight * self.confidence
        exploration_weight = (
            self.config.exploration_weight if exploration_selected else 0.0
        )
        base_weight = 1.0 - engagement_weight - exploration_weight
        base = _bounded(base_score)
        novelty = 100.0 * (1.0 - prediction.confidence)
        return (
            base_weight * base
            + engagement_weight * prediction.score
            + exploration_weight * novelty
        )


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, value))


def _stable_unit(seed: str, namespace: str) -> float:
    material = f"{seed}\x1f{namespace}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / 2**64


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _known(value: object) -> str | None:
    normalized = " ".join(str(value or "").split()).casefold()
    return normalized if normalized not in {"", "unknown", "none", "n/a"} else None


def _feature(kind: str, value: object) -> str | None:
    normalized = _known(value)
    return f"{kind}:{normalized}" if normalized else None


def _source_from_artwork(artwork: Mapping[str, object]) -> str | None:
    explicit = _known(artwork.get("source"))
    if explicit:
        return explicit
    canonical_id = artwork.get("id")
    if isinstance(canonical_id, str) and "_" in canonical_id:
        return canonical_id.split("_", 1)[0].casefold()
    return None


def candidate_feature_keys(artwork: Mapping[str, object]) -> tuple[str, ...]:
    visual = artwork.get("visual_features")
    visual_color = artwork.get("visual_color_family")
    visual_luminance = artwork.get("visual_tone")
    if visual is not None:
        visual_color = getattr(getattr(visual, "dominant_color_family", None), "value", visual_color)
        visual_luminance = getattr(getattr(visual, "luminance_bucket", None), "value", visual_luminance)
    values = (
        _feature("artist", artwork.get("artist", artwork.get("artist_name"))),
        _feature("artist_group", artwork.get("artist_group")),
        _feature("region", artwork.get("region")),
        _feature("style_period", artwork.get("style_or_period", artwork.get("period"))),
        _feature("semantic_family", artwork.get("semantic_family", artwork.get("visual_category"))),
        _feature("museum", artwork.get("museum", artwork.get("museum_name"))),
        _feature("source", _source_from_artwork(artwork)),
        _feature("visual_color", visual_color),
        _feature("visual_luminance", visual_luminance),
        _feature("orientation", artwork.get("published_orientation")),
    )
    return tuple(dict.fromkeys(value for value in values if value))


def context_feature_keys(context: Mapping[str, object]) -> tuple[str, ...]:
    values = (
        _feature("theme", context.get("carousel_theme", context.get("theme"))),
        _feature("format", context.get("carousel_format")),
        _feature("featured_count", context.get("featured_count")),
        _feature("cover_variant", context.get("cover_variant")),
        _feature("caption_hook", context.get("caption_hook_type")),
        _feature("publish_slot", context.get("publish_slot")),
        _feature("weekday", context.get("publication_weekday")),
        _feature("preceding_distance", context.get("preceding_distance_bucket")),
    )
    return tuple(value for value in values if value)


def select_mature_snapshot(snapshots: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """Prefer a comparable 72h snapshot, then final 168h, then provisional 24h."""
    for target in MATURE_SNAPSHOT_PREFERENCE:
        matching = [
            snapshot
            for snapshot in snapshots
            if snapshot.get("target_age_hours") == target
            and isinstance(snapshot.get("metrics"), Mapping)
        ]
        if matching:
            return max(
                matching,
                key=lambda snapshot: str(snapshot.get("captured_at") or ""),
            )
    return None


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _snapshot_components(metrics: Mapping[str, object]) -> tuple[float, dict[str, float]] | None:
    reach = _number(metrics.get("reach"))
    if reach is None or reach <= 0:
        return None
    components: dict[str, float] = {"reach_signal": reach}
    for metric, output_name in (
        ("shares", "share_rate"),
        ("saved", "save_rate"),
        ("comments", "comment_rate"),
        ("likes", "like_rate"),
    ):
        value = _number(metrics.get(metric))
        if value is not None:
            components[output_name] = value / reach
    if len(components) == 1:
        return None
    return reach, components


def _preceding_distance_bucket(minutes: float | None) -> str | None:
    if minutes is None or minutes < 0:
        return None
    if minutes < 180:
        return "under_3h"
    if minutes < 360:
        return "3h_to_6h"
    if minutes < 720:
        return "6h_to_12h"
    return "12h_plus"


def _publication_feature_keys(
    publication: Mapping[str, object],
    artworks: Sequence[Mapping[str, object]],
    posted_at: datetime,
    preceding_minutes: float | None,
) -> tuple[str, ...]:
    context = {
        "carousel_theme": publication.get("carousel_theme", publication.get("theme")),
        "carousel_format": publication.get("carousel_format"),
        "featured_count": publication.get("featured_count") or max(0, len(artworks) - 1),
        "cover_variant": publication.get("cover_variant"),
        "caption_hook_type": publication.get("caption_hook_type"),
        "publish_slot": publication.get("publish_slot"),
        "publication_weekday": posted_at.strftime("%A").casefold(),
        "preceding_distance_bucket": _preceding_distance_bucket(preceding_minutes),
    }
    keys = list(context_feature_keys(context))
    for artwork in artworks:
        if str(artwork.get("publication_role", "")).upper() == "COVER":
            continue
        keys.extend(candidate_feature_keys(artwork))
    return tuple(dict.fromkeys(keys))


def _raw_observations(
    history: Mapping[str, object],
    snapshots: Sequence[Mapping[str, object]],
    now: datetime,
) -> list[_RawObservation]:
    raw_publications = history.get("publications", [])
    raw_artworks = history.get("posted_artworks", [])
    if not isinstance(raw_publications, list) or not isinstance(raw_artworks, list):
        return []
    artworks_by_publication: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for artwork in raw_artworks:
        if not isinstance(artwork, Mapping):
            continue
        publication_id = artwork.get("publication_id")
        if isinstance(publication_id, str):
            artworks_by_publication[publication_id].append(artwork)
    snapshots_by_identity: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        for identity in (snapshot.get("publication_id"), snapshot.get("media_id")):
            if isinstance(identity, str) and identity:
                snapshots_by_identity[identity].append(snapshot)

    valid_publications: list[tuple[datetime, Mapping[str, object]]] = []
    for publication in raw_publications:
        if not isinstance(publication, Mapping) or publication.get("type") != "carousel":
            continue
        posted_at = parse_aware_timestamp(publication.get("posted_at"))
        if posted_at is None or posted_at > now:
            continue
        publication_id = publication.get("id")
        media_id = publication.get("media_id")
        if not isinstance(publication_id, str) or not isinstance(media_id, str):
            continue
        valid_publications.append((posted_at, publication))
    valid_publications.sort(key=lambda item: (item[0], str(item[1].get("id"))))

    result: list[_RawObservation] = []
    previous_posted_at: datetime | None = None
    for posted_at, publication in valid_publications:
        publication_id = str(publication["id"])
        candidates = [
            *snapshots_by_identity.get(publication_id, ()),
            *snapshots_by_identity.get(str(publication["media_id"]), ()),
        ]
        # A snapshot indexed by both identities must still be considered once.
        unique = {(id(snapshot), str(snapshot.get("captured_at"))): snapshot for snapshot in candidates}
        snapshot = select_mature_snapshot(tuple(unique.values()))
        preceding = (
            (posted_at - previous_posted_at).total_seconds() / 60
            if previous_posted_at is not None
            else None
        )
        previous_posted_at = posted_at
        if snapshot is None:
            continue
        metrics = snapshot.get("metrics")
        parsed = _snapshot_components(metrics) if isinstance(metrics, Mapping) else None
        if parsed is None:
            continue
        reach, components = parsed
        target = snapshot.get("target_age_hours")
        maturity = SNAPSHOT_CONFIDENCE.get(target)
        if maturity is None:
            continue
        result.append(
            _RawObservation(
                publication_id=publication_id,
                posted_at=posted_at,
                reach=reach,
                components=components,
                maturity_weight=maturity,
                feature_keys=_publication_feature_keys(
                    publication,
                    artworks_by_publication.get(publication_id, ()),
                    posted_at,
                    preceding,
                ),
            )
        )
    return result


def _score_observations(
    observations: Sequence[_RawObservation],
    *,
    now: datetime,
    config: LearningConfig,
) -> list[_ScoredObservation]:
    if not observations:
        return []
    baselines: dict[str, float] = {}
    bounds: dict[str, tuple[float, float]] = {}
    for component in OUTCOME_WEIGHTS:
        values = [item.components[component] for item in observations if component in item.components]
        if not values:
            continue
        lower = _percentile(values, 0.05)
        upper = _percentile(values, 0.95)
        clipped = [max(lower, min(upper, value)) for value in values]
        baselines[component] = sum(clipped) / len(clipped)
        bounds[component] = (lower, upper)

    scored: list[_ScoredObservation] = []
    for observation in observations:
        weighted_scores = []
        available_weight = 0.0
        for component, configured_weight in OUTCOME_WEIGHTS.items():
            if component not in observation.components or component not in baselines:
                continue
            lower, upper = bounds[component]
            value = max(lower, min(upper, observation.components[component]))
            baseline = baselines[component]
            if baseline <= 0:
                normalized = 50.0 if value <= 0 else 75.0
            else:
                normalized = _bounded(
                    50.0 + 20.0 * math.log2((value + baseline * 0.05) / (baseline * 1.05))
                )
            weighted_scores.append(normalized * configured_weight)
            available_weight += configured_weight
        if available_weight <= 0:
            continue
        outcome = sum(weighted_scores) / available_weight
        age_days = max(0.0, (now - observation.posted_at).total_seconds() / 86400)
        recency = 0.5 ** (age_days / config.recency_half_life_days)
        reach_confidence = observation.reach / (
            observation.reach + config.reach_confidence_prior
        )
        metric_coverage = available_weight / sum(OUTCOME_WEIGHTS.values())
        weight = (
            observation.maturity_weight
            * recency
            * reach_confidence
            * (0.5 + 0.5 * metric_coverage)
        )
        if weight > 0:
            scored.append(_ScoredObservation(outcome, weight, observation.feature_keys))
    return scored


def build_engagement_model(
    history: Mapping[str, object] | None,
    snapshots: Sequence[Mapping[str, object]] | None,
    *,
    now: datetime | None = None,
    config: LearningConfig | None = None,
) -> EngagementModel:
    """Build a rebuildable model from canonical history and Insights snapshots."""
    active_config = config or LearningConfig()
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Learning time must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc)
    if not isinstance(history, Mapping) or not snapshots:
        return EngagementModel.cold_start(active_config)
    observations = _raw_observations(history, snapshots, timestamp)
    scored = _score_observations(observations, now=timestamp, config=active_config)
    if not scored:
        return EngagementModel.cold_start(active_config)
    total_weight = sum(item.weight for item in scored)
    global_score = sum(item.score * item.weight for item in scored) / total_weight
    global_confidence = total_weight / (
        total_weight + active_config.global_confidence_observations
    )
    values_by_feature: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in scored:
        for key in item.feature_keys:
            values_by_feature[key].append((item.score, item.weight))
    estimates: dict[str, FeatureEstimate] = {}
    for key, values in values_by_feature.items():
        effective = sum(weight for _, weight in values)
        observed = sum(score * weight for score, weight in values) / effective
        confidence = effective / (
            effective + active_config.prior_effective_observations
        )
        confidence *= min(
            1.0,
            len(values) / active_config.minimum_feature_observations,
        )
        posterior = confidence * observed + (1.0 - confidence) * global_score
        estimates[key] = FeatureEstimate(
            score=round(_bounded(posterior), 4),
            observed_score=round(_bounded(observed), 4),
            confidence=round(confidence, 4),
            observations=len(values),
            effective_observations=round(effective, 4),
        )
    return EngagementModel(
        global_score=round(_bounded(global_score), 4),
        confidence=round(global_confidence, 4),
        useful_publications=len(scored),
        effective_observations=round(total_weight, 4),
        feature_estimates=estimates,
        config=active_config,
    )
