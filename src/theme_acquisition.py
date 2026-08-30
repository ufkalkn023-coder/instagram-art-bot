"""Bounded multi-query acquisition and deterministic carousel-theme relevance."""

from __future__ import annotations

import hashlib
import logging
import random
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from src.artwork_visual_features import ArtworkVisualFeatures
from src.artwork_metadata import parse_artwork_date, period_bucket
from src.carousel_themes import (
    CarouselFormat,
    CarouselThemeDefinition,
    ThemeEvidenceMode,
    get_format_policy,
)
from src.format_contracts import (
    constrained_adapter_source_ids,
    matches_normalized_format_target,
    qualify_normalized_artwork,
    target_query_terms,
)
from src.models import NormalizedArtwork
from src.museums.base import AdapterHTTPError
from src.quality_filter import calculate_measurement_coverage, calculate_quality_score
from src.rights_policy import is_rights_eligible
from src.carousel_policy import MIN_FEATURED_WORKS

logger = logging.getLogger(__name__)

DEFAULT_MIN_THEME_RELEVANCE = 50.0
ABSOLUTE_MINIMUM_RELEVANT_POOL = MIN_FEATURED_WORKS
PREFERRED_HEADROOM = 12
# Compatibility names retained for existing callers and manifests.
ABSOLUTE_MINIMUM = ABSOLUTE_MINIMUM_RELEVANT_POOL
PREFERRED_PREFLIGHT_TARGET = PREFERRED_HEADROOM
# Compatibility names for callers which configured the former policy fields.
PUBLICATION_ARTWORK_COUNT = ABSOLUTE_MINIMUM
DEFAULT_SAFE_POOL_HEADROOM = PREFERRED_PREFLIGHT_TARGET

PRIMARY_QUERY_MAX = 24.0
SECONDARY_QUERY_MAX = 8.0
REPEATED_QUERY_MAX = 10.0
REQUIRED_METADATA_SCORE = 32.0
PREFERRED_MATCH_SCORE = 4.0
PREFERRED_MATCH_MAX = 12.0
TITLE_EVIDENCE_SCORE = 12.0
CONTEXT_EVIDENCE_SCORE = 8.0
DESCRIPTION_EVIDENCE_SCORE = 4.0
FORMAT_TARGET_SCORE = 40.0
HYBRID_PREQUALIFICATION_THRESHOLD = 20.0
VISUAL_TARGET_MAX = 28.0
VISUAL_SUPPORT_MAX = 12.0


class QueryType(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


@dataclass(frozen=True)
class ThemeQueryHit:
    """One idempotent record of the registry query that returned a candidate."""

    query_type: QueryType
    query_rank: int
    query: str


@dataclass(frozen=True)
class MetadataMatch:
    signal: str
    field: str


@dataclass(frozen=True)
class ThemeRelevanceBreakdown:
    primary_query: float = 0.0
    secondary_query: float = 0.0
    repeated_queries: float = 0.0
    required: float = 0.0
    preferred: float = 0.0
    title_evidence: float = 0.0
    context_evidence: float = 0.0
    description_evidence: float = 0.0
    format_target: float = 0.0
    visual_target: float = 0.0
    visual_support: float = 0.0
    excluded: float = 0.0

    @property
    def total(self) -> float:
        return round(
            max(
                0.0,
                min(
                    100.0,
                    self.primary_query
                    + self.secondary_query
                    + self.repeated_queries
                    + self.required
                    + self.preferred
                    + self.title_evidence
                    + self.context_evidence
                    + self.description_evidence
                    + self.format_target
                    + self.visual_target
                    + self.visual_support
                    + self.excluded,
                ),
            ),
            2,
        )


@dataclass(frozen=True)
class ThemeCandidateEvidence:
    canonical_id: str
    matched_queries: tuple[ThemeQueryHit, ...]
    strongest_query: ThemeQueryHit | None
    metadata_matches: tuple[MetadataMatch, ...]
    required_matches: tuple[str, ...]
    preferred_matches: tuple[str, ...]
    excluded_matches: tuple[str, ...]
    missing_required_groups: tuple[tuple[str, ...], ...]
    theme_relevance_score: float
    relevance_breakdown: ThemeRelevanceBreakdown
    format_target_match: bool = True
    format_rejection_reason: str | None = None
    semantic_grounded: bool = True
    visual_grounded: bool = True
    metadata_score: float = 0.0

    @property
    def relevance_eligible(self) -> bool:
        return (
            self.format_target_match
            and self.semantic_grounded
            and self.visual_grounded
            and not self.excluded_matches
        )

    @property
    def provisional_eligible(self) -> bool:
        """Whether bounded secure image inspection is justified for a hybrid theme."""
        return (
            self.format_target_match
            and not self.excluded_matches
            and self.metadata_score >= HYBRID_PREQUALIFICATION_THRESHOLD
            and (
                bool(self.required_matches)
                or any(hit.query_type is QueryType.PRIMARY for hit in self.matched_queries)
            )
        )


@dataclass(frozen=True)
class CarouselCandidateScoreBreakdown:
    """Theme-dominant ranking kept separate from technical quality semantics."""

    theme_relevance: float
    technical_quality: float
    serendipity: float

    @property
    def total(self) -> float:
        return round(self.theme_relevance + self.technical_quality + self.serendipity, 4)


@dataclass(frozen=True)
class ThemeCandidate:
    artwork: NormalizedArtwork
    evidence: ThemeCandidateEvidence
    candidate_score: CarouselCandidateScoreBreakdown


@dataclass(frozen=True)
class AdapterFailure:
    source_id: str
    query: str
    error_type: str


@dataclass(frozen=True)
class ThemeAvailabilityResult:
    """Metadata preflight; target is preferred headroom, not hard viability."""

    theme_id: str
    raw_candidates: int
    unique_candidates: int
    history_eligible: int
    rights_eligible: int
    relevance_eligible: int
    quality_eligible: int
    estimated_safe_pool: int
    target: int
    sufficient: bool
    failure_reason: str | None
    query_count: int
    network_call_count: int
    adapter_failures: tuple[AdapterFailure, ...]
    format_target_matches: int = 0
    distinct_artists: int = 0
    distinct_museums: int = 0
    distinct_periods: int = 0
    chronological_span_years: int | None = None
    absolute_minimum: int = ABSOLUTE_MINIMUM
    narrow_pool: bool = False
    pool_status: str = "unavailable"
    metadata_prequalified: int = 0
    images_inspected: int = 0
    images_attempted: int = 0
    images_validated: int = 0
    image_validation_failed: int = 0
    visually_scored: int = 0
    final_relevance_qualified: int = 0
    final_score_min: float | None = None
    final_score_p25: float | None = None
    final_score_median: float | None = None
    final_score_p75: float | None = None
    final_score_max: float | None = None
    qualified_at_60: int = 0
    final_relevance_failures: tuple[tuple[str, int], ...] = ()
    aic_fallback_attempted: int = 0
    aic_fallback_recovered: int = 0
    aic_fallback_failed: int = 0


@dataclass
class AcquisitionRunState:
    """Ephemeral adapter health and diagnostics shared by one acquisition run."""

    adapter_calls: int = 0
    http_403_failures: int = 0
    themes_attempted: set[str] = field(default_factory=set)
    seen_adapters: set[str] = field(default_factory=set)
    unavailable_adapters: dict[str, str] = field(default_factory=dict)
    runtime_disabled_adapters: dict[str, str] = field(default_factory=dict)
    disabled_adapters: dict[str, str] = field(default_factory=dict)
    consecutive_backoff_failures: dict[str, int] = field(default_factory=dict)

    def register(self, source_id: str) -> None:
        self.seen_adapters.add(source_id)

    def mark_unavailable(self, source_id: str, reason: str) -> bool:
        self.register(source_id)
        if source_id in self.disabled_adapters:
            return False
        self.unavailable_adapters[source_id] = reason
        self.disabled_adapters[source_id] = reason
        return True

    def disable(self, source_id: str, reason: str) -> bool:
        self.register(source_id)
        if source_id in self.disabled_adapters:
            return False
        self.runtime_disabled_adapters[source_id] = reason
        self.disabled_adapters[source_id] = reason
        return True

    def diagnostics(self) -> dict[str, object]:
        from src.aic_image_policy import get_aic_image_request_policy

        aic_images = get_aic_image_request_policy().diagnostics()
        active_adapters = tuple(
            sorted(self.seen_adapters - set(self.disabled_adapters))
        )
        return {
            "themes_attempted": len(self.themes_attempted),
            "adapter_calls": self.adapter_calls,
            "adapters_disabled_for_run": tuple(sorted(self.disabled_adapters)),
            "active_adapters": active_adapters,
            "unavailable_adapters": dict(sorted(self.unavailable_adapters.items())),
            "runtime_disabled_adapters": dict(
                sorted(self.runtime_disabled_adapters.items())
            ),
            "403_failures": self.http_403_failures,
            "aic_image_requests": {
                "analysis_843": aic_images.analysis_843,
                "final_1686": aic_images.final_1686,
                "fallback_843": aic_images.fallback_843,
                "rate_limited": aic_images.rate_limited,
                "recovered": aic_images.recovered,
                "failed": aic_images.failed,
                "circuit_open": aic_images.circuit_open,
            },
        }


@dataclass(frozen=True)
class ThemeAcquisitionPolicy:
    max_primary_queries: int = 3
    max_secondary_queries: int = 2
    candidates_per_adapter_query: int = 16
    max_network_calls: int = 20
    minimum_safe_pool: int = DEFAULT_SAFE_POOL_HEADROOM
    minimum_relevance: float = DEFAULT_MIN_THEME_RELEVANCE


@dataclass(frozen=True)
class ThemeAcquisitionResult:
    theme: CarouselThemeDefinition
    candidates: tuple[ThemeCandidate, ...]
    all_candidates: tuple[ThemeCandidate, ...]
    availability: ThemeAvailabilityResult
    policy: ThemeAcquisitionPolicy
    validated_artworks: tuple[dict[str, object], ...] = ()


class CarouselThemeAvailabilityError(RuntimeError):
    """Raised before publication mutation when every bounded theme attempt fails."""

    def __init__(self, attempts: Sequence[tuple[str, str]]):
        self.attempts = tuple(attempts)
        summary = "; ".join(f"{theme_id}={reason}" for theme_id, reason in self.attempts)
        super().__init__(f"No viable carousel theme within the fallback limit: {summary}")


def normalize_theme_text(value: object) -> tuple[str, ...]:
    """Unicode-aware normalization used by every signal and phrase comparison."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if character.isalnum() or category.startswith("M"):
            characters.append(character)
        else:
            characters.append(" ")
    return tuple("".join(characters).split())


def phrase_matches(tokens: Sequence[str], phrase: object) -> bool:
    """Match whole token sequences so, for example, art never matches cartography."""
    phrase_tokens = normalize_theme_text(phrase)
    if not phrase_tokens or len(phrase_tokens) > len(tokens):
        return False
    width = len(phrase_tokens)
    return any(tuple(tokens[index:index + width]) == phrase_tokens for index in range(len(tokens) - width + 1))


def _metadata_fields(artwork: NormalizedArtwork) -> dict[str, tuple[str, ...]]:
    return {
        "title": normalize_theme_text(artwork.title),
        "classification": normalize_theme_text(artwork.classification),
        "medium": normalize_theme_text(artwork.medium),
        "culture": normalize_theme_text(artwork.culture),
        "geographic_origin": normalize_theme_text(artwork.geographic_origin),
        "artist_nationality": normalize_theme_text(artwork.artist_nationality),
        "department": normalize_theme_text(artwork.department),
        "style_or_period": normalize_theme_text(artwork.style_or_period),
        "region": normalize_theme_text(artwork.region),
        "description": normalize_theme_text(artwork.description),
    }


def _matching_fields(fields: dict[str, tuple[str, ...]], signal: str) -> tuple[str, ...]:
    return tuple(field_name for field_name, tokens in fields.items() if phrase_matches(tokens, signal))


def _required_groups(theme: CarouselThemeDefinition) -> tuple[tuple[str, ...], ...]:
    if theme.required_term_groups:
        return theme.required_term_groups
    return (theme.required_terms,) if theme.required_terms else ()


def _strongest_query(hits: Sequence[ThemeQueryHit]) -> ThemeQueryHit | None:
    if not hits:
        return None
    return min(
        hits,
        key=lambda hit: (0 if hit.query_type is QueryType.PRIMARY else 1, hit.query_rank, hit.query.casefold()),
    )


def evaluate_theme_relevance(
    artwork: NormalizedArtwork,
    theme: CarouselThemeDefinition,
    matched_queries: Iterable[ThemeQueryHit],
    visual_features: ArtworkVisualFeatures | None = None,
) -> ThemeCandidateEvidence:
    """Evaluate required, preferred, excluded, field, and query evidence on 0-100."""
    hits = tuple(sorted(set(matched_queries), key=lambda hit: (hit.query_type.value, hit.query_rank, hit.query.casefold())))
    fields = _metadata_fields(artwork)
    signal_fields: dict[str, tuple[str, ...]] = {}

    relevant_signals = {
        signal
        for group in _required_groups(theme)
        for signal in group
    } | set(theme.preferred_terms) | set(theme.excluded_terms)
    for signal in sorted(relevant_signals, key=str.casefold):
        signal_fields[signal] = _matching_fields(fields, signal)

    groups = _required_groups(theme)
    required_matches: list[str] = []
    missing_groups: list[tuple[str, ...]] = []
    for group in groups:
        matches = [signal for signal in group if signal_fields.get(signal)]
        if matches:
            required_matches.extend(matches)
        else:
            missing_groups.append(group)

    preferred_matches = tuple(
        signal for signal in theme.preferred_terms if signal_fields.get(signal)
    )
    excluded_matches = tuple(
        signal for signal in theme.excluded_terms if signal_fields.get(signal)
    )
    metadata_matches = tuple(
        MetadataMatch(signal, field_name)
        for signal in sorted(relevant_signals, key=str.casefold)
        for field_name in signal_fields.get(signal, ())
    )

    primary_hits = [hit for hit in hits if hit.query_type is QueryType.PRIMARY]
    secondary_hits = [hit for hit in hits if hit.query_type is QueryType.SECONDARY]
    if primary_hits:
        best_primary_rank = min(hit.query_rank for hit in primary_hits)
        primary_score = max(20.0, PRIMARY_QUERY_MAX - best_primary_rank * 2.0)
        secondary_score = 0.0
    else:
        primary_score = 0.0
        secondary_score = SECONDARY_QUERY_MAX if secondary_hits else 0.0
    repeated_score = min(REPEATED_QUERY_MAX, max(0, len(hits) - 1) * 4.0)

    matched_signal_fields = {
        field_name
        for signal in relevant_signals - set(theme.excluded_terms)
        for field_name in signal_fields.get(signal, ())
    }
    metadata_breakdown = ThemeRelevanceBreakdown(
        primary_query=primary_score,
        secondary_query=secondary_score,
        repeated_queries=repeated_score,
        required=REQUIRED_METADATA_SCORE if groups and not missing_groups else 0.0,
        preferred=min(PREFERRED_MATCH_MAX, len(set(preferred_matches)) * PREFERRED_MATCH_SCORE),
        title_evidence=TITLE_EVIDENCE_SCORE if "title" in matched_signal_fields else 0.0,
        context_evidence=CONTEXT_EVIDENCE_SCORE
        if matched_signal_fields.intersection(
            {"classification", "medium", "culture", "geographic_origin", "artist_nationality", "department", "style_or_period", "region"}
        )
        else 0.0,
        description_evidence=DESCRIPTION_EVIDENCE_SCORE if "description" in matched_signal_fields else 0.0,
        format_target=(
            FORMAT_TARGET_SCORE
            if theme.format
            in {
                CarouselFormat.MONOGRAPHIC,
                CarouselFormat.MUSEUM_SPOTLIGHT,
                CarouselFormat.REGIONAL,
                CarouselFormat.PERIOD_FOCUS,
                CarouselFormat.MEDIUM_FOCUS,
            }
            and theme.format_target is not None
            and matches_normalized_format_target(artwork, theme)
            else 0.0
        ),
        excluded=-100.0 if excluded_matches else 0.0,
    )
    visual_target_score = 0.0
    visual_support_score = 0.0
    visual_grounded = theme.evidence_mode is ThemeEvidenceMode.METADATA
    if theme.visual_target is not None and visual_features is not None:
        checks: list[bool] = []
        target = theme.visual_target
        if target.color_families:
            checks.append(visual_features.dominant_color_family.value in target.color_families)
        if target.luminance_buckets:
            checks.append(visual_features.luminance_bucket.value in target.luminance_buckets)
        if target.contrast_buckets:
            checks.append(visual_features.contrast_bucket.value in target.contrast_buckets)
        matched_checks = sum(checks)
        visual_grounded = bool(checks) and matched_checks > 0
        visual_target_score = VISUAL_TARGET_MAX * matched_checks / len(checks) if checks else 0.0
        if visual_grounded:
            known_support = 0
            if visual_features.mean_luminance is not None:
                known_support += 1
            if visual_features.mean_saturation is not None:
                known_support += 1
            if visual_features.contrast_bucket.value != "UNKNOWN":
                known_support += 1
            visual_support_score = VISUAL_SUPPORT_MAX * min(3, known_support) / 3

    format_target_match, format_rejection_reason = qualify_normalized_artwork(
        artwork, theme
    )
    primary_semantic_hits = 0
    for hit in primary_hits:
        query_tokens = normalize_theme_text(hit.query)
        if any(phrase_matches(query_tokens, signal) for group in groups for signal in group):
            primary_semantic_hits += 1
    supporting_metadata_fields = matched_signal_fields.intersection(
        {"title", "description", "classification", "medium"}
    )
    strongly_grounded_primary_metadata = (
        bool(primary_hits)
        and len(supporting_metadata_fields) >= 2
        and not excluded_matches
        and format_target_match
    )
    if theme.evidence_mode is ThemeEvidenceMode.METADATA:
        semantic_grounded = not missing_groups or strongly_grounded_primary_metadata
    elif theme.format is CarouselFormat.COLOR_STUDY:
        # The target color is proved by pixels; provenance keeps that evidence thematic.
        semantic_grounded = bool(primary_hits) or not missing_groups
    elif theme.evidence_mode is ThemeEvidenceMode.IMAGE:
        semantic_grounded = True
    else:
        # Pixel statistics cannot prove winter, candles, windows, dawn, or art periods.
        strongly_grounded_primary_provenance = (
            theme.format is CarouselFormat.LIGHT_STUDY
            and primary_semantic_hits >= 1
            and visual_grounded
            and not excluded_matches
            and format_target_match
        )
        semantic_grounded = (
            not missing_groups
            or primary_semantic_hits >= 2
            or strongly_grounded_primary_provenance
        )

    breakdown = ThemeRelevanceBreakdown(
        **{
            field_name: getattr(metadata_breakdown, field_name)
            for field_name in (
                "primary_query", "secondary_query", "repeated_queries", "required",
                "preferred", "title_evidence", "context_evidence",
                "description_evidence", "format_target", "excluded",
            )
        },
        visual_target=round(visual_target_score, 2),
        visual_support=round(visual_support_score, 2),
    )
    return ThemeCandidateEvidence(
        canonical_id=artwork.canonical_id,
        matched_queries=hits,
        strongest_query=_strongest_query(hits),
        metadata_matches=metadata_matches,
        required_matches=tuple(dict.fromkeys(required_matches)),
        preferred_matches=tuple(dict.fromkeys(preferred_matches)),
        excluded_matches=excluded_matches,
        missing_required_groups=tuple(missing_groups),
        theme_relevance_score=breakdown.total,
        relevance_breakdown=breakdown,
        format_target_match=format_target_match,
        format_rejection_reason=format_rejection_reason,
        semantic_grounded=semantic_grounded,
        visual_grounded=visual_grounded,
        metadata_score=metadata_breakdown.total,
    )


def _stable_serendipity(run_seed: str, theme_id: str, candidate_id: str) -> float:
    material = f"{run_seed}\x1ftheme_candidate\x1f{theme_id}\x1f{candidate_id}".encode()
    stable_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")
    return random.Random(stable_seed).uniform(0.0, 2.0)


@dataclass
class _CandidateAggregate:
    artwork: NormalizedArtwork
    hits: set[ThemeQueryHit] = field(default_factory=set)


def _candidate_completeness(artwork: NormalizedArtwork) -> int:
    return sum(
        bool(value)
        for value in (
            artwork.title,
            artwork.description,
            artwork.classification,
            artwork.medium,
            artwork.culture,
            artwork.geographic_origin,
            artwork.department,
            artwork.style_or_period,
            artwork.image_url,
        )
    )


def _candidate_variant_rank(artwork: NormalizedArtwork) -> tuple[int, int, int, int]:
    return (
        int(bool(artwork.image_url)),
        int(bool(artwork.image_width and artwork.image_height)),
        _candidate_completeness(artwork),
        int(bool(artwork.rights_status or artwork.rights_text)),
    )


def _query_rng(run_seed: str, source_id: str, hit: ThemeQueryHit) -> random.Random:
    material = f"{run_seed}\x1ftheme_acquisition\x1f{source_id}\x1f{hit.query_type.value}\x1f{hit.query_rank}\x1f{hit.query}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(material).digest(), "big"))


def _build_candidates(
    aggregates: dict[str, _CandidateAggregate],
    *,
    theme: CarouselThemeDefinition,
    posted_ids: set[str],
    museum_weights: dict,
    min_quality: float,
    min_relevance: float,
    run_seed: str,
) -> tuple[tuple[ThemeCandidate, ...], tuple[ThemeCandidate, ...], dict[str, int]]:
    all_candidates: list[ThemeCandidate] = []
    eligible: list[ThemeCandidate] = []
    counts = {"history": 0, "rights": 0, "target": 0, "relevance": 0, "quality": 0}
    for candidate_id in sorted(aggregates):
        aggregate = aggregates[candidate_id]
        artwork = aggregate.artwork
        evidence = evaluate_theme_relevance(artwork, theme, aggregate.hits)
        quality = calculate_quality_score(artwork, museum_weights)
        artwork.quality_score = quality
        artwork.measurement_coverage = calculate_measurement_coverage(artwork)
        score = CarouselCandidateScoreBreakdown(
            theme_relevance=evidence.theme_relevance_score * 0.68,
            technical_quality=quality * 0.30,
            serendipity=_stable_serendipity(run_seed, theme.id, candidate_id),
        )
        artwork._theme_candidate_breakdown = score
        themed_candidate = ThemeCandidate(artwork, evidence, score)
        all_candidates.append(themed_candidate)

        if evidence.excluded_matches:
            logger.debug(
                "theme_candidate_rejected id=%s theme=%s reason=excluded_signal signal=%s",
                candidate_id,
                theme.id,
                ",".join(evidence.excluded_matches),
            )
        elif evidence.missing_required_groups:
            logger.debug(
                "theme_candidate_rejected id=%s theme=%s reason=missing_required_signal groups=%s",
                candidate_id,
                theme.id,
                "|".join("/".join(group) for group in evidence.missing_required_groups),
            )

        if candidate_id in posted_ids:
            continue
        counts["history"] += 1
        if not evidence.format_target_match:
            logger.debug(
                "theme_candidate_rejected id=%s theme=%s reason=%s",
                candidate_id,
                theme.id,
                evidence.format_rejection_reason,
            )
            continue
        counts["target"] += 1
        if not is_rights_eligible(artwork):
            continue
        counts["rights"] += 1
        if not artwork.image_url or quality < min_quality:
            continue
        counts["quality"] += 1
        if theme.evidence_mode is ThemeEvidenceMode.METADATA:
            relevance_pass = (
                evidence.relevance_eligible
                and evidence.theme_relevance_score >= min_relevance
            )
        else:
            relevance_pass = evidence.provisional_eligible
        if not relevance_pass:
            if (
                theme.evidence_mode is ThemeEvidenceMode.METADATA
                and evidence.relevance_eligible
            ):
                logger.debug(
                    "theme_candidate_rejected id=%s theme=%s reason=relevance_below_threshold score=%.1f threshold=%.1f",
                    candidate_id,
                    theme.id,
                    evidence.theme_relevance_score,
                    min_relevance,
                )
            continue
        counts["relevance"] += 1
        artwork.selection_score = score.total
        eligible.append(themed_candidate)

    eligible.sort(
        key=lambda candidate: (
            -candidate.candidate_score.total,
            -candidate.evidence.theme_relevance_score,
            candidate.artwork.canonical_id,
        )
    )
    return tuple(eligible), tuple(all_candidates), counts


def _failure_reason(
    raw: int,
    unique: int,
    counts: dict[str, int],
    viability_minimum: int,
    theme: CarouselThemeDefinition,
) -> str | None:
    if raw == 0:
        return "no_adapter_results"
    if counts["history"] == 0:
        return "all_candidates_already_posted"
    if theme.format_target is not None and counts["target"] < viability_minimum:
        return "insufficient_format_target_pool"
    if unique < viability_minimum:
        return "insufficient_unique_pool"
    if counts["rights"] < viability_minimum:
        return "insufficient_rights_policy_pool"
    if counts["quality"] < viability_minimum:
        return "insufficient_quality_pool"
    if counts["relevance"] < viability_minimum:
        return "insufficient_relevance_pool"
    return None


def acquire_theme_candidates(
    theme: CarouselThemeDefinition,
    *,
    posted_ids: set[str],
    adapters: Sequence[object],
    run_seed: str,
    museum_weights: dict,
    min_quality: float,
    policy: ThemeAcquisitionPolicy | None = None,
    run_state: AcquisitionRunState | None = None,
) -> ThemeAcquisitionResult:
    """Run bounded registry queries and stop as soon as metadata headroom is sufficient."""
    policy = policy or ThemeAcquisitionPolicy()
    run_state = run_state or AcquisitionRunState()
    run_state.themes_attempted.add(theme.id)
    preferred_target = max(
        PREFERRED_PREFLIGHT_TARGET,
        policy.minimum_safe_pool,
        theme.minimum_candidate_target,
    )
    primary_queries = tuple(
        dict.fromkeys((*target_query_terms(theme), *theme.primary_queries))
    )
    query_plan = [
        ThemeQueryHit(QueryType.PRIMARY, rank, query)
        for rank, query in enumerate(primary_queries[:policy.max_primary_queries])
    ] + [
        ThemeQueryHit(QueryType.SECONDARY, rank, query)
        for rank, query in enumerate(theme.secondary_queries[:policy.max_secondary_queries])
    ]
    aggregates: dict[str, _CandidateAggregate] = {}
    adapter_failures: list[AdapterFailure] = []
    raw_candidates = 0
    query_count = 0
    network_calls = 0
    eligible: tuple[ThemeCandidate, ...] = ()
    all_candidates: tuple[ThemeCandidate, ...] = ()
    counts = {"history": 0, "rights": 0, "target": 0, "relevance": 0, "quality": 0}
    source_constraint = constrained_adapter_source_ids(theme)
    constrained_adapters = tuple(
        adapter
        for adapter in adapters
        if not source_constraint
        or str(getattr(adapter, "source_id", type(adapter).__name__)).casefold()
        in source_constraint
    )
    for adapter in constrained_adapters:
        source_id = str(getattr(adapter, "source_id", type(adapter).__name__))
        run_state.register(source_id)
        unavailable_reason = getattr(adapter, "unavailable_reason", lambda: None)()
        if unavailable_reason and run_state.mark_unavailable(
            source_id, unavailable_reason
        ):
            logger.warning(
                "theme_adapter_disabled_for_run source=%s reason=%s",
                source_id,
                unavailable_reason,
            )
    format_policy = get_format_policy(theme.format)
    logger.info(
        "format_policy theme=%s format=%s required_target=%s general_diversity=soft "
        "same_artist_redundancy=%s same_museum_redundancy=%s sources=%s",
        theme.id,
        theme.format.value,
        format_policy.required_target_dimension.value
        if format_policy.required_target_dimension
        else "none",
        "penalized" if format_policy.penalize_artist_similarity else "ignored",
        "penalized" if format_policy.penalize_museum_similarity else "ignored",
        ",".join(sorted(source_constraint)) or "all",
    )

    for hit in query_plan:
        if network_calls >= policy.max_network_calls:
            break
        if hit.query_type is QueryType.SECONDARY and counts["relevance"] >= preferred_target:
            break
        query_count += 1
        for adapter in constrained_adapters:
            if network_calls >= policy.max_network_calls:
                break
            source_id = str(getattr(adapter, "source_id", type(adapter).__name__))
            if source_id in run_state.disabled_adapters:
                continue
            network_calls += 1
            try:
                run_state.adapter_calls += 1
                fetched = adapter.fetch_candidates(
                    limit=policy.candidates_per_adapter_query,
                    query=hit.query,
                    rng=_query_rng(run_seed, source_id, hit),
                )
            except AdapterHTTPError as error:
                error_type = f"HTTP{error.status_code}"
                adapter_failures.append(AdapterFailure(source_id, hit.query, error_type))
                if error.status_code == 403:
                    run_state.http_403_failures += 1
                failures = run_state.consecutive_backoff_failures.get(source_id, 0) + 1
                run_state.consecutive_backoff_failures[source_id] = failures
                if failures >= 2 and run_state.disable(source_id, error_type):
                    logger.warning(
                        "theme_adapter_disabled_for_run source=%s reason=%s failures=%s",
                        source_id,
                        error_type,
                        failures,
                    )
                continue
            except Exception as error:
                run_state.consecutive_backoff_failures[source_id] = 0
                adapter_failures.append(AdapterFailure(source_id, hit.query, type(error).__name__))
                logger.warning(
                    "theme_adapter_failure theme=%s source=%s query=%r error=%s",
                    theme.id,
                    source_id,
                    hit.query,
                    type(error).__name__,
                )
                continue
            run_state.consecutive_backoff_failures[source_id] = 0
            for artwork in fetched:
                raw_candidates += 1
                candidate_id = artwork.canonical_id
                aggregate = aggregates.get(candidate_id)
                if aggregate is None:
                    aggregates[candidate_id] = _CandidateAggregate(artwork, {hit})
                else:
                    aggregate.hits.add(hit)
                    if _candidate_variant_rank(artwork) > _candidate_variant_rank(aggregate.artwork):
                        aggregate.artwork = artwork

        eligible, all_candidates, counts = _build_candidates(
            aggregates,
            theme=theme,
            posted_ids=posted_ids,
            museum_weights=museum_weights,
            min_quality=min_quality,
            min_relevance=policy.minimum_relevance,
            run_seed=run_seed,
        )
        if (
            theme.evidence_mode is ThemeEvidenceMode.METADATA
            and counts["relevance"] >= preferred_target
        ):
            break

    reason = _failure_reason(
        raw_candidates,
        len(aggregates),
        counts,
        ABSOLUTE_MINIMUM,
        theme,
    )
    qualified_artworks = [candidate.artwork for candidate in eligible]
    known_years = []
    for artwork in qualified_artworks:
        if date_info := parse_artwork_date(artwork.creation_date):
            known_years.append(date_info.representative_year)
    availability = ThemeAvailabilityResult(
        theme_id=theme.id,
        raw_candidates=raw_candidates,
        unique_candidates=len(aggregates),
        history_eligible=counts["history"],
        rights_eligible=counts["rights"],
        relevance_eligible=counts["relevance"],
        quality_eligible=counts["quality"],
        estimated_safe_pool=counts["relevance"],
        target=preferred_target,
        sufficient=reason is None,
        failure_reason=reason,
        query_count=query_count,
        network_call_count=network_calls,
        adapter_failures=tuple(adapter_failures),
        format_target_matches=counts["target"],
        distinct_artists=len(
            {artwork.artist_name.casefold() for artwork in qualified_artworks if artwork.artist_name}
        ),
        distinct_museums=len(
            {artwork.museum_name.casefold() for artwork in qualified_artworks if artwork.museum_name}
        ),
        distinct_periods=len(
            {
                bucket
                for artwork in qualified_artworks
                if (bucket := period_bucket(artwork.creation_date))
            }
        ),
        chronological_span_years=(
            max(known_years) - min(known_years) if len(known_years) >= 2 else None
        ),
        absolute_minimum=ABSOLUTE_MINIMUM,
        narrow_pool=ABSOLUTE_MINIMUM <= counts["relevance"] < PREFERRED_PREFLIGHT_TARGET,
        pool_status=(
            "unavailable"
            if reason is not None
            else "narrow"
            if counts["relevance"] < PREFERRED_PREFLIGHT_TARGET
            else "preferred"
        ),
        metadata_prequalified=(
            counts["relevance"]
            if theme.evidence_mode is not ThemeEvidenceMode.METADATA
            else 0
        ),
        final_relevance_qualified=(
            counts["relevance"]
            if theme.evidence_mode is ThemeEvidenceMode.METADATA
            else 0
        ),
    )
    logger.info(
        "theme_acquisition theme=%s queries=%s calls=%s raw=%s unique=%s history=%s rights=%s "
        "qualified=%s quality=%s absolute_minimum=%s preferred_target=%s pool_status=%s "
        "adapter_failures=%s result=%s",
        theme.id,
        query_count,
        network_calls,
        raw_candidates,
        len(aggregates),
        counts["history"],
        counts["rights"],
        counts["relevance"],
        counts["quality"],
        ABSOLUTE_MINIMUM,
        preferred_target,
        availability.pool_status,
        len(adapter_failures),
        "viable" if availability.sufficient else reason,
    )
    if theme.format in {CarouselFormat.MONOGRAPHIC, CarouselFormat.MUSEUM_SPOTLIGHT, CarouselFormat.CHRONOLOGICAL}:
        logger.info(
            "format_availability theme=%s format=%s target_matches=%s qualified=%s "
            "distinct_artists=%s distinct_museums=%s periods=%s chronological_span=%s result=%s",
            theme.id,
            theme.format.value,
            availability.format_target_matches,
            availability.estimated_safe_pool,
            availability.distinct_artists,
            availability.distinct_museums,
            availability.distinct_periods,
            availability.chronological_span_years,
            "viable" if availability.sufficient else availability.failure_reason,
        )
    for candidate in all_candidates:
        breakdown = candidate.evidence.relevance_breakdown
        logger.debug(
            "theme_relevance id=%s theme=%s primary_query=%+.1f secondary_query=%+.1f repeated=%+.1f "
            "required=%+.1f preferred=%+.1f title_evidence=%+.1f context_evidence=%+.1f "
            "description_evidence=%+.1f format_target=%+.1f visual_target=%+.1f "
            "visual_support=%+.1f excluded=%+.1f total=%.1f eligible=%s",
            candidate.artwork.canonical_id,
            theme.id,
            breakdown.primary_query,
            breakdown.secondary_query,
            breakdown.repeated_queries,
            breakdown.required,
            breakdown.preferred,
            breakdown.title_evidence,
            breakdown.context_evidence,
            breakdown.description_evidence,
            breakdown.format_target,
            breakdown.visual_target,
            breakdown.visual_support,
            breakdown.excluded,
            breakdown.total,
            candidate in eligible,
        )
    return ThemeAcquisitionResult(theme, eligible, all_candidates, availability, policy)
