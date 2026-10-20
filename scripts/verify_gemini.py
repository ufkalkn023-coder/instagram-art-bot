"""Generate local Feed previews using the real model, without publication credentials."""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from scripts.preview_editorial_v2 import acquire_sources
from src.automatic_editorial import prepare_automatic_editorial
from src.automatic_editorial_copy import PROTOCOL, require_editorial_provider
from src.feed_content import PreparedFeedContent


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--format', choices=['single', 'both'], default='single')
    args = parser.parse_args(argv)
    require_editorial_provider()
    args.output.mkdir(parents=True, exist_ok=False)
    sources = acquire_sources(args.output / 'museum-sources', count=6 if args.format == 'both' else 1, seed=20261020)
    results = []
    for format_name in (('single', 'carousel') if args.format == 'both' else ('single',)):
        selected = sources[:1] if format_name == 'single' else sources
        artworks = tuple(dict(s.artwork.model_dump(mode='json'), id=s.artwork.canonical_id,
                              artist=s.artwork.artist_name, date=s.artwork.creation_date,
                              museum=s.artwork.museum_name, local_image_path=s.image_path) for s in selected)
        acquired = PreparedFeedContent(format_name, artworks, tuple(s.image_path for s in selected),
                                       'Verification preview; no publication', theme_id='artfolio_selection')
        content = prepare_automatic_editorial(acquired, args.output / format_name)
        (args.output / format_name / 'caption.txt').write_text(content.caption, encoding='utf-8')
        result = dict(format=format_name, model=config.GEMINI_MODEL, protocol=PROTOCOL,
                      public_title=content.story_delivery.public_title,
                      source_ids=content.publication_ids, pages=len(content.media_paths), published=False)
        results.append(result)
    report = dict(model=config.GEMINI_MODEL, inference_calls=len(results), publications=0, results=results)
    (args.output / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
