"""Assign retained editorial projects to provisional alternating Feed slots."""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preview_editorial_story import _json
from src.editorial_rhythm import plan_editorial_projects, render_editorial_rhythm
from src.insights_storage import parse_aware_timestamp


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projects', nargs='+', type=Path, required=True)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--weeks', type=int, choices=(2, 4), default=2)
    parser.add_argument('--now')
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError('Output must be a new directory')
        now = parse_aware_timestamp(args.now) if args.now else datetime.now(timezone.utc)
        report = plan_editorial_projects(args.projects, _json(args.history, 10_000_000), now=now, weeks=args.weeks)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.output.parent, prefix='.rhythm-') as temporary:
            stage = Path(temporary)
            (stage / 'rhythm.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            (stage / 'index.html').write_text(render_editorial_rhythm(report), encoding='utf-8')
            if args.output.exists() or args.output.is_symlink():
                raise FileExistsError('Output changed during planning')
            stage.rename(args.output)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'Rhythm rejected: {error}', file=sys.stderr)
        return 1
    print(f'Provisional rhythm: {args.output / "index.html"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
