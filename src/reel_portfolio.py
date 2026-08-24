"""Deterministically build a small, quality-first Reel production portfolio.

This layer consumes the already eligible, ranked ``reel-preselector-v1``
decisions. It does not rescore artworks, access feed history, write Reel
history, call Gemini, or invoke the Remotion pipeline. Template fatigue belongs
after the future planner has selected a template and is intentionally absent.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

from src.reel_selector import ReelCandidateDecision


UTC = timezone.utc


logger = logging.getLogger(__name__)

PORTFOLIO_SELECTION_VERSION = "reel-portfolio-v1"
DEFAULT_SELECTION_TARGET = 4
REEL_SELECTION_TARGET_ENV = "REEL_SELECTION_TARGET"
RECENT_REEL_HISTORY_LOOKBACK = 12
DEFAULT_PORTFOLIO_SELECTION_DIRECTORY = Path(config.BASE_DIR) / "output" / "reel-selection"
DEFAULT_PORTFOLIO_SELECTION_MANIFEST = DEFAULT_PORTFOLIO_SELECTION_DIRECTORY / "portfolio-selection.json"


@dataclass(frozen=True)
class PortfolioAdjustmentValues:
    """Centralized V1 portfolio adjustments; the total is capped below."""

    same_batch_artist: float = -8.0
    same_batch_museum: float = -2.0
    same_batch_classification: float = -2.0
    same_batch_orientation: float = -1.0
    same_batch_source: float = -1.0
    recent_artist: float = -4.0
    recent_museum: float = -1.0
    minimum_total: float = -12.0
    maximum_total: float = 4.0


ADJUSTMENTS = PortfolioAdjustmentValues()


@dataclass(frozen=True)
class PortfolioAdjustmentBreakdown:
    same_batch_artist: float = 0.0
    same_batch_museum: float = 0.0
    same_batch_classification: float = 0.0
    same_batch_orientation: float = 0.0
    same_batch_source: float = 0.0
    recent_artist: float = 0.0
    recent_museum: float = 0.0
    other: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.same_batch_artist
            + self.same_batch_museum
            + self.same_batch_classification
            + self.same_batch_orientation
            + self.same_batch_source
            + self.recent_artist
            + self.recent_museum
            + self.other
        )

    def as_manifest(self) -> dict[str, float]:
        return {
            "sameBatchArtist": self.same_batch_artist,
            "sameBatchMuseum": self.same_batch_museum,
            "sameBatchClassification": self.same_batch_classification,
            "sameBatchOrientation": self.same_batch_orientation,
            "sameBatchSource": self.same_batch_source,
            "recentArtist": self.recent_artist,
            "recentMuseum": self.recent_museum,
            "other": self.other,
        }


@dataclass(frozen=True)
class PortfolioCandidateDecision:
    candidate_decision: ReelCandidateDecision
    selection_order: int
    adjustment_breakdown: PortfolioAdjustmentBreakdown
    portfolio_adjustment: float
    portfolio_priority_score: float

    @property
    def reel_pre_planner_score(self) -> float:
        score = self.candidate_decision.reel_pre_planner_score
        assert score is not None
        return score


@dataclass(frozen=True)
class SkippedReelDuplicate:
    candidate_decision: ReelCandidateDecision
    reason: str = "RECENT_REEL_DUPLICATE"


@dataclass(frozen=True)
class PortfolioSelectionResult:
    selected: tuple[PortfolioCandidateDecision, ...]
    skipped_recent_reel_duplicates: tuple[SkippedReelDuplicate, ...]
    selection_target: int
    eligible_candidate_count: int
    available_candidate_count: int
    history_lookback: int

    @property
    def shortfall(self) -> int:
        return max(0, self.selection_target - len(self.selected))


def resolve_selection_target(
    value: int | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Resolve and strictly validate the automatic Reel production target."""
    if value is None:
        environment = os.environ if environment is None else environment
        value = environment.get(REEL_SELECTION_TARGET_ENV, DEFAULT_SELECTION_TARGET)
    if isinstance(value, bool):
        raise ValueError("REEL_SELECTION_TARGET must be an integer >= 1")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("REEL_SELECTION_TARGET must be an integer >= 1") from error
    if isinstance(value, float) or parsed < 1:
        raise ValueError("REEL_SELECTION_TARGET must be an integer >= 1")
    return parsed


def _normalized_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None


def _orientation(width: object, height: object) -> str | None:
    if isinstance(width, bool) or isinstance(height, bool):
        return None
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    ratio = width / height
    if ratio > 1.15:
        return "landscape"
    if ratio < 1 / 1.15:
        return "portrait"
    return "square-ish"


def _history_value(entry: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _normalized_key(entry.get(key))
        if value is not None:
            return value
    return None


def _recent_history_entries(
    recent_reel_history: Iterable[Mapping[str, Any]] | None,
) -> tuple[Mapping[str, Any], ...]:
    if recent_reel_history is None:
        return ()
    # The supplied sequence is chronological (oldest to newest); only its
    # newest fixed window is relevant, avoiding wall-clock-dependent logic.
    entries = tuple(entry for entry in recent_reel_history if isinstance(entry, Mapping))
    return entries[-RECENT_REEL_HISTORY_LOOKBACK:]


def _candidate_key(decision: ReelCandidateDecision, field: str) -> str | None:
    artwork = decision.candidate.artwork
    if field == "artist":
        return _normalized_key(artwork.artist_name)
    if field == "museum":
        return _normalized_key(artwork.museum_name)
    if field == "classification":
        return _normalized_key(artwork.classification)
    if field == "source":
        return _normalized_key(artwork.source)
    if field == "orientation":
        return _orientation(decision.image_width, decision.image_height)
    raise ValueError(f"unknown portfolio key: {field}")


def _has_match(value: str | None, decisions: Iterable[ReelCandidateDecision], field: str) -> bool:
    return value is not None and any(_candidate_key(item, field) == value for item in decisions)


def _has_history_match(value: str | None, history: Iterable[Mapping[str, Any]], *keys: str) -> bool:
    return value is not None and any(_history_value(entry, *keys) == value for entry in history)


def _portfolio_adjustment(
    candidate: ReelCandidateDecision,
    selected: Iterable[PortfolioCandidateDecision],
    recent_history: Iterable[Mapping[str, Any]],
) -> tuple[PortfolioAdjustmentBreakdown, float]:
    selected_candidates = tuple(item.candidate_decision for item in selected)
    artist = _candidate_key(candidate, "artist")
    museum = _candidate_key(candidate, "museum")
    classification = _candidate_key(candidate, "classification")
    source = _candidate_key(candidate, "source")
    orientation = _candidate_key(candidate, "orientation")
    breakdown = PortfolioAdjustmentBreakdown(
        same_batch_artist=ADJUSTMENTS.same_batch_artist if _has_match(artist, selected_candidates, "artist") else 0.0,
        same_batch_museum=ADJUSTMENTS.same_batch_museum if _has_match(museum, selected_candidates, "museum") else 0.0,
        same_batch_classification=(
            ADJUSTMENTS.same_batch_classification if _has_match(classification, selected_candidates, "classification") else 0.0
        ),
        same_batch_orientation=(
            ADJUSTMENTS.same_batch_orientation if _has_match(orientation, selected_candidates, "orientation") else 0.0
        ),
        same_batch_source=ADJUSTMENTS.same_batch_source if _has_match(source, selected_candidates, "source") else 0.0,
        recent_artist=ADJUSTMENTS.recent_artist if _has_history_match(artist, recent_history, "artist", "artist_name") else 0.0,
        recent_museum=ADJUSTMENTS.recent_museum if _has_history_match(museum, recent_history, "museum", "museum_name") else 0.0,
    )
    return breakdown, round(min(ADJUSTMENTS.maximum_total, max(ADJUSTMENTS.minimum_total, breakdown.total)), 4)


def _portfolio_sort_key(item: PortfolioCandidateDecision) -> tuple[float, float, float, float, str]:
    score = item.candidate_decision.score_breakdown
    assert score is not None
    return (
        -item.portfolio_priority_score,
        -item.reel_pre_planner_score,
        -score.technical_quality,
        -score.detail_headroom,
        item.candidate_decision.candidate.artwork.canonical_id,
    )


def select_portfolio_candidates(
    ranked_candidates: Iterable[ReelCandidateDecision],
    target: int | str | None = None,
    recent_reel_history: Iterable[Mapping[str, Any]] | None = None,
    environment: Mapping[str, str] | None = None,
) -> PortfolioSelectionResult:
    """Choose an automatic, quality-first production set from eligible candidates.

    Every selection round recomputes only soft portfolio adjustments. Ties use
    priority score, base score, technical score, detail score, then canonical
    ID ascending. Feed history is deliberately neither read nor accepted.
    """
    resolved_target = resolve_selection_target(target, environment)
    eligible = tuple(
        candidate
        for candidate in ranked_candidates
        if candidate.eligible and candidate.reel_pre_planner_score is not None and candidate.score_breakdown is not None
    )
    all_history = tuple(entry for entry in (recent_reel_history or ()) if isinstance(entry, Mapping))
    recent_history = _recent_history_entries(all_history)
    produced_ids = {
        canonical_id
        for entry in all_history
        if (canonical_id := _history_value(entry, "canonicalId", "canonical_id")) is not None
    }
    skipped = tuple(
        SkippedReelDuplicate(candidate)
        for candidate in eligible
        if _normalized_key(candidate.candidate.artwork.canonical_id) in produced_ids
    )
    remaining = [
        candidate
        for candidate in eligible
        if _normalized_key(candidate.candidate.artwork.canonical_id) not in produced_ids
    ]
    selected: list[PortfolioCandidateDecision] = []
    while remaining and len(selected) < resolved_target:
        round_candidates: list[PortfolioCandidateDecision] = []
        for candidate in remaining:
            breakdown, adjustment = _portfolio_adjustment(candidate, selected, recent_history)
            base_score = candidate.reel_pre_planner_score
            assert base_score is not None
            round_candidates.append(
                PortfolioCandidateDecision(
                    candidate_decision=candidate,
                    selection_order=0,
                    adjustment_breakdown=breakdown,
                    portfolio_adjustment=adjustment,
                    portfolio_priority_score=round(base_score + adjustment, 4),
                )
            )
        winner = min(round_candidates, key=_portfolio_sort_key)
        selected.append(
            PortfolioCandidateDecision(
                candidate_decision=winner.candidate_decision,
                selection_order=len(selected) + 1,
                adjustment_breakdown=winner.adjustment_breakdown,
                portfolio_adjustment=winner.portfolio_adjustment,
                portfolio_priority_score=winner.portfolio_priority_score,
            )
        )
        remaining.remove(winner.candidate_decision)

    result = PortfolioSelectionResult(
        selected=tuple(selected),
        skipped_recent_reel_duplicates=skipped,
        selection_target=resolved_target,
        eligible_candidate_count=len(eligible),
        available_candidate_count=len(remaining) + len(selected),
        history_lookback=RECENT_REEL_HISTORY_LOOKBACK,
    )
    logger.info(
        "[reel-portfolio] target=%s eligible=%s selected=%s shortfall=%s",
        result.selection_target,
        result.eligible_candidate_count,
        len(result.selected),
        result.shortfall,
    )
    for item in result.selected:
        logger.info(
            "[reel-portfolio] #%s %s base=%.4f adjustment=%.4f priority=%.4f",
            item.selection_order,
            item.candidate_decision.candidate.artwork.canonical_id,
            item.reel_pre_planner_score,
            item.portfolio_adjustment,
            item.portfolio_priority_score,
        )
    return result


def _selected_manifest_entry(item: PortfolioCandidateDecision) -> dict[str, object]:
    artwork = item.candidate_decision.candidate.artwork
    return {
        "selectionOrder": item.selection_order,
        "canonicalId": artwork.canonical_id,
        "title": artwork.title,
        "artist": artwork.artist_name,
        "museum": artwork.museum_name,
        "reelPrePlannerScore": item.reel_pre_planner_score,
        "portfolioAdjustment": item.portfolio_adjustment,
        "portfolioPriorityScore": item.portfolio_priority_score,
        "adjustmentBreakdown": item.adjustment_breakdown.as_manifest(),
    }


def write_portfolio_selection_manifest(
    selection: PortfolioSelectionResult,
    output_path: str | Path = DEFAULT_PORTFOLIO_SELECTION_MANIFEST,
    generated_at: datetime | None = None,
) -> Path:
    """Write the local, gitignored portfolio manifest without source URLs."""
    timestamp = generated_at or datetime.now(UTC)
    manifest = {
        "portfolioSelectionVersion": PORTFOLIO_SELECTION_VERSION,
        "generatedAt": timestamp.isoformat(),
        "selectionTarget": selection.selection_target,
        "selectedCount": len(selection.selected),
        "eligibleCandidateCount": selection.eligible_candidate_count,
        "availableCandidateCount": selection.available_candidate_count,
        "shortfall": selection.shortfall,
        "historyLookback": selection.history_lookback,
        "selected": [_selected_manifest_entry(item) for item in selection.selected],
        "skippedRecentReelDuplicates": [
            {
                "canonicalId": item.candidate_decision.candidate.artwork.canonical_id,
                "reason": item.reason,
            }
            for item in selection.skipped_recent_reel_duplicates
        ],
    }
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".reel-portfolio-", suffix=".tmp", delete=False
    ) as temporary:
        json.dump(manifest, temporary, indent=2, ensure_ascii=False)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination
