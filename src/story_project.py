"""Persistent, revisioned local stories with content-addressed page rendering."""

from __future__ import annotations

import hashlib
import fcntl
from contextlib import contextmanager
from uuid import uuid4
import json
import shutil
import tempfile
from pathlib import Path

from PIL import Image
from pydantic import Field, model_validator

from src.carousel_cover import FONT_DIRECTORY
from src.editorial_design import CoverStyle
from src.editorial_v2 import SourceArtwork, StrictModel
from src import story_design, editorial_design, carousel_cover
from src.story_design import RENDERER_VERSION, render_story_slide
from src.story_plan import StoryPlan
from src.story_quality import StoryIssue, StoryQuality, assess_story


class RevisionConflict(ValueError):
    """An editor used an obsolete project revision."""


class QualityBlocked(ValueError):
    def __init__(self, report: StoryQuality):
        self.report = report
        super().__init__(
            "Story blocked: "
            + ", ".join(i.code for i in report.issues if i.severity == "critical")
        )


class StoryProject(StrictModel):
    schema_version: str = "artfolio-project-v1"
    revision: int = Field(ge=1)
    sources: tuple[SourceArtwork, ...] = Field(min_length=1, max_length=8)
    plan: StoryPlan
    headline_history: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def registry(self):
        if self.schema_version != "artfolio-project-v1":
            raise ValueError("Unsupported project version")
        ids = [s.artwork.canonical_id for s in self.sources]
        if len(set(ids)) != len(ids) or set(ids) != set(self.plan.artwork_ids):
            raise ValueError(
                "Project source registry must match plan artwork identities"
            )
        return self


def _atomic(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".story-", delete=False
    ) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_project(directory: Path) -> StoryProject:
    directory = directory.resolve()
    file = directory / "project.json"
    if file.stat().st_size > 2_000_000:
        raise ValueError("Project exceeds byte limit")
    data = json.loads(file.read_text())
    for source in data.get("sources", []):
        path = (directory / source["image_path"]).resolve()
        if not path.is_relative_to(directory / "sources"):
            raise ValueError("Source image must remain inside the project directory")
        source["image_path"] = str(path)
    return StoryProject.model_validate(data)


def _saved(project: StoryProject, directory: Path) -> dict:
    data = project.model_dump(mode="json")
    for source in data["sources"]:
        source["image_path"] = str(
            Path(source["image_path"]).relative_to(directory.resolve())
        )
    return data


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render_project(directory: Path, project: StoryProject, *, verify_only: bool = False) -> dict:
    quality = assess_story(
        project.plan,
        list(project.sources),
        headline_history=list(project.headline_history),
    )
    if not quality.can_render:
        raise QualityBlocked(quality)
    registry = {s.artwork.canonical_id: s for s in project.sources}
    fingerprints = {
        identity: _fingerprint(Path(source.image_path))
        for identity, source in registry.items()
    }
    fonts = [
        _fingerprint(FONT_DIRECTORY / relative)
        for relative in (
            "source-serif/SourceSerif4-Regular.otf",
            "source-sans/SourceSans3-Regular.otf",
        )
    ]
    renderer_code = [
        _fingerprint(Path(module.__file__))
        for module in (story_design, editorial_design, carousel_cover)
    ]
    assets = directory / "pages"
    assets.mkdir(exist_ok=True)
    slides = []
    rendered_count = 0
    total = len(project.plan.slides)
    for position, slide in enumerate(project.plan.slides, 1):
        detail = (
            next(
                (
                    s
                    for s in project.plan.slides
                    if s.role == "detail" and s.artwork_ids == slide.artwork_ids
                ),
                None,
            )
            if slide.role == "cover"
            and project.plan.cover_style is CoverStyle.DETAIL_STUDY
            else None
        )
        identity = dict(
            renderer=RENDERER_VERSION,
            renderer_code=renderer_code,
            fonts=fonts,
            slide=slide.model_dump(mode="json"),
            source_metadata=[
                registry[i].artwork.model_dump(mode="json") for i in slide.artwork_ids
            ],
            images=[fingerprints[i] for i in slide.artwork_ids],
            position=position,
            total=total,
            public_title=project.plan.public_title if slide.role == "cover" else None,
            cover_style=project.plan.cover_style.value
            if slide.role == "cover"
            else None,
            cover_focus=dict(focus=detail.focus, basis=detail.focus_basis)
            if detail
            else None,
        )
        key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        relative = f"pages/{slide.id}-{key[:20]}.jpg"
        destination = directory / relative
        metadata_path = destination.with_suffix(".json")
        metadata = None
        if destination.exists() and metadata_path.exists():
            try:
                with Image.open(destination) as cached:
                    if cached.size == (1080, 1350) and cached.format == "JPEG":
                        cached.verify()
                        metadata = json.loads(metadata_path.read_text())
                        if not isinstance(metadata, dict):
                            metadata = None
            except (OSError, ValueError):
                metadata = None
        if metadata is None:
            if verify_only:
                raise ValueError("Story review requires a current valid render")
            temporary = destination.with_suffix(".render.jpg")
            try:
                metadata = render_story_slide(
                    slide,
                    registry,
                    project.plan.public_title,
                    project.plan.cover_style,
                    str(temporary),
                    position=position,
                    total=total,
                    cover_detail=detail,
                )
                temporary.replace(destination)
                metadata.pop("output_path", None)
                _atomic(metadata_path, json.dumps(metadata))
            except ValueError as error:
                temporary.unlink(missing_ok=True)
                failed = StoryIssue(
                    severity="critical", code="layout_capacity", slide_id=slide.id
                )
                raise QualityBlocked(
                    quality.model_copy(
                        update={
                            "can_render": False,
                            "issues": (*quality.issues, failed),
                        }
                    )
                ) from error
            rendered_count += 1
        slides.append(
            dict(
                slide_id=slide.id,
                role=slide.role,
                artwork_ids=list(slide.artwork_ids),
                path=relative,
                focus=list(slide.focus) if slide.focus else None,
                focus_basis=slide.focus_basis,
                content_hash=key,
                render_metadata=metadata,
            )
        )
    return dict(
        revision=project.revision,
        rendered_count=rendered_count,
        reused_count=total - rendered_count,
        quality=quality.model_dump(mode="json"),
        package=dict(
            schema_version="artfolio-story-package-v1",
            artwork_ids=list(project.plan.artwork_ids),
            slides=slides,
            publication_enabled=False,
            metadata=dict(
                narrative=project.plan.narrative,
                cover_variant=project.plan.cover_style.value,
                headline_kind=project.plan.headline_kind,
                theme_id=project.plan.theme_id,
                story_schema=project.plan.schema_version,
                renderer_version=RENDERER_VERSION,
            ),
        ),
    )


def _gallery(project: StoryProject, report: dict) -> str:
    from src.story_review import review_page

    return review_page(project, report)


def _persist(directory: Path, project: StoryProject, report: dict) -> None:
    # A single pointer swaps the complete revision. Rendering or snapshot-write
    # failures leave all three public documents on the previous revision.
    documents = {
        "project.json": json.dumps(
            _saved(project, directory), ensure_ascii=False, indent=2
        ),
        "report.json": json.dumps(report, ensure_ascii=False, indent=2),
        "index.html": _gallery(project, report),
    }
    snapshots = directory / ".revisions"
    snapshots.mkdir(exist_ok=True)
    snapshot = snapshots / f"{project.revision:06d}-{uuid4().hex}"
    snapshot.mkdir()
    for name, content in documents.items():
        _atomic(snapshot / name, content)
    for name in documents:
        link = directory / name
        if not link.is_symlink() and not link.exists():
            link.symlink_to(Path("current") / name)
    pointer = directory / f".current-{uuid4().hex}"
    try:
        pointer.symlink_to(snapshot.relative_to(directory), target_is_directory=True)
        pointer.replace(directory / "current")
    finally:
        pointer.unlink(missing_ok=True)


@contextmanager
def _project_lock(directory: Path):
    # Advisory OS lock covers read/revision check/render/commit across CLI and
    # editor processes, rather than protecting only one server's request queue.
    with (directory / ".story.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _create_project(
    directory: Path,
    sources: list[SourceArtwork],
    plan: StoryPlan,
    *,
    headline_history: list[str] | None = None,
) -> dict:
    directory = directory.resolve()
    quality = assess_story(plan, sources, headline_history=headline_history)
    if not quality.can_render:
        raise QualityBlocked(quality)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sources").mkdir(exist_ok=True)
    saved_sources = []
    for index, source in enumerate(sources, 1):
        path = directory / "sources" / f"{index:02d}.img"
        shutil.copyfile(source.image_path, path)
        saved_sources.append(
            SourceArtwork(artwork=source.artwork, image_path=str(path))
        )
    project = StoryProject(
        revision=1,
        sources=tuple(saved_sources),
        plan=plan,
        headline_history=tuple((headline_history or [])[-100:]),
    )
    report = _render_project(directory, project)
    _persist(directory, project, report)
    return report


def _update_project(
    directory: Path, plan_data: dict, *, expected_revision: int
) -> dict:
    directory = directory.resolve()
    project = load_project(directory)
    if type(expected_revision) is not int or project.revision != expected_revision:
        raise RevisionConflict("Project changed; reload before saving")
    plan = StoryPlan.model_validate(plan_data)
    updated = StoryProject(
        revision=project.revision + 1,
        sources=project.sources,
        plan=plan,
        headline_history=project.headline_history,
    )
    report = _render_project(directory, updated)
    _persist(directory, updated, report)
    return report


def create_project(
    directory: Path,
    sources: list[SourceArtwork],
    plan: StoryPlan,
    *,
    headline_history: list[str] | None = None,
) -> dict:
    directory = directory.resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError("Project directory must be empty")
    quality = assess_story(plan, sources, headline_history=headline_history)
    if not quality.can_render:
        raise QualityBlocked(quality)
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Build outside the destination. A failed first render leaves an empty or
    # absent destination ready for correction; only our temporary files vanish.
    with tempfile.TemporaryDirectory(
        dir=directory.parent, prefix=".story-build-"
    ) as stage:
        staged = Path(stage)
        report = _create_project(
            staged, sources, plan, headline_history=headline_history
        )
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ValueError("Project directory changed during creation")
        staged.rename(directory)
    return report


def update_project(directory: Path, plan_data: dict, *, expected_revision: int) -> dict:
    directory = directory.resolve()
    with _project_lock(directory):
        return _update_project(
            directory, plan_data, expected_revision=expected_revision
        )


def approve_story(directory: Path, *, expected_revision: int) -> dict:
    """Record explicit editorial acceptance of exact local source/page bytes."""
    from datetime import datetime, timezone
    from src.story_feed import review_snapshot

    directory = directory.resolve()
    with _project_lock(directory):
        project = load_project(directory)
        if type(expected_revision) is not int or project.revision != expected_revision:
            raise RevisionConflict("Project changed; review the current revision")
        snapshot = review_snapshot(directory, project)
        review = dict(schema_version="artfolio-story-review-v1",
                      revision=project.revision, digest=snapshot["digest"],
                      reviewed_at=datetime.now(timezone.utc).isoformat())
        _atomic(directory / ".story-review.json", json.dumps(review, indent=2))
        return review
