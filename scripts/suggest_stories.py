#!/usr/bin/env python3
"""Render new unapproved local story drafts from an existing rights-cleared manifest."""
import argparse
import json
import sys
import tempfile
from html import escape
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preview_editorial_v2 import load_sources
from src.story_plan import StoryPlan
from src.story_project import create_project
from src.story_suggestions import suggest_stories


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--maximum', type=int, choices=range(1, 7), default=3)
    parser.add_argument('--history', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError('Use a new review directory')
        if args.manifest.stat().st_size > 2_000_000:
            raise ValueError('Manifest exceeds byte limit')
        manifest = json.loads(args.manifest.read_text())
        if not isinstance(manifest, dict) or not isinstance(manifest.get('artworks'), list):
            raise ValueError('Manifest must contain an artwork array')
        count = len(manifest['artworks'])
        sources = load_sources(args.manifest, count=count)
        history = []
        if args.history:
            if args.history.stat().st_size > 100_000:
                raise ValueError('Headline history exceeds byte limit')
            history = json.loads(args.history.read_text())
            if not isinstance(history, list):
                raise ValueError('Headline history must be an array')
        proposals = suggest_stories(sources, maximum=args.maximum, headline_history=history)
        if not proposals:
            raise ValueError('No proposal passes local quality and headline checks')
        registry = {s.artwork.canonical_id: s for s in sources}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.output.parent, prefix='.story-suggestions-') as temporary:
            stage = Path(temporary)
            links = []
            for proposal in proposals:
                plan = StoryPlan.model_validate(proposal['plan'])
                create_project(stage / proposal['id'], [registry[i] for i in plan.artwork_ids],
                               plan, headline_history=history)
                links.append(f'<li><a href="{proposal["id"]}/index.html">{escape(plan.public_title)}</a>'
                             f' · {escape(plan.narrative)} · {escape(proposal["basis"])}</li>')
            report = {'schema_version': 'artfolio-story-suggestions-v1', 'publication_ready': False,
                      'manual_review_required': True, 'suggestions': proposals}
            (stage / 'suggestions.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            (stage / 'index.html').write_text(
                '<!doctype html><html lang="tr"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hikâye önerileri</title>'
                '<style>body{font:17px/1.7 system-ui;max-width:900px;margin:3rem auto;padding:1rem;'
                'background:#f9f6ee;color:#24271f}li{margin:1rem 0}a{color:#294735}</style>'
                '<h1>İncelenecek hikâye önerileri</h1><p>Bunlar alternatif taslaklardır; aynı eser '
                'farklı önerilerde bulunabilir. Görsel detaylar ve anlatı insan incelemesi gerektirir. '
                'Editoryal onay, kuyruk kaydı veya yayın izni verilmemiştir.</p>'
                f'<ul>{"".join(links)}</ul></html>', encoding='utf-8')
            if args.output.exists() or args.output.is_symlink():
                raise FileExistsError('Review directory changed during preparation')
            stage.rename(args.output)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Story suggestions failed ({type(error).__name__})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
