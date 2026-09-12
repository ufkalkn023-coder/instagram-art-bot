#!/usr/bin/env python3
"""Publish exactly one verified Artfolio Reel release package to Instagram."""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.instagram_poster import InstagramAPIError  # noqa: E402
from src.reel_publication import (  # noqa: E402
    ReelPublicationPersistenceError,
    publish_verified_reel,
)
from src.reel_release import ReleaseIntakeError  # noqa: E402

logger = logging.getLogger(__name__)

_MESSAGE_SAFE_ERRORS = (
    ReleaseIntakeError,
    ReelPublicationPersistenceError,
    InstagramAPIError,
)


def _log_failure(error: Exception) -> None:
    if isinstance(error, _MESSAGE_SAFE_ERRORS):
        logger.error("Reel publication failed: %s: %s", type(error).__name__, error)
    else:
        logger.error("Reel publication failed: %s", type(error).__name__)


def _default_reels_root() -> Path:
    configured = os.environ.get("ARTFOLIO_REELS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT.parent / "Remotion İnstagram Reels" / "artfolio-reels"


def _resolve_credentials() -> tuple[str, str] | None:
    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not account_id or not access_token:
        return None
    return account_id, access_token


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish exactly one deeply verified Artfolio Reel release package "
            "to Instagram Reels"
        )
    )
    parser.add_argument(
        "release",
        help="Artfolio Reels release ID or release directory to publish",
    )
    parser.add_argument(
        "--artfolio-reels-root",
        type=Path,
        default=None,
        help=(
            "Path to the Artfolio Reels repository "
            "(default: ARTFOLIO_REELS_ROOT or the sibling-project default)"
        ),
    )
    args = parser.parse_args(argv)

    credentials = _resolve_credentials()
    if credentials is None:
        logger.error(
            "Reel publication aborted: INSTAGRAM_ACCOUNT_ID and "
            "INSTAGRAM_ACCESS_TOKEN must both be set"
        )
        return 1

    reels_root = (
        args.artfolio_reels_root
        if args.artfolio_reels_root is not None
        else _default_reels_root()
    )
    try:
        publication = publish_verified_reel(
            release=args.release,
            reels_repository=reels_root,
            account_id=credentials[0],
            access_token=credentials[1],
        )
    except Exception as error:
        _log_failure(error)
        return 1
    logger.info(
        "Reel published publication_id=%s media_id=%s",
        publication.id,
        publication.media_id,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
