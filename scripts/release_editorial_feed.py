"""Inspect or explicitly transfer an active Feed approval to the current main SHA."""
import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.manage_feed_schedule import GitHubEvidence
from src.feed_schedule import FeedScheduleError, FeedScheduleManager


def run(argv=None, *, manager=None, github=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-generation', type=int)
    parser.add_argument('--evidence-ref')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{40}', args.expected_sha):
        parser.error('An exact main SHA is required')
    if args.apply and (not args.evidence_ref or args.expected_generation is None or args.expected_generation < 1):
        parser.error('Activation requires evidence and the inspected positive generation')
    try:
        github = github or GitHubEvidence()
        if github.main_sha() != args.expected_sha:
            raise FeedScheduleError('RELEASE_MAIN_SHA_CHANGED')
        manager = manager or FeedScheduleManager()
        before = manager.status(expected_sha=args.expected_sha)
        if args.apply:
            inspection = manager.inspect()
            control = inspection.get('schedule_control') or {}
            approvals = control.get('continuous_approvals') or []
            if (control.get('paused') is not False or not approvals
                    or approvals[-1].get('revoked_at') is not None):
                raise FeedScheduleError('RELEASE_REQUIRES_ACTIVE_CONTINUOUS_APPROVAL')
            if (inspection['safety_generation'] != args.expected_generation
                    or before['generation'] != args.expected_generation):
                raise FeedScheduleError('RELEASE_GENERATION_CHANGED')
            cadence = approvals[-1]['cadence']
            manager.enable_continuous(expected_generation=args.expected_generation,
                                      approved_sha=args.expected_sha, main_sha=args.expected_sha,
                                      cadence=cadence, review_ref=args.evidence_ref)
            value = manager.status(expected_sha=args.expected_sha)
            if (value['approved_sha'] != args.expected_sha or value['cadence'] != cadence
                    or value['last_successful_feed_at'] != before['last_successful_feed_at']
                    or value['next_eligible_at'] != before['next_eligible_at']):
                raise FeedScheduleError('RELEASE_POSTCHECK_REQUIRES_OPERATOR_REVIEW')
        else:
            value = before
        print(json.dumps(value, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        detail = str(error) if isinstance(error, FeedScheduleError) else type(error).__name__
        print(f'RELEASE_CONTROL_FAILED: {detail}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
