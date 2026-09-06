#!/usr/bin/env python3
"""Run the isolated, read-only Instagram Insights collector once."""

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.insights_collector import InsightsCollector, format_summary, manual_association  # noqa: E402
from src.insights_storage import InsightsStorage, InsightsStorageError  # noqa: E402
from src.instagram_insights import (  # noqa: E402
    InstagramInsightsClient,
    InstagramInsightsConfigurationError,
    InstagramInsightsError,
)
from src.local_credentials import (  # noqa: E402
    COLLECTOR_PROFILE,
    format_credential_status,
    load_keychain_credentials,
)
from src.reel_analytics import load_local_reels  # noqa: E402


def _default_reels_root() -> Path:
    configured = os.environ.get("ARTFOLIO_REELS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT.parent / "Remotion İnstagram Reels" / "artfolio-reels"


def _configure_logging(log_file: Path | None) -> None:
    if log_file is None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        return
    destination = log_file.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(destination, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect due Instagram media Insights snapshots")
    parser.add_argument("--reels-root", type=Path, default=_default_reels_root(), help="Path to the Artfolio Reels repository")
    parser.add_argument("--dry-run", action="store_true", help="Inspect mapped due slots without Meta calls or R2 writes")
    parser.add_argument("--check-secrets", action="store_true", help="Report required credentials as SET/MISSING without revealing values")
    parser.add_argument("--log-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--legacy-publications", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--link", nargs=2, metavar=("LOCAL_REEL_ID", "INSTAGRAM_MEDIA_ID"), help="Persist a manual media association")
    args = parser.parse_args()
    _configure_logging(args.log_file)
    credential_status = load_keychain_credentials(COLLECTOR_PROFILE)
    if args.check_secrets:
        print(format_credential_status(COLLECTOR_PROFILE, credential_status))
        return 0

    try:
        storage = InsightsStorage()
        local_reels = load_local_reels(args.reels_root) if args.reels_root.exists() else None
        client = None if args.dry_run and not args.link else InstagramInsightsClient()
        if args.link:
            if local_reels is None:
                raise ValueError("A valid Artfolio Reels repository is required for manual linking")
            association, changed = manual_association(storage, client, local_reels, args.link[0], args.link[1])
            logging.getLogger(__name__).info(
                "[insights] manual_link=%s reel_id=%s media_id=%s",
                "written" if changed else "unchanged",
                association["reel_id"],
                association["instagram_media_id"],
            )
            return 0
        summary = InsightsCollector(storage, client).run(
            dry_run=args.dry_run,
            local_reels=local_reels,
            association_mode=not args.legacy_publications,
        )
    except (InsightsStorageError, InstagramInsightsError, InstagramInsightsConfigurationError, ValueError) as exc:
        logging.getLogger(__name__).error("Insights collector could not start: %s", exc)
        return 1

    rendered_summary = format_summary(summary)
    print(rendered_summary)
    if args.log_file is not None:
        logging.getLogger(__name__).info("\n%s", rendered_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
