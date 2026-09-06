"""Acquire a safe, local artwork pool before the existing Reel selector runs.

This module owns sourcing only. It reuses the active museum registry, secure
image downloader, Reel hard gates, and handoff exporter. It receives only
Remotion-normalized production exclusion IDs; it never reads history, calls
Gemini, publishes, or runs the selector's ranking logic.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

from src.art_fetcher import _museum_adapters, _museum_adapter_rng, resolve_selection_run_seed
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult, validate_and_download_image_with_metadata
from src.reel_batch_candidates import _candidate_from_handoff, resolve_batch_candidate_limit
from src.reel_handoff import DEFAULT_HANDOFF_DIRECTORY, ReelHandoffExportError, _required_metadata, export_reel_handoff
from src.reel_portfolio import resolve_selection_target
from src.reel_selector import ReelCandidate, ReelCandidateDecision, validate_reel_candidate
from src.source_health import (
    FAIL_FAST_SOURCE_FAILURE_CATEGORIES,
    classify_exception,
    normalize_source_failure_category,
)


UTC = timezone.utc


ACQUISITION_VERSION = "reel-candidate-acquisition-v2"
REEL_CANDIDATE_POOL_SIZE_ENV = "REEL_CANDIDATE_POOL_SIZE"
REEL_ACQUISITION_MAX_ATTEMPTS_ENV = "REEL_ACQUISITION_MAX_ATTEMPTS"
DEFAULT_REEL_CANDIDATE_POOL_SIZE = 24
DEFAULT_REEL_ACQUISITION_MAX_ATTEMPTS = 80
DEFAULT_ACQUISITION_MANIFEST = Path(config.BASE_DIR) / "output" / "reel-selection" / "acquisition.json"
DEFAULT_ACQUISITION_WORK_DIRECTORY = Path(config.BASE_DIR) / "output" / "reel-acquisition"

logger = logging.getLogger(__name__)

# These adapters already treat these variables as required.  Keeping the
# availability note here makes an expected operational omission visible in the
# acquisition manifest without changing adapter behavior.
_REQUIRED_CREDENTIAL_ENV = {
    "smithsonian": "SMITHSONIAN_API_KEY",
    "europeana": "EUROPEANA_API_KEY",
}


@dataclass(frozen=True)
class AcquisitionResult:
    manifest: dict[str, object]

    @property
    def safe_candidate_count(self) -> int:
        return int(self.manifest["safeCandidateCount"])


def _parse_positive_integer(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer >= {minimum}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer >= {minimum}") from error
    if isinstance(value, float) or parsed < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return parsed


def resolve_reel_candidate_pool_size(
    value: int | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Resolve the desired safe handoff depth against the existing queue size."""
    environment = os.environ if environment is None else environment
    if value is None:
        value = environment.get(REEL_CANDIDATE_POOL_SIZE_ENV, DEFAULT_REEL_CANDIDATE_POOL_SIZE)
    target = resolve_selection_target(environment=environment)
    candidate_limit = resolve_batch_candidate_limit(target, environment=environment)
    return _parse_positive_integer(value, REEL_CANDIDATE_POOL_SIZE_ENV, candidate_limit)


def resolve_reel_acquisition_attempt_limit(
    pool_size: int,
    value: int | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Resolve a finite examination/download budget for one acquisition run."""
    environment = os.environ if environment is None else environment
    if value is None:
        value = environment.get(REEL_ACQUISITION_MAX_ATTEMPTS_ENV, DEFAULT_REEL_ACQUISITION_MAX_ATTEMPTS)
    return _parse_positive_integer(value, REEL_ACQUISITION_MAX_ATTEMPTS_ENV, pool_size)


def _reason_from_decision(decision: ReelCandidateDecision) -> str:
    reasons = set(decision.rejection_reasons)
    if "RIGHTS_NOT_CONFIRMED" in reasons:
        return "RIGHTS_REJECTED"
    if "METADATA_INCOMPLETE" in reasons or "CANONICAL_ID_MISSING" in reasons:
        return "METADATA_INCOMPLETE"
    if "RESOLUTION_TOO_LOW" in reasons:
        return "RESOLUTION_TOO_LOW"
    return "IMAGE_VALIDATION_FAILED"


_SAFE_REASON_MESSAGES = {
    "RIGHTS_REJECTED": "Artwork does not have confirmed public-domain rights.",
    "METADATA_INCOMPLETE": "Artwork is missing required Reel handoff metadata.",
    "IMAGE_DOWNLOAD_FAILED": "Secure image download failed validation.",
    "IMAGE_VALIDATION_FAILED": "Local image did not pass the required validation.",
    "RESOLUTION_TOO_LOW": "Image does not meet the Reel resolution minimum.",
    "HANDOFF_FAILED": "Artwork could not be exported as a safe Reel handoff.",
    "DUPLICATE_CANONICAL_ID": "Canonical artwork ID is already present in this pool.",
    "PRODUCTION_HISTORY_DUPLICATE": "Artwork is already excluded from automatic Reel production.",
    "SOURCE_FAILED": "Museum source was unavailable for this acquisition run.",
}

_SAFE_DOWNLOAD_FAILURE_CATEGORIES = {
    "unsafe_url",
    "too_many_redirects",
    "http_status",
    "too_large",
    "invalid_content_type",
    "unsupported_format",
    "too_many_pixels",
    "image_too_small",
    "decompression_bomb",
    "invalid_image",
    "network_error",
    "file_error",
}


def _rejection(source: str, reason_code: str, canonical_id: str | None = None) -> dict[str, str]:
    value = {"source": source, "reasonCode": reason_code, "safeErrorMessage": _SAFE_REASON_MESSAGES[reason_code]}
    if canonical_id:
        value["canonicalId"] = canonical_id
    return value


def _download_rejection(source: str, canonical_id: str, result: ImageValidationResult) -> dict[str, str | int]:
    """Record bounded AIC download diagnostics without exposing URLs or response bodies."""
    rejection: dict[str, str | int] = _rejection(source, "IMAGE_DOWNLOAD_FAILED", canonical_id)
    if source != "aic":
        return rejection

    category = result.reason if result.reason in _SAFE_DOWNLOAD_FAILURE_CATEGORIES else "unknown"
    rejection["downloadFailureCategory"] = category
    if category == "http_status" and isinstance(result.http_status, int) and 100 <= result.http_status <= 599:
        rejection["httpStatus"] = result.http_status
    return rejection


def _source_stats(source: str) -> dict[str, object]:
    return {"source": source, "attempted": 0, "accepted": 0, "rejected": 0, "failed": 0, "rejectionReasons": {}}


def _adapter_source_failure_category(adapter: Any) -> str | None:
    category = getattr(adapter, "source_failure_category", None)
    return normalize_source_failure_category(category) if category is not None else None


def _handoff_acceptance(path: Path) -> tuple[NormalizedArtwork, str, ReelCandidateDecision] | None:
    try:
        candidate = _candidate_from_handoff(path)
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return None
    decision = validate_reel_candidate(candidate)
    return candidate.artwork, str(candidate.local_image_path), decision


def _accepted_entry(
    artwork: NormalizedArtwork,
    handoff_path: Path,
    image_path: str | Path,
    decision: ReelCandidateDecision,
    *,
    reused: bool,
) -> dict[str, object]:
    assert decision.image_width is not None and decision.image_height is not None
    return {
        "canonicalId": artwork.canonical_id,
        "source": artwork.source,
        "handoffPath": str(handoff_path),
        "imagePath": str(image_path),
        "imageWidth": decision.image_width,
        "imageHeight": decision.image_height,
        "reused": reused,
    }


def _write_json_atomically(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, indent=2, ensure_ascii=False)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def acquire_reel_candidate_pool(
    *,
    pool_size: int | str | None = None,
    attempt_limit: int | str | None = None,
    environment: Mapping[str, str] | None = None,
    adapters: Sequence[Any] | None = None,
    handoff_directory: str | Path = DEFAULT_HANDOFF_DIRECTORY,
    manifest_path: str | Path | None = DEFAULT_ACQUISITION_MANIFEST,
    work_directory: str | Path = DEFAULT_ACQUISITION_WORK_DIRECTORY,
    downloader: Callable[[str, str], ImageValidationResult] = validate_and_download_image_with_metadata,
    excluded_canonical_ids: Sequence[str] = (),
) -> AcquisitionResult:
    """Fill a bounded, usable handoff pool using Remotion-supplied exclusions."""
    environment = os.environ if environment is None else environment
    desired_pool_size = resolve_reel_candidate_pool_size(pool_size, environment)
    resolved_attempt_limit = resolve_reel_acquisition_attempt_limit(desired_pool_size, attempt_limit, environment)
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    download_duration_ms = 0
    handoff_root = Path(handoff_directory).expanduser().resolve()
    source_rows: dict[str, dict[str, object]] = {}
    accepted: list[dict[str, object]] = []
    rejections: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    production_history_exclusions = {canonical_id for canonical_id in excluded_canonical_ids if isinstance(canonical_id, str) and canonical_id}
    existing_safe_count = 0
    history_excluded_count = 0
    usable_existing_count = 0
    newly_acquired_count = 0

    # Valid, existing handoffs are safe to keep; invalid stale handoffs make
    # room for freshly acquired candidates instead of being silently trusted.
    for path in sorted(handoff_root.glob("*.json")) if handoff_root.exists() else ():
        reusable = _handoff_acceptance(path)
        if reusable is None:
            continue
        artwork, image_path, decision = reusable
        row = source_rows.setdefault(artwork.source, _source_stats(artwork.source))
        if not decision.eligible:
            row["rejected"] = int(row["rejected"]) + 1
            rejections.append(_rejection(artwork.source, _reason_from_decision(decision), artwork.canonical_id))
            continue
        if artwork.canonical_id in seen_ids:
            row["rejected"] = int(row["rejected"]) + 1
            rejections.append(_rejection(artwork.source, "DUPLICATE_CANONICAL_ID", artwork.canonical_id))
            continue
        seen_ids.add(artwork.canonical_id)
        existing_safe_count += 1
        if artwork.canonical_id in production_history_exclusions:
            history_excluded_count += 1
            continue
        row["accepted"] = int(row["accepted"]) + 1
        accepted.append(_accepted_entry(artwork, path, image_path, decision, reused=True))
        usable_existing_count += 1

    candidates_by_source: list[tuple[str, list[NormalizedArtwork]]] = []
    source_failures: dict[str, dict[str, str]] = {}
    unavailable_sources: set[str] = set()

    def record_source_failure(source: str, category: str) -> None:
        """Record one bounded source-wide failure for this run only."""
        category = normalize_source_failure_category(category)
        if source in source_failures:
            return
        source_failures[source] = {"category": category}
        row = source_rows.setdefault(source, _source_stats(source))
        row["failed"] = int(row["failed"]) + 1
        rejections.append(_rejection(source, "SOURCE_FAILED"))
        if category in FAIL_FAST_SOURCE_FAILURE_CATEGORIES:
            unavailable_sources.add(source)
            logger.warning("[reel-acquire] %s unavailable=%s", source, category)

    if len(accepted) < desired_pool_size:
        run_seed = resolve_selection_run_seed(dict(environment))
        for adapter in list(adapters) if adapters is not None else _museum_adapters():
            source = str(adapter.source_id)
            row = source_rows.setdefault(source, _source_stats(source))
            required_credential = _REQUIRED_CREDENTIAL_ENV.get(source)
            if required_credential and not environment.get(required_credential, "").strip():
                record_source_failure(source, "MISSING_CREDENTIAL")
                continue
            try:
                fetched = adapter.fetch_candidates(
                    limit=min(resolved_attempt_limit, 20),
                    rng=_museum_adapter_rng(run_seed, source, "reel_acquisition", None),
                )
            except Exception as error:
                record_source_failure(source, classify_exception(error))
                continue
            source_failure_category = _adapter_source_failure_category(adapter)
            if source_failure_category is not None:
                record_source_failure(source, source_failure_category)
                continue
            candidates_by_source.append((source, list(fetched)))

    positions = [0] * len(candidates_by_source)
    work_root = Path(work_directory).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=work_root) as temporary_directory:
        while len(accepted) < desired_pool_size and sum(int(row["attempted"]) for row in source_rows.values()) < resolved_attempt_limit:
            progressed = False
            for index, (source, candidates) in enumerate(candidates_by_source):
                if len(accepted) == desired_pool_size or sum(int(row["attempted"]) for row in source_rows.values()) >= resolved_attempt_limit:
                    break
                if source in unavailable_sources:
                    continue
                if positions[index] >= len(candidates):
                    continue
                progressed = True
                artwork = candidates[positions[index]]
                positions[index] += 1
                row = source_rows[source]
                row["attempted"] = int(row["attempted"]) + 1
                canonical_id = artwork.canonical_id
                if canonical_id in production_history_exclusions:
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "PRODUCTION_HISTORY_DUPLICATE", canonical_id))
                    continue
                if canonical_id in seen_ids:
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "DUPLICATE_CANONICAL_ID", canonical_id))
                    continue
                if not artwork.is_public_domain or artwork.rights_status != "CONFIRMED_PUBLIC_DOMAIN":
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "RIGHTS_REJECTED", canonical_id))
                    continue
                try:
                    _required_metadata("canonicalId", canonical_id)
                    for field, value in (("title", artwork.title), ("artist", artwork.artist_name), ("date", artwork.creation_date), ("medium", artwork.medium), ("museum", artwork.museum_name), ("classification", artwork.classification)):
                        _required_metadata(field, value)
                except ReelHandoffExportError:
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "METADATA_INCOMPLETE", canonical_id))
                    continue
                if not artwork.image_url:
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "IMAGE_DOWNLOAD_FAILED", canonical_id))
                    continue
                temporary_image = Path(temporary_directory) / f"{len(accepted)}-{canonical_id}.jpg"
                download_started = time.monotonic()
                try:
                    result = downloader(artwork.image_url, str(temporary_image))
                except Exception:
                    download_duration_ms += round((time.monotonic() - download_started) * 1000)
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "IMAGE_DOWNLOAD_FAILED", canonical_id))
                    continue
                download_duration_ms += round((time.monotonic() - download_started) * 1000)
                if not result.valid or result.width is None or result.height is None:
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_download_rejection(source, canonical_id, result))
                    if source == "aic" and result.cloudflare_challenge:
                        record_source_failure(source, "CLOUDFLARE_CHALLENGE")
                    continue
                measured = artwork.model_copy(update={"image_width": result.width, "image_height": result.height})
                decision = validate_reel_candidate(ReelCandidate(measured, temporary_image))
                if not decision.eligible:
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, _reason_from_decision(decision), canonical_id))
                    continue
                try:
                    handoff_path = export_reel_handoff(measured, temporary_image, handoff_root)
                except (OSError, ReelHandoffExportError):
                    row["rejected"] = int(row["rejected"]) + 1
                    rejections.append(_rejection(source, "HANDOFF_FAILED", canonical_id))
                    continue
                seen_ids.add(canonical_id)
                row["accepted"] = int(row["accepted"]) + 1
                accepted.append(_accepted_entry(measured, handoff_path, Path(json.loads(handoff_path.read_text(encoding="utf-8"))["imagePath"]), decision, reused=False))
                newly_acquired_count += 1
            if not progressed:
                break

    attempted_count = sum(int(row["attempted"]) for row in source_rows.values())
    rejected_count = sum(int(row["rejected"]) for row in source_rows.values())
    source_failure_count = sum(int(row["failed"]) for row in source_rows.values())
    rejection_counts_by_source: dict[str, dict[str, int]] = {}
    for rejection in rejections:
        reason_code = rejection["reasonCode"]
        # SOURCE_FAILED belongs to the existing failed total, not a candidate
        # rejection. Every other emitted code already increments rejected.
        if reason_code == "SOURCE_FAILED":
            continue
        source_counts = rejection_counts_by_source.setdefault(rejection["source"], {})
        source_counts[reason_code] = source_counts.get(reason_code, 0) + 1
    for source, row in source_rows.items():
        row["rejectionReasons"] = dict(sorted(rejection_counts_by_source.get(source, {}).items()))
    finished_at = datetime.now(UTC)
    manifest: dict[str, object] = {
        "acquisitionVersion": ACQUISITION_VERSION,
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "desiredPoolSize": desired_pool_size,
        "attemptLimit": resolved_attempt_limit,
        "attemptedCount": attempted_count,
        "safeCandidateCount": len(accepted),
        "existingSafeCount": existing_safe_count,
        "historyExcludedCount": history_excluded_count,
        "usableExistingCount": usable_existing_count,
        "newlyAcquiredCount": newly_acquired_count,
        "usableSafeCandidateCount": len(accepted),
        "rejectedCount": rejected_count,
        "sourceFailureCount": source_failure_count,
        "sourceFailures": dict(sorted(source_failures.items())),
        "shortfall": max(0, desired_pool_size - len(accepted)),
        "acquisitionDurationMs": round((time.monotonic() - started_monotonic) * 1000),
        "downloadDurationMs": download_duration_ms,
        "sources": list(source_rows.values()),
        "acceptedCandidates": accepted,
        "rejections": rejections,
    }
    if manifest_path is not None:
        _write_json_atomically(Path(manifest_path).expanduser().resolve(), manifest)
    return AcquisitionResult(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Acquire a safe local Artfolio Reel handoff pool")
    parser.add_argument("--pool-size", type=int)
    parser.add_argument("--attempt-limit", type=int)
    parser.add_argument("--handoff-dir", type=Path, default=DEFAULT_HANDOFF_DIRECTORY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_ACQUISITION_MANIFEST)
    parser.add_argument("--excluded-canonical-id", action="append", default=[], help="Remotion-supplied automatic-production exclusion ID")
    args = parser.parse_args()
    result = acquire_reel_candidate_pool(
        pool_size=args.pool_size,
        attempt_limit=args.attempt_limit,
        handoff_directory=args.handoff_dir,
        manifest_path=args.manifest,
        excluded_canonical_ids=args.excluded_canonical_id,
    )
    print(json.dumps(result.manifest, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
