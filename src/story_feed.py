"""Review-bound story conversion and portable export; no external service calls."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from src.feed_content import PreparedStoryContent, artwork_digest
from src.feed_queue import ARTWORK_FIELDS
from src.instagram_image import inspect_instagram_image_publishability
from src.story_delivery import StoryDelivery, StoryPage
from src.story_project import (
    QualityBlocked,
    _project_lock,
    _render_project,
    load_project,
)
from src.story_quality import assess_story


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_snapshot(directory: Path, project) -> dict:
    quality = assess_story(
        project.plan,
        list(project.sources),
        headline_history=list(project.headline_history),
    )
    if not quality.can_render:
        raise QualityBlocked(quality)
    snapshot = (directory / "current").resolve()
    if not snapshot.is_relative_to(directory / ".revisions"):
        raise ValueError("Story review snapshot escaped project")
    report_path = snapshot / "report.json"
    if report_path.stat().st_size > 2_000_000:
        raise ValueError("Story review report exceeds byte bound")
    report = json.loads(report_path.read_text())
    package = report.get("package", {})
    slides = package.get("slides", [])
    if (
        report.get("revision") != project.revision
        or package.get("artwork_ids") != list(project.plan.artwork_ids)
        or len(slides) != len(project.plan.slides)
    ):
        raise ValueError("Story review render does not match project")
    verified = _render_project(directory, project, verify_only=True)
    if verified["package"]["slides"] != slides:
        raise ValueError("Story review render no longer matches the current plan")
    paths, pages = [], []
    for slide, rendered in zip(project.plan.slides, slides):
        if (
            rendered.get("slide_id") != slide.id
            or rendered.get("role") != slide.role
            or rendered.get("artwork_ids") != list(slide.artwork_ids)
            or rendered.get("focus") != (list(slide.focus) if slide.focus else None)
            or rendered.get("focus_basis") != slide.focus_basis
        ):
            raise ValueError("Story review pages do not match the plan")
        path = (directory / rendered["path"]).resolve()
        if (
            not path.is_relative_to(directory / "pages")
            or not path.is_file()
            or path.stat().st_size > 8_000_000
        ):
            raise ValueError("Story review asset escaped project or exceeds bound")
        if not inspect_instagram_image_publishability(str(path)).publishable:
            raise ValueError("Story review page is not Instagram compatible")
        paths.append(str(path))
        pages.append(
            dict(
                id=slide.id,
                role=slide.role,
                artwork_ids=slide.artwork_ids,
                sha256=_hash(path),
            )
        )
    sources = {
        s.artwork.canonical_id: _hash(Path(s.image_path)) for s in project.sources
    }
    identity = dict(
        project=project.model_dump(mode="json"),
        report=report,
        sources=sources,
        pages=pages,
    )
    # Absolute project location is transport, not reviewed editorial content.
    for source in identity["project"]["sources"]:
        source["image_path"] = (
            Path(source["image_path"]).relative_to(directory).as_posix()
        )
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return dict(digest=digest, paths=paths, pages=pages, sources=sources)


def _artwork(source) -> dict:
    art = source.artwork
    values = art.model_dump(mode="json")
    values.update(
        id=art.canonical_id,
        artist=art.artist_display_name or art.artist_name,
        date=art.creation_date_display or art.creation_date,
        museum=art.museum_name,
        period=art.style_or_period,
    )
    return {key: value for key, value in values.items() if key in ARTWORK_FIELDS}


def prepare_story_content(directory: Path) -> PreparedStoryContent:
    directory = Path(directory).resolve()
    with _project_lock(directory):
        project = load_project(directory)
        review_path = directory / ".story-review.json"
        if (
            not review_path.is_file()
            or review_path.is_symlink()
            or review_path.stat().st_size > 10_000
        ):
            raise ValueError("Story requires current editorial review approval")
        review = json.loads(review_path.read_text())
        snapshot = review_snapshot(directory, project)
        if (
            review.get("schema_version") != "artfolio-story-review-v1"
            or review.get("revision") != project.revision
            or review.get("digest") != snapshot["digest"]
        ):
            raise ValueError("Story review is stale; review current content again")
        registry = {s.artwork.canonical_id: s for s in project.sources}
        artworks = tuple(_artwork(registry[i]) for i in project.plan.artwork_ids)
        credits = []
        for art in artworks:
            pieces = [
                f"{art['title']} — {art['artist']}",
                art.get("date"),
                art["museum"],
                art.get("credit_line"),
                art.get("license"),
                art.get("artwork_url"),
            ]
            credits.append(" · ".join(str(p) for p in pieces if p))
        caption = "\n\n".join(
            p
            for p in (
                project.plan.public_title,
                project.plan.editorial_angle,
                "\n".join(credits),
            )
            if p
        )
        delivery = StoryDelivery(
            reviewed_revision=project.revision,
            reviewed_digest=snapshot["digest"],
            source_ids=project.plan.artwork_ids,
            source_sha256=snapshot["sources"],
            artwork_sha256={a["id"]: artwork_digest(a) for a in artworks},
            caption_sha256=hashlib.sha256(caption.encode()).hexdigest(),
            public_title=project.plan.public_title,
            narrative=project.plan.narrative,
            theme_id=project.plan.theme_id,
            theme_title=project.plan.theme_title,
            cover_variant=json.loads((directory / "report.json").read_text())[
                "package"
            ]["slides"][0]["render_metadata"]["actual_style"],
            headline_kind=project.plan.headline_kind,
            pages=tuple(StoryPage(**p) for p in snapshot["pages"]),
        )
        metadata = dict(story_delivery=delivery.model_dump(mode="json"))
        return PreparedStoryContent(
            "carousel",
            artworks,
            tuple(snapshot["paths"]),
            caption,
            publication_metadata=metadata,
            theme_id=project.plan.theme_id,
            story_delivery=delivery,
        )


def export_story_content(directory: Path, output: Path) -> dict:
    content = prepare_story_content(directory)
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Story export destination already exists")
    if output.is_relative_to(Path(directory).resolve()):
        raise ValueError("Export must be outside the source project")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=output.parent, prefix=".story-export-"
    ) as stage:
        stage = Path(stage)
        assets = []
        for index, (source, page) in enumerate(
            zip(content.media_paths, content.story_delivery.pages)
        ):
            target = stage / f"media-{index}.jpg"
            shutil.copyfile(source, target)
            if _hash(target) != page.sha256:
                raise ValueError("Story changed after review during export")
            assets.append(dict(path=target.name, sha256=page.sha256))
        manifest = dict(
            content_kind="story",
            publication_format="carousel",
            artworks=content.artworks,
            assets=assets,
            caption=content.caption,
            publication_metadata=content.publication_metadata,
            story_delivery=content.story_delivery.model_dump(mode="json"),
        )
        (stage / "content.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        if output.exists():
            raise FileExistsError("Story export destination changed")
        stage.rename(output)
    return manifest


def validate_story_media(content: PreparedStoryContent) -> None:
    # Recheck after claim, immediately before reserving production identity.
    replace(content)
    for path, page in zip(content.media_paths, content.story_delivery.pages):
        file = Path(path)
        if (
            not file.is_file()
            or file.stat().st_size > 8_000_000
            or _hash(file) != page.sha256
        ):
            raise ValueError("Story media changed after review")


@contextmanager
def snapshot_story_media(content: PreparedStoryContent):
    """Bind staging to reviewed bytes, independent of mutable editor/queue paths."""
    replace(content)
    with tempfile.TemporaryDirectory(prefix="artfolio-reviewed-media-") as directory:
        paths = []
        for index, (source, page) in enumerate(
            zip(content.media_paths, content.story_delivery.pages)
        ):
            file = Path(source)
            with file.open("rb") as stream:
                data = stream.read(8_000_001)
            if len(data) > 8_000_000 or hashlib.sha256(data).hexdigest() != page.sha256:
                raise ValueError("Story media changed after review")
            target = Path(directory) / f"page-{index}.jpg"
            target.write_bytes(data)
            paths.append(str(target))
        yield replace(content, media_paths=tuple(paths))
