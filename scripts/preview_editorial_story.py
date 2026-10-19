"""Render an explicit local editorial brief without AI or publication calls."""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preview_editorial_v2 import load_sources
from src.editorial_draft import create_editorial_gallery


def _json(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('Editorial input exceeds byte limit')
    return json.loads(path.read_text(encoding='utf-8'))


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--brief', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--history', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = _json(args.manifest, 2_000_000)
        if not isinstance(manifest, dict) or not isinstance(manifest.get('artworks'), list):
            raise ValueError('Manifest requires an artworks list')
        if any(not isinstance(entry, dict) or not isinstance(entry.get('image_path'), str)
               for entry in manifest['artworks']):
            raise ValueError('Each source requires a local image path')
        sources = load_sources(args.manifest, count=len(manifest['artworks']))
        history = _json(args.history, 100_000) if args.history else ()
        create_editorial_gallery(args.output, sources, _json(args.brief, 128_000), headline_history=history)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'Editorial preview rejected: {error}', file=sys.stderr)
        return 1
    print(f'Local editorial review: {args.output / "index.html"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
