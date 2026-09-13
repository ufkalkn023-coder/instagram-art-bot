"""Single-Reel production orchestration: acquisition, selection, handoff, release.

Composes the existing candidate acquisition and batch-candidate queue pipeline and
returns exactly one selected handoff for a one-run/one-Reel production, then drives an
existing pinned ``artfolio-reels`` checkout through its real production, packaging, and
deep-verification commands. This module never publishes to Instagram, reserves
publication state, or mutates Reel history.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src import reel_batch_candidates, reel_candidate_acquisition

logger = logging.getLogger(__name__)

REEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_REELS_CHECKOUT_SCRIPTS = ("reel.ts", "package-release.ts", "verify-release.ts")


class ReelProductionSelectionError(RuntimeError):
    """No eligible Reel candidate survived acquisition and selection."""


class ReelProductionCommandError(RuntimeError):
    """One Reel production command failed or the checkout is unusable."""


class ReelReleaseVerificationError(RuntimeError):
    """Deep verification did not prove a valid release."""


@dataclass(frozen=True)
class ReelProductionSelection:
    canonical_id: str
    handoff_path: Path
    acquisition: reel_candidate_acquisition.AcquisitionResult
    queue: reel_batch_candidates.BatchCandidateQueue


@dataclass(frozen=True)
class ReelReleaseStage:
    reel_id: str
    handoff_path: Path
    release_directory: Path


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


def _validated_reels_repository(reels_repository: str | Path) -> Path:
    root = Path(reels_repository).expanduser().resolve()
    if not root.is_dir() or not (root / "package.json").is_file():
        raise ReelProductionCommandError(
            "artfolio-reels checkout is missing or incomplete"
        )
    for script in _REELS_CHECKOUT_SCRIPTS:
        if not (root / "scripts" / script).is_file():
            raise ReelProductionCommandError(
                f"artfolio-reels checkout is missing scripts/{script}"
            )
    return root


def _stage_selection_handoff(
    selection: ReelProductionSelection, reels_root: Path
) -> tuple[str, Path]:
    reel_id = selection.canonical_id
    if not REEL_ID_PATTERN.match(reel_id):
        raise ReelProductionCommandError(
            "Selected handoff id is not a safe reel id"
        )
    destination = reels_root / "handoffs" / f"{reel_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = Path(selection.handoff_path).read_bytes()
    if destination.exists():
        if destination.read_bytes() != source_bytes:
            raise ReelProductionCommandError(
                f"Conflicting staged handoff for {reel_id}"
            )
        return reel_id, destination
    staged_tmp = destination.with_name(f"{destination.name}.tmp")
    staged_tmp.write_bytes(source_bytes)
    staged_tmp.replace(destination)
    return reel_id, destination


def _run_reel_command(
    command_runner: Callable[..., Any],
    command: list[str],
    *,
    cwd: Path,
    label: str,
):
    try:
        completed = command_runner(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ReelProductionCommandError(
            f"Reel {label} command could not start"
        ) from error
    if completed.returncode != 0:
        logger.warning(
            "reel_command_failed label=%s exit=%s stderr=%s",
            label,
            completed.returncode,
            (completed.stderr or "")[-500:],
        )
        raise ReelProductionCommandError(
            f"Reel {label} command failed with exit {completed.returncode}"
        )
    return completed


def _parse_reel_verifier_json(stdout: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            return json.loads("\n".join(lines[index:]))
    raise ValueError("no JSON object found")


def _verified_reel_release_directory(stdout: str, reel_id: str) -> Path:
    try:
        payload = _parse_reel_verifier_json(stdout)
    except ValueError as error:
        raise ReelReleaseVerificationError(
            "Reel deep verification output was not valid JSON"
        ) from error
    if payload.get("valid") is not True:
        raise ReelReleaseVerificationError(
            "Reel deep verification did not report a valid release"
        )
    if payload.get("errors") != []:
        raise ReelReleaseVerificationError(
            "Reel deep verification reported errors"
        )
    if payload.get("reelId") != reel_id:
        raise ReelReleaseVerificationError(
            "Reel deep verification reel id does not match the selected handoff"
        )
    directory = payload.get("directory")
    if not isinstance(directory, str) or not directory:
        raise ReelReleaseVerificationError(
            "Reel deep verification reported no release directory"
        )
    release_directory = Path(directory)
    if not release_directory.is_dir():
        raise ReelReleaseVerificationError(
            "Reel deep verification directory does not exist"
        )
    return release_directory


def produce_verified_reel_release(
    selection: ReelProductionSelection,
    *,
    reels_repository: str | Path,
    command_runner: Callable[..., Any] | None = None,
) -> ReelReleaseStage:
    """Drive one selected handoff through the pinned reels checkout to a verified release.

    Runs the existing production commands in order inside ``reels_repository``:
    ``npm run reel -- <staged-handoff> --render``, ``npm run package -- <reel-id>``,
    then ``npm run reels:verify-release -- <reel-id> --deep --json``. The reel id is
    the selected handoff's canonical id (identical to the reels repository's
    ``artifactIdFor`` output for exported handoffs). Any command or verification
    failure fails closed without retries and without any later stage running. No
    publication, reservation, or R2 mutation happens here.
    """
    runner = command_runner or subprocess.run
    reels_root = _validated_reels_repository(reels_repository)
    reel_id, staged_handoff = _stage_selection_handoff(selection, reels_root)

    _run_reel_command(
        runner,
        ["npm", "run", "reel", "--", str(staged_handoff), "--render"],
        cwd=reels_root,
        label="reel",
    )
    _run_reel_command(
        runner,
        ["npm", "run", "package", "--", reel_id],
        cwd=reels_root,
        label="package",
    )
    verified = _run_reel_command(
        runner,
        ["npm", "run", "reels:verify-release", "--", reel_id, "--deep", "--json"],
        cwd=reels_root,
        label="reels:verify-release",
    )
    release_directory = _verified_reel_release_directory(verified.stdout, reel_id)
    logger.info(
        "reel_release_verified reel_id=%s release=%s",
        reel_id,
        release_directory,
    )
    return ReelReleaseStage(
        reel_id=reel_id,
        handoff_path=staged_handoff,
        release_directory=release_directory,
    )
