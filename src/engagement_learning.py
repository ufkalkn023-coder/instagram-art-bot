"""Explainable, deterministic learning from carousel-level Instagram Insights.

Instagram exposes post-level outcomes, not slide-level attribution. This module
therefore learns only repeated publication and artwork-set features and shrinks
every estimate toward the account baseline.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from src.engagement_features import EngagementFeatureVector
from src.insights_storage import parse_aware_timestamp
from src.insights_snapshot import learning_snapshot_components


MODEL_VERSION = "engagement_rates_v2"
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
    target_age_hours: int
    reach: float
    components: Mapping[str, float]
    maturity_weight: float
    feature_keys: tuple[str, ...]


@dataclass(frozen=True)
class _ScoredObservation:
    publication_id: str
    target_age_hours: int
    reach: float
    score: float
    weight: float
    maturity_factor: float
    recency_factor: float
    reach_confidence_factor: float
    metric_coverage_factor: float
    feature_keys: tuple[str, ...]


@dataclass(frozen=True)
class ObservationDiagnostic:
    """Secret-free factors that exactly explain one observation's weight."""

    publication_identifier: str
    selected_target_age_hours: int
    reach: float
    maturity_factor: float
    recency_factor: float
    reach_confidence_factor: float
    metric_coverage_factor: float
    final_observation_weight: float


@dataclass(frozen=True)
class EngagementAudit:
    """Read-only funnel and weight diagnostics for engagement learning."""

    model: "EngagementModel"
    total_publication_records: int = 0
    carousel_publications: int = 0
    single_publications: int = 0
    valid_publication_media_identities: int = 0
    publications_with_snapshots: int = 0
    snapshot_slot_publications: Mapping[int, int] = field(default_factory=dict)
    eligible_learning_observations: int = 0
    excluded_by_reason: Mapping[str, int] = field(default_factory=dict)
    selected_snapshot_slot_counts: Mapping[int, int] = field(default_factory=dict)
    average_reach: float | None = None
    minimum_reach: float | None = None
    maximum_reach: float | None = None
    mature_observations: int = 0
    provisional_observations: int = 0
    effective_observations: float = 0.0
    global_confidence: float = 0.0
    observations: tuple[ObservationDiagnostic, ...] = ()


@dataclass(frozen=True)
class EngagementModel:
    global_score: float = 50.0
    confidence: float = 0.0
    useful_publications: int = 0
    effective_observations: float = 0.0
    feature_estimates: Mapping[str, FeatureEstimate] = field(default_factory=dict)
    config: LearningConfig = field(default_factory=LearningConfig)
    version: str = MODEL_VERSION

    @property
    def useful_carousel_observations(self) -> int:
        """Unambiguous name for the historical ``useful_publications`` field."""
        return self.useful_publications

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

    def can_influence_selection(self, *, exploration_selected: bool) -> bool:
        """Return whether learning or explicit exploration may change a baseline."""
        return self.confidence > 0 or exploration_selected

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


def candidate_feature_keys(artwork: Mapping[str, object]) -> tuple[str, ...]:
    return EngagementFeatureVector.from_candidate(artwork).candidate_feature_keys()


def context_feature_keys(context: Mapping[str, object]) -> tuple[str, ...]:
    return EngagementFeatureVector.from_context(context).context_feature_keys()


def select_mature_snapshot(snapshots: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """Prefer 72h, then 168h, then 24h, while skipping unusable snapshots."""
    for target in MATURE_SNAPSHOT_PREFERENCE:
        matching = [
            snapshot
            for snapshot in snapshots
            if snapshot.get("target_age_hours") == target
            and isinstance(snapshot.get("metrics"), Mapping)
            and learning_snapshot_components(snapshot["metrics"]) is not None
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


def _publication_feature_keys(
    publication: Mapping[str, object],
    artworks: Sequence[Mapping[str, object]],
    posted_at: datetime,
    preceding_minutes: float | None,
) -> tuple[str, ...]:
    recorded_preceding = publication.get("preceding_post_distance_minutes")
    preceding_value = (
        recorded_preceding
        if isinstance(recorded_preceding, (int, float))
        and not isinstance(recorded_preceding, bool)
        and recorded_preceding >= 0
        else preceding_minutes
    )
    context = {
        "engagement_features": publication.get("engagement_features"),
        "carousel_theme": publication.get("carousel_theme", publication.get("theme")),
        "carousel_format": publication.get("carousel_format"),
        "featured_count": publication.get("featured_count") or max(0, len(artworks) - 1),
        "cover_variant": publication.get("cover_variant"),
        "caption_hook_type": publication.get("caption_hook_type"),
        "publish_slot": publication.get("publish_slot"),
        "publication_weekday": posted_at.strftime("%A").casefold(),
        "previous_post_spacing_bucket": publication.get(
            "previous_post_spacing_bucket"
        ),
        "preceding_post_distance_minutes": preceding_value,
    }
    keys = list(context_feature_keys(context))
    for artwork in artworks:
        if str(artwork.get("publication_role", "")).upper() == "COVER":
            continue
        keys.extend(candidate_feature_keys(artwork))
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class _RawObservationResult:
    observations: tuple[_RawObservation, ...]
    total_publication_records: int
    carousel_publications: int
    single_publications: int
    valid_publication_media_identities: int
    publications_with_snapshots: int
    snapshot_slot_publications: Mapping[int, int]
    excluded_by_reason: Mapping[str, int]


def _raw_observations(
    history: Mapping[str, object],
    snapshots: Sequence[Mapping[str, object]],
    now: datetime,
) -> _RawObservationResult:
    raw_publications = history.get("publications", [])
    raw_artworks = history.get("posted_artworks", [])
    if not isinstance(raw_publications, list) or not isinstance(raw_artworks, list):
        return _RawObservationResult((), 0, 0, 0, 0, 0, {}, {"malformed_history": 1})
    exclusions: Counter[str] = Counter()
    artworks_by_publication: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for artwork in raw_artworks:
        if not isinstance(artwork, Mapping):
            continue
        publication_id = artwork.get("publication_id")
        if isinstance(publication_id, str):
            artworks_by_publication[publication_id].append(artwork)

    carousel_publications = sum(
        isinstance(item, Mapping) and item.get("type") == "carousel"
        for item in raw_publications
    )
    single_publications = sum(
        isinstance(item, Mapping) and item.get("type") == "single"
        for item in raw_publications
    )
    publication_media: dict[str, str] = {}
    media_publication: dict[str, str] = {}
    identity_validity: dict[int, bool] = {}
    valid_identity_count = 0
    for publication in raw_publications:
        if not isinstance(publication, Mapping):
            exclusions["malformed_publication_record"] += 1
            continue
        publication_id = publication.get("id")
        media_id = publication.get("media_id")
        if not (
            isinstance(publication_id, str)
            and publication_id.strip()
            and isinstance(media_id, str)
            and media_id.strip()
        ):
            identity_validity[id(publication)] = False
            exclusions["invalid_publication_media_identity"] += 1
            continue
        publication_id = publication_id.strip()
        media_id = media_id.strip()
        if publication_id in publication_media or media_id in media_publication:
            identity_validity[id(publication)] = False
            exclusions["duplicate_publication_media_identity"] += 1
            continue
        publication_media[publication_id] = media_id
        media_publication[media_id] = publication_id
        identity_validity[id(publication)] = True
        valid_identity_count += 1

    snapshots_by_pair: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            exclusions["malformed_snapshot"] += 1
            continue
        snapshot_publication = snapshot.get("publication_id")
        snapshot_media = snapshot.get("media_id")
        if not (
            isinstance(snapshot_publication, str)
            and snapshot_publication
            and isinstance(snapshot_media, str)
            and snapshot_media
        ):
            exclusions["malformed_snapshot_identity"] += 1
            continue
        snapshot_publication = snapshot_publication.strip()
        snapshot_media = snapshot_media.strip()
        authoritative_media = publication_media.get(snapshot_publication)
        authoritative_publication = media_publication.get(snapshot_media)
        if (
            authoritative_media is not None
            and authoritative_media == snapshot_media
            and authoritative_publication == snapshot_publication
        ):
            snapshots_by_pair[(snapshot_publication, snapshot_media)].append(snapshot)
        elif authoritative_media is not None or authoritative_publication is not None:
            exclusions["snapshot_identity_mismatch"] += 1

    valid_publications: list[tuple[datetime, Mapping[str, object]]] = []
    for publication in raw_publications:
        if not isinstance(publication, Mapping) or publication.get("type") != "carousel":
            continue
        if not identity_validity.get(id(publication), False):
            continue
        posted_at = parse_aware_timestamp(publication.get("posted_at"))
        if posted_at is None or posted_at > now:
            exclusions[
                "future_publication" if posted_at is not None else "invalid_publication_timestamp"
            ] += 1
            continue
        valid_publications.append((posted_at, publication))
    valid_publications.sort(key=lambda item: (item[0], str(item[1].get("id"))))

    result: list[_RawObservation] = []
    publications_with_snapshots = 0
    slot_publications: Counter[int] = Counter()
    previous_posted_at: datetime | None = None
    for posted_at, publication in valid_publications:
        publication_id = str(publication["id"]).strip()
        media_id = str(publication["media_id"]).strip()
        candidates = snapshots_by_pair.get((publication_id, media_id), ())
        if candidates:
            publications_with_snapshots += 1
            slot_publications.update(
                {
                    target
                    for candidate in candidates
                    if isinstance((target := candidate.get("target_age_hours")), int)
                    and not isinstance(target, bool)
                }
            )
        snapshot = select_mature_snapshot(candidates)
        preceding = (
            (posted_at - previous_posted_at).total_seconds() / 60
            if previous_posted_at is not None
            else None
        )
        previous_posted_at = posted_at
        if snapshot is None:
            exclusions["no_usable_snapshot" if candidates else "no_snapshot"] += 1
            continue
        metrics = snapshot.get("metrics")
        parsed = learning_snapshot_components(metrics) if isinstance(metrics, Mapping) else None
        if parsed is None:
            exclusions["no_usable_snapshot"] += 1
            continue
        reach, components = parsed
        target = snapshot.get("target_age_hours")
        maturity = SNAPSHOT_CONFIDENCE.get(target)
        if maturity is None:
            exclusions["unsupported_snapshot_slot"] += 1
            continue
        result.append(
            _RawObservation(
                publication_id=publication_id,
                posted_at=posted_at,
                target_age_hours=target,
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
    return _RawObservationResult(
        observations=tuple(result),
        total_publication_records=len(raw_publications),
        carousel_publications=carousel_publications,
        single_publications=single_publications,
        valid_publication_media_identities=valid_identity_count,
        publications_with_snapshots=publications_with_snapshots,
        snapshot_slot_publications=dict(sorted(slot_publications.items())),
        excluded_by_reason=dict(sorted(exclusions.items())),
    )


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
            scored.append(
                _ScoredObservation(
                    publication_id=observation.publication_id,
                    target_age_hours=observation.target_age_hours,
                    reach=observation.reach,
                    score=outcome,
                    weight=weight,
                    maturity_factor=observation.maturity_weight,
                    recency_factor=recency,
                    reach_confidence_factor=reach_confidence,
                    metric_coverage_factor=0.5 + 0.5 * metric_coverage,
                    feature_keys=observation.feature_keys,
                )
            )
    return scored


def _model_from_scored(
    scored: Sequence[_ScoredObservation],
    active_config: LearningConfig,
) -> EngagementModel:
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


def _anonymized_publication_identifier(publication_id: str) -> str:
    return hashlib.sha256(publication_id.encode()).hexdigest()[:12]


def analyze_engagement_learning(
    history: Mapping[str, object] | None,
    snapshots: Sequence[Mapping[str, object]] | None,
    *,
    now: datetime | None = None,
    config: LearningConfig | None = None,
) -> EngagementAudit:
    """Build the model and a read-only, self-explaining learning funnel."""
    active_config = config or LearningConfig()
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Learning time must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc)
    if not isinstance(history, Mapping):
        model = EngagementModel.cold_start(active_config)
        return EngagementAudit(model=model, excluded_by_reason={"malformed_history": 1})
    snapshot_values = snapshots if isinstance(snapshots, Sequence) else ()
    raw = _raw_observations(history, snapshot_values, timestamp)
    scored = _score_observations(
        raw.observations,
        now=timestamp,
        config=active_config,
    )
    model = _model_from_scored(scored, active_config)
    total_weight = sum(item.weight for item in scored)
    exact_confidence = (
        total_weight / (total_weight + active_config.global_confidence_observations)
        if total_weight > 0
        else 0.0
    )
    selected_slots = Counter(item.target_age_hours for item in scored)
    reaches = [item.reach for item in scored]
    exclusions = Counter(raw.excluded_by_reason)
    zero_weight_count = len(raw.observations) - len(scored)
    if zero_weight_count:
        exclusions["zero_observation_weight"] += zero_weight_count
    observation_diagnostics = tuple(
        ObservationDiagnostic(
            publication_identifier=_anonymized_publication_identifier(item.publication_id),
            selected_target_age_hours=item.target_age_hours,
            reach=item.reach,
            maturity_factor=item.maturity_factor,
            recency_factor=item.recency_factor,
            reach_confidence_factor=item.reach_confidence_factor,
            metric_coverage_factor=item.metric_coverage_factor,
            final_observation_weight=item.weight,
        )
        for item in scored
    )
    return EngagementAudit(
        model=model,
        total_publication_records=raw.total_publication_records,
        carousel_publications=raw.carousel_publications,
        single_publications=raw.single_publications,
        valid_publication_media_identities=raw.valid_publication_media_identities,
        publications_with_snapshots=raw.publications_with_snapshots,
        snapshot_slot_publications=raw.snapshot_slot_publications,
        eligible_learning_observations=len(scored),
        excluded_by_reason=dict(sorted(exclusions.items())),
        selected_snapshot_slot_counts=dict(sorted(selected_slots.items())),
        average_reach=sum(reaches) / len(reaches) if reaches else None,
        minimum_reach=min(reaches) if reaches else None,
        maximum_reach=max(reaches) if reaches else None,
        mature_observations=sum(item.target_age_hours in {72, 168} for item in scored),
        provisional_observations=sum(item.target_age_hours == 24 for item in scored),
        effective_observations=total_weight,
        global_confidence=exact_confidence,
        observations=observation_diagnostics,
    )


def build_engagement_model(
    history: Mapping[str, object] | None,
    snapshots: Sequence[Mapping[str, object]] | None,
    *,
    now: datetime | None = None,
    config: LearningConfig | None = None,
) -> EngagementModel:
    """Build a rebuildable model from canonical history and Insights snapshots."""
    return analyze_engagement_learning(
        history,
        snapshots,
        now=now,
        config=config,
    ).model
