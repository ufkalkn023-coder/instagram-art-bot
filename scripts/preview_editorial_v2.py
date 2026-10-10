"""Build a local Artfolio editorial review gallery; no publication integration."""

from __future__ import annotations

import argparse
import html
import json
import random
import sys
from pathlib import Path
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from src.editorial_design import CoverStyle, _validate_focus, render_editorial_cover
from src.editorial_v2 import (
    GeminiEditorialProvider,
    EditorialProviderUnavailable,
    SourceArtwork,
    select_plan,
)
from src.museums.cleveland import ClevelandAdapter
from src.quality_filter import validate_and_download_image_with_metadata
from src.rights_policy import RightsPolicyMode, is_rights_eligible


def _empty_directory(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(
            "Output directory must be empty; existing user data is preserved"
        )
    path.mkdir(parents=True, exist_ok=True)


def _check_image(path: Path) -> None:
    if path.stat().st_size > 30_000_000:
        raise ValueError("Image exceeds byte limit")
    with Image.open(path) as image:
        if image.width * image.height > 40_000_000 or min(image.size) < 300:
            raise ValueError("Image dimensions outside preview limits")
        image.verify()


def load_sources(manifest: Path, *, count: int) -> list[SourceArtwork]:
    if not 1 <= count <= 50:
        raise ValueError("count must be between 1 and 50")
    manifest = manifest.resolve()
    if manifest.stat().st_size > 2_000_000:
        raise ValueError("Manifest exceeds byte limit")
    data = json.loads(manifest.read_text())
    entries = data.get("artworks", [])
    if not isinstance(entries, list) or not count <= len(entries) <= 50:
        raise ValueError(
            "Manifest must contain the requested number of artworks, at most 50"
        )
    sources = []
    seen = set()
    for entry in entries[:count]:
        path = (manifest.parent / entry["image_path"]).resolve()
        if not path.is_relative_to(manifest.parent):
            raise ValueError("Image path must remain within the manifest directory")
        source = SourceArtwork.model_validate({**entry, "image_path": str(path)})
        if source.artwork.canonical_id in seen:
            raise ValueError("Duplicate artwork identity in manifest")
        seen.add(source.artwork.canonical_id)
        _check_image(path)
        sources.append(source)
    return sources


def acquire_sources(directory: Path, *, count: int, seed: int) -> list[SourceArtwork]:
    if not 1 <= count <= 50:
        raise ValueError("count must be between 1 and 50")
    _empty_directory(directory)
    pool = ClevelandAdapter().fetch_candidates(limit=150, rng=random.Random(seed))
    sources = []
    seen = set()
    attempts = 0
    rejected = []
    for artwork in pool:
        if artwork.canonical_id in seen or not is_rights_eligible(
            artwork, RightsPolicyMode.STRICT_PUBLIC_DOMAIN
        ):
            continue
        seen.add(artwork.canonical_id)
        if attempts >= 90:
            break
        attempts += 1
        path = directory / f"work-{attempts:03d}.jpg"
        result = validate_and_download_image_with_metadata(artwork.image_url, str(path))
        if not result.valid:
            rejected.append(dict(artwork_id=artwork.canonical_id, reason=result.reason))
            continue
        try:
            _check_image(path)
        except (ValueError, OSError) as error:
            rejected.append(
                dict(artwork_id=artwork.canonical_id, reason=type(error).__name__)
            )
            continue
        sources.append(SourceArtwork(artwork=artwork, image_path=str(path.resolve())))
        print(f"Downloaded {len(sources)}/{count}: {artwork.title}", flush=True)
        if len(sources) == count:
            break
    manifest = {
        "artworks": [
            dict(
                artwork=s.artwork.model_dump(mode="json"),
                image_path=Path(s.image_path).name,
            )
            for s in sources
        ],
        "download_attempts": attempts,
        "rejected": rejected,
        "source": "Cleveland public API",
        "seed": seed,
    }
    (directory / "sources.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    if len(sources) != count:
        raise ValueError(
            f"Only {len(sources)} rights-confirmed valid images available; need {count}"
        )
    return sources


def _link(url: str | None, label: str) -> str:
    if url and urlsplit(url).scheme == "https" and urlsplit(url).netloc:
        return f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">{html.escape(label)}</a>'
    return html.escape(label)


def load_detail_focus(path: Path) -> dict[str, tuple[float, float, float, float]]:
    """Read explicit preview crop coordinates; never treat them as AI evidence."""
    if path.stat().st_size > 100_000:
        raise ValueError("Detail focus file exceeds byte limit")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not 1 <= len(data) <= 50:
        raise ValueError("Detail focus must map 1–50 artwork identities to rectangles")
    for identity, focus in data.items():
        if (
            not identity
            or len(identity) > 150
            or not isinstance(focus, list)
            or len(focus) != 4
            or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in focus
            )
        ):
            raise ValueError(
                "Detail focus requires an artwork identity and four numeric coordinates"
            )
    return {identity: _validate_focus(focus) for identity, focus in data.items()}


def build_gallery(
    sources: list[SourceArtwork],
    output: Path,
    *,
    provider: GeminiEditorialProvider | None = None,
    history: list[str] | None = None,
    detail_focus: dict[str, tuple[float, float, float, float]] | None = None,
) -> dict:
    if not 1 <= len(sources) <= 50:
        raise ValueError("Gallery requires 1–50 artworks")
    sources = [SourceArtwork.model_validate(source.model_dump()) for source in sources]
    if len({s.artwork.canonical_id for s in sources}) != len(sources):
        raise ValueError("Gallery requires distinct artwork identities")
    identities = {s.artwork.canonical_id for s in sources}
    preview_focus = detail_focus or {}
    if set(preview_focus) - identities:
        raise ValueError("Detail focus refers to unknown artwork identities")
    preview_focus = {
        identity: _validate_focus(focus) for identity, focus in preview_focus.items()
    }
    for source in sources:
        _check_image(Path(source.image_path))
    _empty_directory(output)
    recent = list(history or [])[-100:]
    items = []
    sections = []
    for index, source in enumerate(sources, 1):
        candidates = []
        provider_status = "ai_disabled"
        if provider is not None:
            try:
                candidates = provider.analyze(
                    [source], "single_artwork", "Single artwork study"
                )
                provider_status = "proposal_received"
            except EditorialProviderUnavailable as error:
                provider_status = str(error)
        plan = select_plan(
            [source],
            "single_artwork",
            "Single artwork study",
            candidates,
            history=recent,
        )
        recent.append(plan.public_title)
        covers = []
        for style in CoverStyle:
            focus = plan.focus
            focus_basis = "model_focus"
            if (
                style is CoverStyle.DETAIL_STUDY
                and source.artwork.canonical_id in preview_focus
            ):
                focus = preview_focus[source.artwork.canonical_id]
                focus_basis = "preview_focus"
            relative = f"covers/{index:02d}-{style.value}.jpg"
            rendered = render_editorial_cover(
                source.image_path,
                plan.public_title,
                source.artwork.museum_name,
                style,
                str(output / relative),
                focus,
                focus_basis=focus_basis,
            )
            covers.append(
                {
                    **rendered,
                    "output_path": relative,
                    "focus": list(focus)
                    if style is CoverStyle.DETAIL_STUDY and focus is not None
                    else None,
                }
            )
        items.append(
            dict(
                artwork=source.artwork.model_dump(mode="json"),
                plan=plan.model_dump(mode="json"),
                provider_status=provider_status,
                candidates=[c.model_dump(mode="json") for c in candidates],
                covers=covers,
            )
        )
        cards = "".join(
            f'<figure><img loading="lazy" src="{c["output_path"]}" alt="{html.escape(plan.public_title, quote=True)} — {c["requested_style"]}">'
            f"<figcaption>{c['requested_style'].replace('_', ' ').title()} · {c['crop_basis']}</figcaption></figure>"
            for c in covers
        )
        sections.append(
            f"<section><h2>{index:02d}. {html.escape(plan.public_title)}</h2>"
            f"<p>{html.escape(source.artwork.artist_name)} · {html.escape(source.artwork.creation_date or 'Unknown date')} · "
            f"{_link(source.artwork.artwork_url, source.artwork.museum_name)} · {html.escape(source.artwork.license or 'Confirmed rights')}</p>"
            f"<p>{html.escape(plan.status)} · {html.escape(provider_status)} · Manual review required</p>"
            f'<div class="covers">{cards}</div></section>'
        )
    report = dict(
        ai_enabled=provider is not None,
        ai_calls=provider.calls if provider else 0,
        history_count=len(history or []),
        artwork_count=len(items),
        cover_count=len(items) * 3,
        factual_fallback_count=sum(
            i["plan"]["status"] == "factual_fallback" for i in items
        ),
        publication_enabled=False,
        preview_detail_count=len(preview_focus),
        items=items,
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    page += "<title>Artfolio — Editorial cover review</title><style>body{margin:0 auto;max-width:1500px;padding:32px;background:#f2efe7;color:#252522;font:16px system-ui}h1,h2{font-family:Georgia,serif;font-weight:400}h1{font-size:44px}section{margin:50px 0}.covers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}figure{margin:0}img{width:100%;display:block}figcaption{margin-top:12px;font-size:13px}a{color:inherit}p{line-height:1.6}@media(max-width:750px){.covers{grid-template-columns:1fr}body{padding:20px}}</style>"
    page += f"<h1>Artfolio · Editorial cover review</h1><p>{len(items)} rights-confirmed museum artworks · {len(items) * 3} covers · local preview only.</p>"
    page += (
        "<p>AI disabled: original museum titles are factual fallbacks, not generated editorial headlines.</p>"
        if provider is None
        else "<p>AI proposals require manual visual and source review. Automated evidence checks do not certify historical truth.</p>"
    )
    if preview_focus:
        page += "<p>Detail Study uses explicit crops selected for local visual review, not Gemini recommendations. Crop coordinates are recorded in the report.</p>"
    page += "<p>Without supplied or model focus, Detail Study shows a labeled full-artwork fallback.</p>"
    page += (
        '<p><a href="report.json">Evidence and layout report</a></p>'
        + "".join(sections)
        + "</html>"
    )
    (output / "index.html").write_text(page, encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--acquire-cleveland", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--history", type=Path)
    parser.add_argument(
        "--detail-focus",
        type=Path,
        help="JSON mapping artwork identities to reviewed preview crop coordinates",
    )
    parser.add_argument("--use-gemini", action="store_true")
    parser.add_argument("--max-ai-calls", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.count <= 50 or not 0 <= args.max_ai_calls <= 50:
        parser.error("count must be 1–50 and max-ai-calls 0–50")
    if args.max_ai_calls and not args.use_gemini:
        parser.error("max-ai-calls requires explicit --use-gemini")
    if args.detail_focus and not args.manifest:
        parser.error(
            "--detail-focus requires --manifest; inspect existing source images before choosing crops"
        )
    detail_focus = load_detail_focus(args.detail_focus) if args.detail_focus else None
    provider = None
    if args.use_gemini:
        import os
        from src.gemini_ai import _create_client

        key = os.environ.get("GOOGLE_GEMINI_API_KEY")
        if not key or not args.max_ai_calls:
            parser.error(
                "Opt-in requires GOOGLE_GEMINI_API_KEY and a positive max-ai-calls"
            )
        provider = GeminiEditorialProvider(
            client=_create_client(key),
            cache_directory=args.output.parent / "editorial-cache",
            max_calls=args.max_ai_calls,
        )
    history = []
    if args.history:
        if args.history.stat().st_size > 1_000_000:
            parser.error("History exceeds byte limit")
        history = json.loads(args.history.read_text())
        if not isinstance(history, list) or not all(
            isinstance(t, str) and len(t) <= 500 for t in history
        ):
            parser.error("History must be a JSON array of headline strings")
    sources = (
        load_sources(args.manifest, count=args.count)
        if args.manifest
        else acquire_sources(args.acquire_cleveland, count=args.count, seed=args.seed)
    )
    report = build_gallery(
        sources,
        args.output,
        provider=provider,
        history=history[-100:],
        detail_focus=detail_focus,
    )
    print(json.dumps({k: v for k, v in report.items() if k != "items"}, indent=2))
    print(args.output.resolve() / "index.html")


if __name__ == "__main__":
    main()
