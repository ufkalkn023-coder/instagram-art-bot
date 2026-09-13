#!/usr/bin/env python3
"""Produce and publish exactly one verified Artfolio Reel."""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.reel_production import (  # noqa: E402
    ReelProductionCommandError,
    ReelProductionSelectionError,
    ReelReleaseVerificationError,
    produce_and_publish_reel,
)

logger = logging.getLogger(__name__)

_MESSAGE_SAFE_ERRORS = (
    ReelProductionSelectionError,
    ReelProductionCommandError,
    ReelReleaseVerificationError,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Produce and publish exactly one verified Artfolio Reel"
    )
    parser.add_argument(
        "--artfolio-reels-root",
        required=True,
        type=Path,
        help="Path to the pinned artfolio-reels checkout",
    )
    args = parser.parse_args(argv)

    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not account_id or not access_token:
        logger.error(
            "Reel production aborted: INSTAGRAM_ACCOUNT_ID and "
            "INSTAGRAM_ACCESS_TOKEN must both be set"
        )
        return 1

    try:
        outcome = produce_and_publish_reel(
            reels_repository=args.artfolio_reels_root,
            account_id=account_id,
            access_token=access_token,
        )
    except _MESSAGE_SAFE_ERRORS as error:
        logger.error("Reel production failed: %s: %s", type(error).__name__, error)
        return 1
    except Exception as error:
        logger.error("Reel production failed: %s", type(error).__name__)
        return 1
    logger.info(
        "Reel published publication_id=%s artwork=%s media_id=%s release=%s",
        outcome.publication.id,
        outcome.canonical_id,
        outcome.publication.media_id,
        outcome.release_directory,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    raise SystemExit(main())
