#!/usr/bin/env python3
"""Prepare 3–5 local Feed packages without reservation, staging or publication."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import main  # noqa: E402
from src.feed_queue import PreparedFeedQueue  # noqa: E402
from src.feed_schedule import FeedScheduleManager  # noqa: E402
from src.publication_state import PublicationStateStore  # noqa: E402
from src.r2_feed_queue import R2PreparedFeedQueue  # noqa: E402
from src.local_credentials import ENGAGEMENT_AUDIT_PROFILE, credential_variables, load_keychain_credentials  # noqa: E402
from src.rights_policy import RIGHTS_POLICY_ENV, RightsPolicyMode  # noqa: E402


def _refill_decision(remote: R2PreparedFeedQueue, expected_sha: str, *, early: bool = False) -> dict:
    decision = remote.stock_status() if early else remote.refill_status()
    if not decision['refill_needed']:
        return decision
    store = PublicationStateStore(config=remote.config, client=remote.client)
    schedule = FeedScheduleManager(store).status(expected_sha=expected_sha)
    status = schedule.get('status')
    if (status not in {'READY', 'WAITING_COOLDOWN', 'WAITING_FAILURE_BACKOFF', 'WAITING_WINDOW'}
            or schedule.get('approved_sha') != expected_sha):
        return {'refill_needed': False, 'reason': f'SCHEDULE_{status or "UNKNOWN"}'}
    if schedule.get('next_format') not in {'single', 'carousel'}:
        raise RuntimeError('Refill has no verified next Feed format')
    if early and decision.get('first_retained_format') not in {None, schedule['next_format']}:
        return {'refill_needed': False, 'reason': 'SCHEDULE_RETAINED_FORMAT_MISMATCH'}
    return {**decision, 'next_format': decision.get('next_format') or schedule['next_format']}


def _prepare(format_name: str, directory: Path, excluded_ids: set[str], *, theme_definition=None,
             excluded_theme_ids=None, inventory_context=()):
    # This CLI owns its process. Isolate the existing acquisition paths as well
    # as rendered outputs so preparing another package cannot overwrite one.
    original_data, original_raw = config.DATA_DIR, config.OUTPUT_RAW_IMAGE_PATH
    try:
        config.DATA_DIR = str(directory)
        config.OUTPUT_RAW_IMAGE_PATH = str(directory / "source-artwork.jpg")
        args = SimpleNamespace(dry_run=True, prepare_only=True, preparation_directory=directory,
                               image_url=None, pinterest=False, excluded_theme_ids=excluded_theme_ids or set(),
                               inventory_context=inventory_context)
        prepare = main.prepare_single_content if format_name == "single" else main.prepare_carousel_content
        return prepare(args, excluded_ids=excluded_ids,
                       **({"theme_definition": theme_definition} if theme_definition is not None else {}))
    finally:
        config.DATA_DIR, config.OUTPUT_RAW_IMAGE_PATH = original_data, original_raw


def _carousel_slots(target: int, first_format: str) -> list[int]:
    return [index for index in range(target)
            if (first_format if index % 2 == 0 else ("single" if first_format == "carousel" else "carousel")) == "carousel"]


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--target", type=int, choices=(3, 4, 5), default=3)
    parser.add_argument("--first-format", choices=("auto", "carousel", "single"), default="auto")
    parser.add_argument("--ttl-hours", type=int, default=336)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true", help="Read the manifest only")
    modes.add_argument("--check-refill", action="store_true", help="Read-only R2 queue and exact-SHA schedule gate")
    modes.add_argument("--refill", action="store_true", help="Prepare only an empty, exhausted or expired R2 batch")
    parser.add_argument("--early", action="store_true", help="Keep fresh READY entries and add three packages when at most two remain")
    parser.add_argument("--expected-sha", default=os.environ.get('GITHUB_SHA'))
    parser.add_argument("--github-output", action="store_true", help="Emit the read-only refill gate to GITHUB_OUTPUT")
    parser.add_argument("--r2", action="store_true", help="Explicitly install the prepared batch into private state R2")
    parser.add_argument("--skip-keychain", action="store_true", help="Use existing environment credentials instead")
    parser.add_argument("--story-project", action="append", type=Path, default=[],
                        help="Reviewed story project to use for the next carousel slot (repeatable)")
    args = parser.parse_args(argv)
    if args.story_project and (args.status or args.check_refill or args.refill):
        parser.error("--story-project cannot be used with status or refill modes")
    conditional = args.refill or args.check_refill
    if args.early and (not conditional or args.target != 3):
        parser.error("--early requires refill/check-refill mode with target3")
    if conditional and (not args.r2 or not args.skip_keychain or args.first_format != 'auto'
                        or not re.fullmatch(r'[0-9a-f]{40}', args.expected_sha or '')):
        parser.error('Refill requires --r2 --skip-keychain, auto format and an exact --expected-sha')
    if args.github_output and not args.check_refill:
        parser.error('--github-output requires --check-refill')
    queue = PreparedFeedQueue(args.directory)
    original_policy = os.environ.get(RIGHTS_POLICY_ENV)
    try:
        if args.r2 and not args.status and not args.skip_keychain:
            raise ValueError("R2 installation requires explicitly supplied state-writer credentials; use --skip-keychain")
        remote = R2PreparedFeedQueue(args.directory / '.remote-read') if args.r2 else None
        decision = _refill_decision(remote, args.expected_sha, early=args.early) if conditional else None
        if decision is not None and (args.check_refill or not decision['refill_needed']):
            if args.github_output:
                output = os.environ.get('GITHUB_OUTPUT')
                if not output:
                    raise ValueError('GITHUB_OUTPUT is missing')
                with Path(output).open('a', encoding='utf-8') as handle:
                    handle.write(f"refill_needed={str(decision['refill_needed']).lower()}\n")
            print(json.dumps(decision, indent=2))
            return 0
        if not args.status:
            if not args.skip_keychain:
                status = load_keychain_credentials(ENGAGEMENT_AUDIT_PROFILE)
                if any(not status[name] for name in credential_variables(ENGAGEMENT_AUDIT_PROFILE)):
                    raise ValueError("Read-only engagement-audit credentials are incomplete")
            os.environ[RIGHTS_POLICY_ENV] = RightsPolicyMode.STRICT_PUBLIC_DOMAIN.value
            first_format = (decision['next_format'] if decision is not None else
                            main._resolve_production_mode(SimpleNamespace(mode="auto")).value
                            if args.first_format == "auto" else args.first_format)
            slots = _carousel_slots(args.target, first_format)
            if len(args.story_project) > len(slots):
                raise ValueError("More story projects were supplied than available carousel slots")
            story_contents = []
            story_ids = set()
            if args.story_project:
                from src.story_feed import prepare_story_content
                story_contents = [prepare_story_content(path) for path in args.story_project]
                for content in story_contents:
                    overlap = story_ids.intersection(content.publication_ids)
                    if overlap:
                        raise ValueError("Story projects repeat an artwork")
                    story_ids.update(content.publication_ids)
            from src.feed_editorial import FeedPairPlanner
            from src.feed_variety import context_from_inventory, variety_enabled
            inventory = ()
            if args.early and remote is not None and variety_enabled():
                inventory = context_from_inventory(remote._read_manifest()[0]['packages'], now=datetime.now(timezone.utc))
            planner = FeedPairPlanner(inventory_context=inventory)
            carousel_position = 0

            def prepare_slot(format_name, directory, excluded):
                nonlocal carousel_position
                if format_name == "carousel":
                    slot = carousel_position
                    carousel_position += 1
                    if slot < len(story_contents):
                        from src.story_feed import prepare_story_content
                        story = prepare_story_content(args.story_project[slot])
                        if set(story.publication_ids).intersection(excluded):
                            raise ValueError("Story repeats an artwork already used in this queue")
                        planner.remember(story)
                        return story
                return planner.prepare(format_name, directory, excluded | story_ids | set((decision or {}).get("excluded_artwork_ids", [])), prepare=_prepare)

            queue.build(target=args.target, first_format=first_format,
                        prepare=prepare_slot, ttl_hours=args.ttl_hours)
        if args.r2:
            if not args.status:
                if args.refill:
                    latest = _refill_decision(remote, args.expected_sha, early=args.early)
                    if (not latest['refill_needed']
                            or latest.get('next_format') != decision['next_format']
                            or args.early and latest.get('queue_etag') != decision.get('queue_etag')):
                        raise RuntimeError('Refill authorization or queue changed during preparation')
                if args.early:
                    remote.replenish(queue, expected_etag=decision["queue_etag"])
                else:
                    remote.install(queue)
            result = remote.status()
        else:
            result = queue.status()
        print(json.dumps(result, indent=2))
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
