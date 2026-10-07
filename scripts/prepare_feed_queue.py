#!/usr/bin/env python3
"""Prepare 3–5 local Feed packages without reservation, staging or publication."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import main  # noqa: E402
from src.feed_queue import PreparedFeedQueue  # noqa: E402
from src.local_credentials import ENGAGEMENT_AUDIT_PROFILE, credential_variables, load_keychain_credentials  # noqa: E402
from src.rights_policy import RIGHTS_POLICY_ENV, RightsPolicyMode  # noqa: E402


def _prepare(format_name: str, directory: Path, excluded_ids: set[str]):
    # This CLI owns its process. Isolate the existing acquisition paths as well
    # as rendered outputs so preparing another package cannot overwrite one.
    original_data, original_raw = config.DATA_DIR, config.OUTPUT_RAW_IMAGE_PATH
    try:
        config.DATA_DIR = str(directory)
        config.OUTPUT_RAW_IMAGE_PATH = str(directory / "source-artwork.jpg")
        args = SimpleNamespace(dry_run=True, prepare_only=True, preparation_directory=directory,
                               image_url=None, pinterest=False)
        prepare = main.prepare_single_content if format_name == "single" else main.prepare_carousel_content
        return prepare(args, excluded_ids=excluded_ids)
    finally:
        config.DATA_DIR, config.OUTPUT_RAW_IMAGE_PATH = original_data, original_raw


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--target", type=int, choices=(3, 4, 5), default=3)
    parser.add_argument("--first-format", choices=("auto", "carousel", "single"), default="auto")
    parser.add_argument("--ttl-hours", type=int, default=336)
    parser.add_argument("--status", action="store_true", help="Read the local manifest only")
    parser.add_argument("--skip-keychain", action="store_true", help="Use existing environment credentials instead")
    args = parser.parse_args(argv)
    queue = PreparedFeedQueue(args.directory)
    original_policy = os.environ.get(RIGHTS_POLICY_ENV)
    try:
        if not args.status:
            if not args.skip_keychain:
                status = load_keychain_credentials(ENGAGEMENT_AUDIT_PROFILE)
                if any(not status[name] for name in credential_variables(ENGAGEMENT_AUDIT_PROFILE)):
                    raise ValueError("Read-only engagement-audit credentials are incomplete")
            os.environ[RIGHTS_POLICY_ENV] = RightsPolicyMode.STRICT_PUBLIC_DOMAIN.value
            first_format = (main._resolve_production_mode(SimpleNamespace(mode="auto")).value
                            if args.first_format == "auto" else args.first_format)
            queue.build(target=args.target, first_format=first_format,
                        prepare=_prepare, ttl_hours=args.ttl_hours)
        print(json.dumps(queue.status(), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Feed queue preparation failed ({type(error).__name__}); existing artifacts are preserved\n")
    finally:
        if original_policy is None:
            os.environ.pop(RIGHTS_POLICY_ENV, None)
        else:
            os.environ[RIGHTS_POLICY_ENV] = original_policy
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
