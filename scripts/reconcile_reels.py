#!/usr/bin/env python3
"""Run bounded, conservative Reel publication reconciliation once.

This wrapper never publishes anything; all behavior lives in the existing
``reconcile_reel_publications()`` implementation.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.reel_reconciliation import reconcile_reel_publications  # noqa: E402

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded, conservative Reel publication reconciliation"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of unresolved reservations to inspect",
    )
    args = parser.parse_args(argv)

    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not access_token:
        logger.error("Reel reconciliation aborted: INSTAGRAM_ACCESS_TOKEN must be set")
        return 1

    try:
        summary = reconcile_reel_publications(
            access_token=access_token, limit=args.limit
        )
    except Exception as error:
        logger.error("Reel reconciliation failed: %s", type(error).__name__)
        return 1
    logger.info(
        "Reel reconciliation completed inspected=%s confirmed_published=%s "
        "confirmed_not_published=%s still_ambiguous=%s errors=%s "
        "cleanup_deleted=%s cleanup_failures=%s",
        summary.inspected,
        summary.confirmed_published,
        summary.confirmed_not_published,
        summary.still_ambiguous,
        summary.errors,
        summary.cleanup_deleted,
        summary.cleanup_failures,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    raise SystemExit(main())
