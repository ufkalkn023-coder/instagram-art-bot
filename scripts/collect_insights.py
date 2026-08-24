#!/usr/bin/env python3
"""Run the isolated, read-only Instagram Insights collector once."""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.insights_collector import InsightsCollector
from src.insights_storage import InsightsStorage, InsightsStorageError
from src.instagram_insights import InstagramInsightsClient, InstagramInsightsConfigurationError


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect due Instagram media Insights snapshots")
    parser.add_argument("--dry-run", action="store_true", help="Inspect due slots without Meta or R2 writes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        storage = InsightsStorage()
        client = None if args.dry_run else InstagramInsightsClient()
        summary = InsightsCollector(storage, client).run(dry_run=args.dry_run)
    except (InsightsStorageError, InstagramInsightsConfigurationError, ValueError) as exc:
        logging.getLogger(__name__).error("Insights collector could not start: %s", exc)
        return 1

    logging.getLogger(__name__).info("Insights collector summary: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
