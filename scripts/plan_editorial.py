#!/usr/bin/env python3
"""Write a new local editorial idea calendar; no acquisition, AI or publishing."""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.editorial_horizon import build_editorial_horizon, render_editorial_horizon
from src.insights_storage import parse_aware_timestamp


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--weeks', type=int, choices=(2, 4), default=2)
    parser.add_argument('--seed', default='editorial-v1')
    parser.add_argument('--now')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        now = parse_aware_timestamp(args.now) if args.now else datetime.now(timezone.utc)
        if now is None or args.history.stat().st_size > 16_000_000:
            raise ValueError('Use an aware time and a bounded history file')
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError('Use a new output directory')
        plan = build_editorial_horizon(json.loads(args.history.read_text()), now=now, weeks=args.weeks, seed=args.seed)
        page = render_editorial_horizon(plan)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.output.parent, prefix='.editorial-plan-') as temporary:
            stage = Path(temporary)
            (stage / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
            (stage / 'index.html').write_text(page, encoding='utf-8')
            if args.output.exists() or args.output.is_symlink():
                raise FileExistsError('Output directory changed during preparation')
            stage.rename(args.output)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f'Editorial planning failed ({type(error).__name__})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
