"""Deterministically rank already-safe artworks before Reel planning.

This module is deliberately local-only.  It never fetches museum data, calls
Gemini, invokes Remotion, or changes the feed-selection pipeline.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import config
from PIL import Image, ImageStat

from src.models import NormalizedArtwork
from src.quality_filter import validate_local_image_file
from src.reel_handoff import (
    HANDOFF_RIGHTS_STATUS,
    ReelHandoffExportError,
    _measure_validated_image,
    _required_metadata,
)


logger = logging.getLogger(__name__)

SELECTION_VERSION = "reel-preselector-v1"
DEFAULT_SHORTLIST_SIZE = 8
REEL_SHORTLIST_SIZE_ENV = "REEL_SHORTLIST_SIZE"
DEFAULT_SELECTION_DIRECTORY = Path(config.BASE_DIR) / "output" / "reel-selection"
DEFAULT_SELECTION_MANIFEST = DEFAULT_SELECTION_DIRECTORY / "shortlist.json"

# These are Reel-specific source thresholds, on top of the production secure
# decode policy.  A 720px short side and a 1080px long side are the minimum
# useful raster for a 1080x1920 overview or moderate detail treatment.
MIN_REEL_SHORT_SIDE = 720
MIN_REEL_LONG_SIDE = config.REELS_WIDTH

SCORE_WEIGHTS = {
    "technicalQuality": 30.0,
    "detailHeadroom": 25.0,
    "metadataCompleteness": 20.0,
    "compositionFlexibility": 15.0,
    "visualInformation": 10.0,
}


@dataclass(frozen=True)
class ReelCandidate:
    """One normalized artwork and its already-validated local image path."""

    artwork: NormalizedArtwork
    local_image_path: str | Path


@dataclass(frozen=True)
class ReelScoreBreakdown:
    technical_quality: float
    detail_headroom: float
    metadata_completeness: float
    composition_flexibility: float
    visual_information: float

    @property
    def total(self) -> float:
        return (
            self.technical_quality
            + self.detail_headroom
            + self.metadata_completeness
            + self.composition_flexibility
            + self.visual_information
        )

    def as_manifest(self) -> dict[str, float]:
        return {
            "technicalQuality": self.technical_quality,
            "detailHeadroom": self.detail_headroom,
            "metadataCompleteness": self.metadata_completeness,
            "compositionFlexibility": self.composition_flexibility,
            "visualInformation": self.visual_information,
        }


@dataclass(frozen=True)
class ReelCandidateDecision:
    candidate: ReelCandidate
    eligible: bool
    rejection_reasons: tuple[str, ...] = ()
    image_width: int | None = None
    image_height: int | None = None
    score_breakdown: ReelScoreBreakdown | None = None

    @property
    def reel_pre_planner_score(self) -> float | None:
        return None if self.score_breakdown is None else self.score_breakdown.total


@dataclass(frozen=True)
class ReelSelectionResult:
    shortlist: tuple[ReelCandidateDecision, ...]
    rejected: tuple[ReelCandidateDecision, ...]
    candidate_count: int
    eligible_count: int
    shortlist_size: int


def resolve_shortlist_size(
    value: int | str | None = None,
    environment: dict[str, str] | None = None,
) -> int:
    """Resolve and strictly validate the central Reel shortlist-size setting."""
    if value is None:
        environment = os.environ if environment is None else environment
        value = environment.get(REEL_SHORTLIST_SIZE_ENV, DEFAULT_SHORTLIST_SIZE)
    if isinstance(value, bool):
        raise ValueError("REEL_SHORTLIST_SIZE must be an integer >= 1")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("REEL_SHORTLIST_SIZE must be an integer >= 1") from error
    if isinstance(value, float) or parsed < 1:
        raise ValueError("REEL_SHORTLIST_SIZE must be an integer >= 1")
    return parsed


def _clamped_progress(value: float, minimum: float, excellent: float) -> float:
    if excellent <= minimum:
        raise ValueError("score range must increase")
    return min(1.0, max(0.0, (value - minimum) / (excellent - minimum)))


def _score_visual_information(image_path: Path) -> float:
    """Return a small, deterministic image-statistics score (0-10)."""
    with Image.open(image_path) as image:
        grayscale = image.convert("L")
        # Bound work without changing the source-dependent result category.
        grayscale.thumbnail((256, 256))
        histogram = grayscale.histogram()
        pixel_count = sum(histogram)
        entropy = -sum(
            (count / pixel_count) * math.log2(count / pixel_count)
            for count in histogram
            if count and pixel_count
        )
        contrast = ImageStat.Stat(grayscale).stddev[0]
        pixels = list(grayscale.getdata())
        width, height = grayscale.size
        if width < 2 or height < 2:
            edge_variation = 0.0
        else:
            horizontal = sum(
                abs(pixels[row * width + column] - pixels[row * width + column - 1])
                for row in range(height)
                for column in range(1, width)
            )
            vertical = sum(
                abs(pixels[row * width + column] - pixels[(row - 1) * width + column])
                for row in range(1, height)
                for column in range(width)
            )
            comparisons = height * (width - 1) + (height - 1) * width
            edge_variation = (horizontal + vertical) / comparisons

    return round(
        min(5.0, entropy / 8.0 * 5.0)
        + min(3.0, contrast / 64.0 * 3.0)
        + min(2.0, edge_variation / 32.0 * 2.0),
        4,
    )


def _score_candidate(artwork: NormalizedArtwork, image_path: Path, width: int, height: int) -> ReelScoreBreakdown:
    short_side = min(width, height)
    long_side = max(width, height)
    pixel_count = width * height
    aspect_ratio = long_side / short_side
    technical_weight = SCORE_WEIGHTS["technicalQuality"]
    detail_weight = SCORE_WEIGHTS["detailHeadroom"]
    metadata_weight = SCORE_WEIGHTS["metadataCompleteness"]
    composition_weight = SCORE_WEIGHTS["compositionFlexibility"]

    technical_quality = round(
        technical_weight * 0.5 * _clamped_progress(short_side, MIN_REEL_SHORT_SIDE, 2160)
        + technical_weight / 3.0 * _clamped_progress(pixel_count, 1_000_000, 9_000_000)
        + technical_weight / 6.0 * _clamped_progress(long_side, MIN_REEL_LONG_SIDE, 3840),
        4,
    )
    detail_headroom = round(
        detail_weight * 0.64 * _clamped_progress(short_side, MIN_REEL_SHORT_SIDE, 2400)
        + detail_weight * 0.36 * _clamped_progress(pixel_count, 1_000_000, 12_000_000),
        4,
    )

    metadata_fields = (
        artwork.title,
        artwork.artist_name,
        artwork.creation_date,
        artwork.medium,
        artwork.museum_name,
        artwork.classification,
    )
    metadata_completeness = metadata_weight - 2.0 + (
        2.0 if isinstance(artwork.artwork_url, str) and artwork.artwork_url.strip() else 0.0
    )

    ordinary_ratio = 1.0 if aspect_ratio <= 2.0 else _clamped_progress(3.0 - aspect_ratio, 0.0, 1.0)
    composition_flexibility = round(
        composition_weight * (7.0 / 15.0) * ordinary_ratio
        + composition_weight / 3.0 * _clamped_progress(short_side, MIN_REEL_SHORT_SIDE, 1800)
        + composition_weight / 5.0 * _clamped_progress(pixel_count, 1_000_000, 6_000_000),
        4,
    )

    # Keep the field tuple evaluated here: all fields were hard-gated through
    # the same handoff metadata validator before scoring.
    assert all(isinstance(value, str) and value.strip() for value in metadata_fields)
    return ReelScoreBreakdown(
        technical_quality=technical_quality,
        detail_headroom=detail_headroom,
        metadata_completeness=metadata_completeness,
        composition_flexibility=composition_flexibility,
        visual_information=_score_visual_information(image_path),
    )


def validate_reel_candidate(candidate: ReelCandidate) -> ReelCandidateDecision:
    """Apply the existing Reel hard gates without computing a ranking score.

    Candidate acquisition needs this narrow boundary to decide whether a safe
    local asset may enter the handoff pool.  Ranking remains solely in
    ``select_reel_candidates``.
    """
    artwork = candidate.artwork
    reasons: list[str] = []
    source_image = Path(candidate.local_image_path).expanduser()

    if not isinstance(artwork.source, str) or not artwork.source.strip() or not isinstance(artwork.source_id, str) or not artwork.source_id.strip():
        reasons.append("CANONICAL_ID_MISSING")
    if not artwork.is_public_domain or artwork.rights_status != HANDOFF_RIGHTS_STATUS:
        reasons.append("RIGHTS_NOT_CONFIRMED")

    try:
        _required_metadata("canonicalId", artwork.canonical_id)
        for field, value in (
            ("title", artwork.title),
            ("artist", artwork.artist_name),
            ("date", artwork.creation_date),
            ("medium", artwork.medium),
            ("museum", artwork.museum_name),
            ("classification", artwork.classification),
        ):
            _required_metadata(field, value)
    except ReelHandoffExportError:
        reasons.append("METADATA_INCOMPLETE")

    if not source_image.is_file():
        reasons.append("IMAGE_MISSING")
    else:
        secure_validation = validate_local_image_file(str(source_image))
        if not secure_validation.valid:
            reasons.append("IMAGE_INVALID")
        else:
            try:
                width, height = _measure_validated_image(source_image, artwork)
            except ReelHandoffExportError as error:
                message = str(error)
                reasons.append("IMAGE_DIMENSIONS_INVALID" if "dimensions" in message else "IMAGE_INVALID")
            else:
                if min(width, height) < MIN_REEL_SHORT_SIDE or max(width, height) < MIN_REEL_LONG_SIDE:
                    reasons.append("RESOLUTION_TOO_LOW")
                if not reasons:
                    return ReelCandidateDecision(
                        candidate=candidate,
                        eligible=True,
                        image_width=width,
                        image_height=height,
                    )

    return ReelCandidateDecision(candidate=candidate, eligible=False, rejection_reasons=tuple(sorted(set(reasons))))


def _evaluate_candidate(candidate: ReelCandidate) -> ReelCandidateDecision:
    """Apply hard gates and then calculate the selector-only ranking score."""
    decision = validate_reel_candidate(candidate)
    if not decision.eligible:
        return decision
    assert decision.image_width is not None and decision.image_height is not None
    return replace(
        decision,
        score_breakdown=_score_candidate(
            decision.candidate.artwork,
            Path(decision.candidate.local_image_path).expanduser(),
            decision.image_width,
            decision.image_height,
        ),
    )


def select_reel_candidates(
    candidates: Iterable[ReelCandidate],
    shortlist_size: int | str | None = None,
    environment: dict[str, str] | None = None,
) -> ReelSelectionResult:
    """Hard-gate, score, and deterministically shortlist supplied local candidates.

    Ties are ordered by total score, technical quality, detail headroom, then
    canonical ID in ascending lexical order.
    """
    resolved_shortlist_size = resolve_shortlist_size(shortlist_size, environment)
    evaluated = tuple(_evaluate_candidate(candidate) for candidate in candidates)
    eligible = [decision for decision in evaluated if decision.eligible]
    rejected = tuple(decision for decision in evaluated if not decision.eligible)
    eligible.sort(
        key=lambda decision: (
            -decision.reel_pre_planner_score,
            -decision.score_breakdown.technical_quality,
            -decision.score_breakdown.detail_headroom,
            decision.candidate.artwork.canonical_id,
        )
    )
    shortlist = tuple(eligible[:resolved_shortlist_size])
    result = ReelSelectionResult(
        shortlist=shortlist,
        rejected=rejected,
        candidate_count=len(evaluated),
        eligible_count=len(eligible),
        shortlist_size=resolved_shortlist_size,
    )
    logger.info(
        "[reel-selector] candidates=%s eligible=%s rejected=%s shortlist=%s",
        result.candidate_count,
        len(eligible),
        len(result.rejected),
        len(result.shortlist),
    )
    for rank, decision in enumerate(result.shortlist, start=1):
        breakdown = decision.score_breakdown
        logger.info(
            "[reel-selector] #%s %s score=%.4f technical=%.4f detail=%.4f metadata=%.4f composition=%.4f visual=%.4f",
            rank,
            decision.candidate.artwork.canonical_id,
            decision.reel_pre_planner_score,
            breakdown.technical_quality,
            breakdown.detail_headroom,
            breakdown.metadata_completeness,
            breakdown.composition_flexibility,
            breakdown.visual_information,
        )
    return result


def _shortlist_manifest_entry(rank: int, decision: ReelCandidateDecision) -> dict[str, object]:
    artwork = decision.candidate.artwork
    return {
        "rank": rank,
        "canonicalId": artwork.canonical_id,
        "source": artwork.source,
        "title": artwork.title,
        "artist": artwork.artist_name,
        "imagePath": str(Path(decision.candidate.local_image_path).expanduser().resolve()),
        "imageWidth": decision.image_width,
        "imageHeight": decision.image_height,
        "reelPrePlannerScore": decision.reel_pre_planner_score,
        "scoreBreakdown": decision.score_breakdown.as_manifest(),
    }


def write_selection_manifest(
    selection: ReelSelectionResult,
    output_path: str | Path = DEFAULT_SELECTION_MANIFEST,
    generated_at: datetime | None = None,
) -> Path:
    """Write the generated selection manifest atomically, without source URLs."""
    timestamp = generated_at or datetime.now(UTC)
    manifest = {
        "selectionVersion": SELECTION_VERSION,
        "generatedAt": timestamp.isoformat(),
        "candidateCount": selection.candidate_count,
        "eligibleCount": selection.eligible_count,
        "rejectedCount": len(selection.rejected),
        "shortlistSize": selection.shortlist_size,
        "shortlist": [
            _shortlist_manifest_entry(rank, decision)
            for rank, decision in enumerate(selection.shortlist, start=1)
        ],
        "rejected": [
            {
                "canonicalId": decision.candidate.artwork.canonical_id,
                "eligible": False,
                "rejectionReasons": list(decision.rejection_reasons),
            }
            for decision in selection.rejected
        ],
    }
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".reel-selection-", suffix=".tmp", delete=False
    ) as temporary:
        json.dump(manifest, temporary, indent=2, ensure_ascii=False)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination
