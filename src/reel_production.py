"""Single-Reel production orchestration: acquisition, selection, one handoff.

Composes the existing candidate acquisition and batch-candidate queue pipeline and
returns exactly one selected handoff for a one-run/one-Reel production. This slice
performs no rendering, no artfolio-reels invocation, no Instagram publication, and no
Reel reservation or R2 history mutation.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from src import reel_batch_candidates, reel_candidate_acquisition

logger = logging.getLogger(__name__)


class ReelProductionSelectionError(RuntimeError):
    """No eligible Reel candidate survived acquisition and selection."""


@dataclass(frozen=True)
class ReelProductionSelection:
    canonical_id: str
    handoff_path: Path
    acquisition: reel_candidate_acquisition.AcquisitionResult
    queue: reel_batch_candidates.BatchCandidateQueue


def produce_reel_handoff(
    *,
    pool_size: int | str | None = None,
    attempt_limit: int | str | None = None,
    handoff_directory: str | Path = reel_candidate_acquisition.DEFAULT_HANDOFF_DIRECTORY,
    manifest_path: str | Path | None = reel_candidate_acquisition.DEFAULT_ACQUISITION_MANIFEST,
    work_directory: str | Path = reel_candidate_acquisition.DEFAULT_ACQUISITION_WORK_DIRECTORY,
    batch_output_directory: str | Path = reel_batch_candidates.DEFAULT_BATCH_HANDOFF_DIRECTORY,
    excluded_canonical_ids: Sequence[str] = (),
    selection_target: int | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ReelProductionSelection:
    """Acquire one bounded handoff pool and select exactly one handoff.

    Rights validation, image validation, scoring, portfolio ordering, handoff
    serialization, and duplicate exclusion all remain inside the existing pipeline
    calls. Remotion production-history exclusions enter through
    ``excluded_canonical_ids``; the pinned reels repository applies its own production
    history again at planning time.
    """
    acquisition = reel_candidate_acquisition.acquire_reel_candidate_pool(
        pool_size=pool_size,
        attempt_limit=attempt_limit,
        environment=environment,
        handoff_directory=handoff_directory,
        manifest_path=manifest_path,
        work_directory=work_directory,
        excluded_canonical_ids=excluded_canonical_ids,
    )
    queue = reel_batch_candidates.build_batch_candidate_queue(
        target=selection_target,
        source_directory=handoff_directory,
        output_directory=batch_output_directory,
        environment=environment,
    )
    if not queue.candidates:
        raise ReelProductionSelectionError(
            "No eligible Reel candidate survived acquisition and selection"
        )
    selected = queue.candidates[0]
    logger.info(
        "reel_handoff_selected canonical_id=%s handoff=%s",
        selected["canonicalId"],
        selected["handoffPath"],
    )
    return ReelProductionSelection(
        canonical_id=str(selected["canonicalId"]),
        handoff_path=Path(str(selected["handoffPath"])),
        acquisition=acquisition,
        queue=queue,
    )
