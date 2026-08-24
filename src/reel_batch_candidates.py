"""Produce a deterministic, rights-safe queue for the Remotion Reel batch runner.

This is the Art Bot-owned side of the cross-repository boundary. It only
consumes previously exported local Reel handoffs, reuses the approved
pre-selector, portfolio ordering, and handoff exporter, and prints a compact
JSON contract for Remotion. It never fetches museum data or writes Reel
history.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import config

from src.models import NormalizedArtwork
from src.reel_handoff import DEFAULT_HANDOFF_DIRECTORY, export_reel_handoff
from src.reel_portfolio import resolve_selection_target, select_portfolio_candidates
from src.reel_selector import ReelCandidate, select_reel_candidates


BATCH_CANDIDATE_VERSION = "reel-batch-candidates-v1"
DEFAULT_BATCH_CANDIDATE_LIMIT = 8
REEL_BATCH_CANDIDATE_LIMIT_ENV = "REEL_BATCH_CANDIDATE_LIMIT"
DEFAULT_BATCH_HANDOFF_DIRECTORY = Path(config.BASE_DIR) / "output" / "reel-batches"
REEL_PRODUCTION_HISTORY_VERSION = "reel-production-history-v1"


@dataclass(frozen=True)
class BatchCandidateStageCounts:
    """Compact, cross-process visibility into the deterministic queue boundary."""

    source_handoffs: int
    history_excluded_at_boundary: int
    acquired_usable: int
    preselector_eligible: int
    portfolio_available: int
    queued: int
    preselector_rejection_counts: tuple[tuple[str, int], ...]

    def as_contract(self) -> dict[str, object]:
        return {
            "sourceHandoffs": self.source_handoffs,
            "historyExcludedAtBoundary": self.history_excluded_at_boundary,
            "acquiredUsable": self.acquired_usable,
            "preselectorEligible": self.preselector_eligible,
            "portfolioAvailable": self.portfolio_available,
            "queued": self.queued,
            "preselectorRejectionCounts": dict(self.preselector_rejection_counts),
        }


@dataclass(frozen=True)
class BatchCandidateQueue:
    target: int
    candidate_limit: int
    candidate_count: int
    candidates: tuple[dict[str, object], ...]
    stage_counts: BatchCandidateStageCounts

    def as_contract(self) -> dict[str, object]:
        return {
            "batchCandidateVersion": BATCH_CANDIDATE_VERSION,
            "target": self.target,
            "candidateLimit": self.candidate_limit,
            "candidateCount": self.candidate_count,
            "candidates": list(self.candidates),
            "stageCounts": self.stage_counts.as_contract(),
        }


def resolve_batch_candidate_limit(
    target: int,
    value: int | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Resolve the queue depth without creating a second production target."""
    if value is None:
        environment = os.environ if environment is None else environment
        value = environment.get(REEL_BATCH_CANDIDATE_LIMIT_ENV, DEFAULT_BATCH_CANDIDATE_LIMIT)
    if isinstance(value, bool):
        raise ValueError("REEL_BATCH_CANDIDATE_LIMIT must be an integer >= REEL_SELECTION_TARGET")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("REEL_BATCH_CANDIDATE_LIMIT must be an integer >= REEL_SELECTION_TARGET") from error
    if isinstance(value, float) or parsed < target:
        raise ValueError("REEL_BATCH_CANDIDATE_LIMIT must be an integer >= REEL_SELECTION_TARGET")
    return parsed


def _source_id_for(canonical_id: str, source: str) -> str:
    prefix = f"{source}_"
    return canonical_id[len(prefix):] if canonical_id.startswith(prefix) else canonical_id


def _candidate_from_handoff(path: Path) -> ReelCandidate:
    raw = json.loads(path.read_text(encoding="utf-8"))
    canonical_id = raw["canonicalId"]
    source = raw["source"]
    artwork = NormalizedArtwork(
        source=source,
        source_id=_source_id_for(canonical_id, source),
        title=raw["title"],
        artist_name=raw["artist"],
        creation_date=raw["date"],
        medium=raw["medium"],
        museum_name=raw["museum"],
        classification=raw["classification"],
        is_public_domain=raw["rightsStatus"] == "CONFIRMED_PUBLIC_DOMAIN",
        rights_status=raw["rightsStatus"],
        image_width=raw["imageWidth"],
        image_height=raw["imageHeight"],
    )
    return ReelCandidate(artwork=artwork, local_image_path=raw["imagePath"])


def _handoff_paths(source_directory: Path) -> tuple[Path, ...]:
    if not source_directory.exists():
        return ()
    return tuple(sorted(path for path in source_directory.glob("*.json") if path.is_file()))


def _canonical_id_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None


def load_reel_production_history(path: str | Path | None) -> tuple[dict[str, str], ...]:
    """Read only the selector fields from the Remotion-owned production ledger."""
    if path is None:
        return ()
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Reel production history: {source}") from error
    if not isinstance(payload, dict) or payload.get("version") != REEL_PRODUCTION_HISTORY_VERSION:
        raise ValueError(f"invalid Reel production history: {source}")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"invalid Reel production history: {source}")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") not in {"QC_PASSED", "RENDERED"}:
            raise ValueError(f"invalid Reel production history: {source}")
        canonical_id, artist, museum, source_name, template, batch_id, qc_passed_at = (
            entry.get("canonicalId"), entry.get("artist"), entry.get("museum"), entry.get("source"),
            entry.get("template"), entry.get("batchId"), entry.get("qcPassedAt"),
        )
        if not all(isinstance(value, str) and value.strip() for value in (canonical_id, artist, museum, source_name, template, batch_id, qc_passed_at)):
            raise ValueError(f"invalid Reel production history: {source}")
        if canonical_id in seen_ids or (entry["status"] == "RENDERED" and not all(isinstance(value, str) and value.strip() for value in (entry.get("renderedAt"), entry.get("renderPath")))):
            raise ValueError(f"invalid Reel production history: {source}")
        if entry["status"] == "QC_PASSED" and ("renderedAt" in entry or "renderPath" in entry):
            raise ValueError(f"invalid Reel production history: {source}")
        seen_ids.add(canonical_id)
        normalized.append({"canonicalId": canonical_id, "artist": artist, "museum": museum})
    return tuple(normalized)


def build_batch_candidate_queue(
    *,
    target: int | str | None = None,
    candidate_limit: int | str | None = None,
    source_directory: str | Path = DEFAULT_HANDOFF_DIRECTORY,
    output_directory: str | Path = DEFAULT_BATCH_HANDOFF_DIRECTORY,
    reel_history_path: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> BatchCandidateQueue:
    """Create/reuse selected handoffs and return the stable Remotion queue contract."""
    resolved_target = resolve_selection_target(target, environment)
    resolved_limit = resolve_batch_candidate_limit(resolved_target, candidate_limit, environment)
    source_root = Path(source_directory).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()
    all_candidates: list[ReelCandidate] = []
    for handoff_path in _handoff_paths(source_root):
        try:
            all_candidates.append(_candidate_from_handoff(handoff_path))
        except (KeyError, OSError, json.JSONDecodeError, ValueError):
            continue

    # Acquisition deliberately retains historical handoffs locally.  Exclude
    # them before truncating the preselector shortlist; otherwise historical
    # records can consume all queue slots and only be removed by portfolio.
    history = load_reel_production_history(reel_history_path)
    produced_ids = {
        key
        for entry in history
        if (key := _canonical_id_key(entry["canonicalId"])) is not None
    }
    candidates = [
        candidate
        for candidate in all_candidates
        if _canonical_id_key(candidate.artwork.canonical_id) not in produced_ids
    ]
    shortlist = select_reel_candidates(candidates, shortlist_size=resolved_limit, environment=environment)
    portfolio = select_portfolio_candidates(
        shortlist.shortlist, target=resolved_limit,
        recent_reel_history=history, environment=environment,
    )
    entries: list[dict[str, object]] = []
    for item in portfolio.selected:
        decision = item.candidate_decision
        handoff_path = export_reel_handoff(
            decision.candidate.artwork, decision.candidate.local_image_path, output_root / "handoffs"
        )
        artwork = decision.candidate.artwork
        entries.append({
            "canonicalId": artwork.canonical_id,
            "artist": artwork.artist_name,
            "museum": artwork.museum_name,
            "handoffPath": str(handoff_path),
            "baseScore": item.reel_pre_planner_score,
            "portfolioPriorityScore": item.portfolio_priority_score,
        })
    rejection_counts = Counter(
        reason
        for decision in shortlist.rejected
        for reason in decision.rejection_reasons
    )
    stage_counts = BatchCandidateStageCounts(
        source_handoffs=len(all_candidates),
        history_excluded_at_boundary=len(all_candidates) - len(candidates),
        acquired_usable=len(candidates),
        preselector_eligible=shortlist.eligible_count,
        portfolio_available=portfolio.available_candidate_count,
        queued=len(entries),
        preselector_rejection_counts=tuple(sorted(rejection_counts.items())),
    )
    return BatchCandidateQueue(resolved_target, resolved_limit, len(entries), tuple(entries), stage_counts)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the automatic, safe Artfolio Reel candidate queue")
    parser.add_argument("--target", type=int)
    parser.add_argument("--candidate-limit", type=int)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_HANDOFF_DIRECTORY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_BATCH_HANDOFF_DIRECTORY)
    parser.add_argument("--output", type=Path, help="Optional queue JSON file; stdout remains the contract")
    parser.add_argument("--reel-history", type=Path, help="Remotion-owned Reel production ledger")
    args = parser.parse_args()
    queue = build_batch_candidate_queue(
        target=args.target, candidate_limit=args.candidate_limit,
        source_directory=args.source_dir, output_directory=args.output_dir, reel_history_path=args.reel_history,
    )
    contract = queue.as_contract()
    if args.output:
        _write_json_atomically(args.output.expanduser().resolve(), contract)
    print(json.dumps(contract, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
