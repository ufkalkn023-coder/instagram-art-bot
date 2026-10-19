"""Create a five-artwork local pilot from retained sources and explicit briefs."""
import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preview_editorial_story import _json
from scripts.preview_editorial_v2 import load_sources
from src.editorial_pilot import build_editorial_pilot
from src.insights_storage import parse_aware_timestamp


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--series', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--history', type=Path, help='Optional retained publication-history snapshot')
    parser.add_argument('--now', help='Aware timestamp for reproducible provisional slots')
    args = parser.parse_args(argv)
    try:
        manifest = _json(args.manifest, 2_000_000)
        if not isinstance(manifest, dict) or not isinstance(manifest.get('artworks'), list):
            raise ValueError('Manifest requires an artworks list')
        sources = load_sources(args.manifest, count=len(manifest['artworks']))
        build_editorial_pilot(args.output, sources, _json(args.series, 128_000),
                              history=_json(args.history, 10_000_000) if args.history else None,
                              now=parse_aware_timestamp(args.now) if args.now else None)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'Pilot rejected: {error}', file=sys.stderr)
        return 1
    print(f'Local pilot: {args.output / "index.html"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
