"""Local 3x3 Instagram profile preview workspace and loopback editor for the Artfolio Astra pilot."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import shutil
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.astra_catalog import get_design, list_usable_designs, load_design_catalog
from src.astra_renderer import AstraArtworkInput, render_astra_design

MAX_API_BYTES = 128_000
MAX_PINNED_POSTS = 3


class _StrictPilotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PilotArtworkRecord(_StrictPilotModel):
    """Real museum artwork metadata bound to a local image file."""

    source_key: str = Field(min_length=1, max_length=64)
    image_path: str = Field(min_length=1, max_length=4096)
    artist: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    date: str = Field(default="", max_length=80)
    institution: str = Field(default="", max_length=160)
    museum_url: str = Field(default="", max_length=512)
    focus: tuple[float, float] | tuple[float, float, float, float] | None = None


class PilotDraftItem(_StrictPilotModel):
    """One local draft post (single, carousel first slide, or Reel cover)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    post_format: Literal["single", "carousel", "reel"]
    design_id: str = Field(pattern=r"^[a-z][a-z0-9]{1,15}-[0-9]{3}$")
    editorial_title: str = Field(min_length=1, max_length=160)
    caption_en: str = Field(min_length=1, max_length=2200)
    artworks: tuple[PilotArtworkRecord, ...] = Field(min_length=1, max_length=3)
    detail_focuses: tuple[tuple[float, float] | tuple[float, float, float, float], ...] | None = None
    slide_count: int = Field(default=1, ge=1, le=10, strict=True)
    rendered_path: str = Field(default="", max_length=512)
    rendered_sha256: str = Field(default="", max_length=64)
    reel_status: Literal["cover_ready", "not_applicable"] = "not_applicable"
    published_at_label: str = Field(default="", max_length=64)


class PriorProfilePost(_StrictPilotModel):
    """Existing historical profile post rendered with a real local image."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    post_format: Literal["single", "carousel", "reel"] = "single"
    title: str = Field(min_length=1, max_length=160)
    artist: str = Field(min_length=1, max_length=160)
    date: str = Field(default="", max_length=80)
    institution: str = Field(default="", max_length=160)
    museum_url: str = Field(default="", max_length=512)
    caption_en: str = Field(default="", max_length=2200)
    image_path: str = Field(min_length=1, max_length=512)
    published_at_label: str = Field(default="", max_length=64)


class AstraPilotWorkspaceState(_StrictPilotModel):
    """Persisted local state for the 9-draft 3x3 profile preview."""

    schema_version: Literal["artfolio-astra-pilot-v1"] = "artfolio-astra-pilot-v1"
    profile_handle: str = Field(default="@artfolio.db", min_length=2, max_length=64)
    publication_ready: Literal[False] = False
    drafts: tuple[PilotDraftItem, ...] = Field(min_length=1, max_length=30)
    draft_order: tuple[str, ...] = Field(min_length=1, max_length=30)
    pinned_ids: tuple[str, ...] = Field(default=(), max_length=MAX_PINNED_POSTS)
    history_posts: tuple[PriorProfilePost, ...] = Field(default=(), max_length=30)

    @model_validator(mode="after")
    def _validate_references(self) -> "AstraPilotWorkspaceState":
        draft_ids = [item.id for item in self.drafts]
        if len(set(draft_ids)) != len(draft_ids):
            raise ValueError("Draft IDs must be unique")
        if set(self.draft_order) != set(draft_ids) or len(self.draft_order) != len(draft_ids):
            raise ValueError("draft_order must contain each draft ID exactly once")
        history_ids = [item.id for item in self.history_posts]
        if len(set(history_ids)) != len(history_ids):
            raise ValueError("History post IDs must be unique")
        if set(draft_ids).intersection(history_ids):
            raise ValueError("Draft IDs and history post IDs must be distinct")
        valid_pin_targets = set(draft_ids).union(history_ids)
        if len(set(self.pinned_ids)) != len(self.pinned_ids):
            raise ValueError("Pinned post IDs must be unique")
        if any(pid not in valid_pin_targets for pid in self.pinned_ids):
            raise ValueError("Pinned IDs must reference existing draft or history posts")
        return self


def compute_profile_grid_cells(state: AstraPilotWorkspaceState) -> list[dict]:
    """Compute Instagram 3x3 grid cells: up to 3 pinned at top (no duplicates), then newest->oldest."""
    draft_map = {item.id: item for item in state.drafts}
    history_map = {item.id: item for item in state.history_posts}

    ordered_cells: list[dict] = []
    seen: set[str] = set()

    def _pack_draft(item: PilotDraftItem, *, pinned: bool) -> dict:
        return {
            "id": item.id,
            "kind": "draft",
            "pinned": pinned,
            "post_format": item.post_format,
            "design_id": item.design_id,
            "editorial_title": item.editorial_title,
            "caption_en": item.caption_en,
            "artworks": [art.model_dump(mode="json") for art in item.artworks],
            "slide_count": item.slide_count,
            "image_path": item.rendered_path,
            "rendered_sha256": item.rendered_sha256,
            "reel_status": item.reel_status,
            "published_at_label": item.published_at_label,
        }

    def _pack_history(item: PriorProfilePost, *, pinned: bool) -> dict:
        return {
            "id": item.id,
            "kind": "history",
            "pinned": pinned,
            "post_format": item.post_format,
            "design_id": None,
            "editorial_title": item.title,
            "caption_en": item.caption_en,
            "artworks": [
                {
                    "source_key": item.id,
                    "image_path": item.image_path,
                    "artist": item.artist,
                    "title": item.title,
                    "date": item.date,
                    "institution": item.institution,
                    "museum_url": item.museum_url,
                }
            ],
            "slide_count": 1,
            "image_path": item.image_path,
            "rendered_sha256": "",
            "reel_status": "not_applicable",
            "published_at_label": item.published_at_label,
        }

    for pid in state.pinned_ids:
        if pid in seen:
            continue
        seen.add(pid)
        if pid in draft_map:
            ordered_cells.append(_pack_draft(draft_map[pid], pinned=True))
        elif pid in history_map:
            ordered_cells.append(_pack_history(history_map[pid], pinned=True))

    for did in state.draft_order:
        if did in seen:
            continue
        seen.add(did)
        ordered_cells.append(_pack_draft(draft_map[did], pinned=False))

    for hpost in state.history_posts:
        if hpost.id in seen:
            continue
        seen.add(hpost.id)
        ordered_cells.append(_pack_history(hpost, pinned=False))

    return ordered_cells


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _render_draft_item(workspace_dir: Path, item: PilotDraftItem) -> PilotDraftItem:
    """Render a draft's real 1080x1350 JPEG using its chosen Astra template."""
    entry = get_design(item.design_id)
    if not entry.is_usable:
        raise ValueError(f"Design '{item.design_id}' is not ported yet")
    if len(item.artworks) != entry.required_sources:
        raise ValueError(
            f"Design '{item.design_id}' requires {entry.required_sources} artwork(s), "
            f"but draft '{item.id}' has {len(item.artworks)}"
        )

    sources = []
    for art in item.artworks:
        art_path = Path(art.image_path)
        if not art_path.is_absolute():
            art_path = (workspace_dir / art_path).resolve()
        sources.append(
            AstraArtworkInput(
                image_path=art_path,
                title=art.title,
                artist=art.artist,
                date=art.date,
                institution=art.institution,
                museum_url=art.museum_url,
                focus=art.focus,
            )
        )

    renders_dir = workspace_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    seed_payload = json.dumps(
        {
            "id": item.id,
            "design_id": item.design_id,
            "editorial_title": item.editorial_title,
            "artworks": [a.model_dump(mode="json") for a in item.artworks],
            "detail_focuses": item.detail_focuses,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    short_hash = hashlib.sha256(seed_payload.encode("utf-8")).hexdigest()[:12]
    rel_path = f"renders/{item.id}-{item.design_id}-{short_hash}.jpg"
    result = render_astra_design(
        item.design_id,
        sources,
        item.editorial_title,
        workspace_dir / rel_path,
        detail_focuses=item.detail_focuses,
    )
    return item.model_copy(
        update={
            "rendered_path": rel_path,
            "rendered_sha256": result["sha256"],
        }
    )


def save_pilot_state(workspace_dir: Path, state: AstraPilotWorkspaceState) -> None:
    _atomic_write_text(
        workspace_dir / "state.json",
        json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    )


def load_pilot_state(workspace_dir: Path) -> AstraPilotWorkspaceState:
    state_path = Path(workspace_dir).resolve() / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise FileNotFoundError(f"Pilot workspace state not found: {state_path}")
    return AstraPilotWorkspaceState.model_validate_json(state_path.read_text(encoding="utf-8"))


def update_draft_design(
    workspace_dir: Path,
    draft_id: str,
    design_id: str,
) -> AstraPilotWorkspaceState:
    """Change a draft's Astra design, re-render its real output JPEG, and persist state."""
    workspace_dir = Path(workspace_dir).resolve()
    state = load_pilot_state(workspace_dir)
    updated_drafts: list[PilotDraftItem] = []
    found = False
    for item in state.drafts:
        if item.id == draft_id:
            found = True
            candidate = item.model_copy(update={"design_id": design_id})
            rendered = _render_draft_item(workspace_dir, candidate)
            updated_drafts.append(rendered)
        else:
            updated_drafts.append(item)
    if not found:
        raise KeyError(f"Unknown draft id: {draft_id}")
    new_state = state.model_copy(update={"drafts": tuple(updated_drafts)})
    save_pilot_state(workspace_dir, new_state)
    return new_state


def update_workspace_order(
    workspace_dir: Path,
    *,
    draft_order: list[str] | tuple[str, ...],
    pinned_ids: list[str] | tuple[str, ...],
) -> AstraPilotWorkspaceState:
    """Persist updated newest->oldest order and pinned IDs (max 3, no duplicates)."""
    workspace_dir = Path(workspace_dir).resolve()
    state = load_pilot_state(workspace_dir)
    new_state = AstraPilotWorkspaceState.model_validate(
        {
            **state.model_dump(mode="json"),
            "draft_order": list(draft_order),
            "pinned_ids": list(pinned_ids),
        }
    )
    save_pilot_state(workspace_dir, new_state)
    return new_state


def build_default_nine_draft_specs(assets_dir: Path) -> tuple[list[dict], list[dict]]:
    """Build 9 diverse drafts (non-prototype artworks, 2 distinct triples, single/carousel/reel) + 3 history posts."""
    meta_path = assets_dir / "art.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"Museum metadata file not found: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    def _art(key: str, focus=None) -> dict:
        artist, title, date, url, _acc, institution = meta[key]
        img = assets_dir / f"{key}.jpg"
        if not img.is_file():
            raise FileNotFoundError(f"Artwork file missing: {img}")
        return {
            "source_key": key,
            "image_path": str(img.resolve()),
            "artist": artist,
            "title": title,
            "date": date,
            "institution": institution,
            "museum_url": url,
            "focus": focus,
        }

    # Intentionally uses real museum artworks distinct from the prototype's defaults
    # and includes two completely different triples for astra-094 (Three Rooms).
    drafts = [
        {
            "id": "draft-01",
            "post_format": "single",
            "design_id": "astra-001",
            "editorial_title": "Fire on the river",
            "caption_en": (
                "Turner watched Parliament burn from the Thames and painted heat before architecture.\n\n"
                "J. M. W. Turner — The Burning of the Houses of Lords and Commons, 16 October 1834 (1835). "
                "Cleveland Museum of Art.\n\n"
                "Which area draws your eye first: the flame or the cold water below?"
            ),
            "artworks": [_art("turner")],
            "slide_count": 1,
            "published_at_label": "Slot 09 · En yeni",
        },
        {
            "id": "draft-02",
            "post_format": "carousel",
            "design_id": "astra-013",
            "editorial_title": "Three slices of the wave",
            "caption_en": (
                "Read Hokusai's composition in three vertical steps: the crest, the hollow, and Mount Fuji waiting behind.\n\n"
                "Katsushika Hokusai — The Great Wave off Kanagawa (c. 1830–32). The Metropolitan Museum of Art.\n\n"
                "Swipe through the close-ups to see how Prussian blue carries the rhythm."
            ),
            "artworks": [_art("wave")],
            "detail_focuses": [(0.22, 0.48), (0.48, 0.46), (0.76, 0.56)],
            "slide_count": 4,
            "published_at_label": "Slot 08",
        },
        {
            "id": "draft-03",
            "post_format": "reel",
            "design_id": "astra-083",
            "editorial_title": "Shadow before the face",
            "caption_en": (
                "Caravaggio lets darkness take three-quarters of the canvas so a single shoulder carries the whole drama.\n\n"
                "Caravaggio — The Crucifixion of Saint Andrew (1606–7). Cleveland Museum of Art.\n\n"
                "Watch the slow pan from the lantern edge into the quiet center."
            ),
            "artworks": [_art("carav", focus=(0.48, 0.40))],
            "slide_count": 1,
            "reel_status": "cover_ready",
            "published_at_label": "Slot 07 · Reels kapağı",
        },
        {
            "id": "draft-04",
            "post_format": "carousel",
            "design_id": "astra-061",
            "editorial_title": "Five tones of spring",
            "caption_en": (
                "Before Monet built his garden at Giverny, he tested how dark background greens make petals glow.\n\n"
                "Claude Monet — Spring Flowers (1864). Cleveland Museum of Art.\n\n"
                "Every swatch below is sampled directly from the painting's pigments."
            ),
            "artworks": [_art("flowers")],
            "slide_count": 3,
            "published_at_label": "Slot 06",
        },
        {
            "id": "draft-05",
            "post_format": "carousel",
            "design_id": "astra-094",
            "editorial_title": "Three coastal rooms",
            "caption_en": (
                "Three painters, three temperatures of water and shore—placed side by side in architectural windows.\n\n"
                "Eugène Boudin · Winslow Homer · Utagawa Hiroshige.\n\n"
                "Which room would you step into first?"
            ),
            "artworks": [_art("boudin"), _art("homer"), _art("hiro")],
            "slide_count": 4,
            "published_at_label": "Slot 05 · Üçlü #1",
        },
        {
            "id": "draft-06",
            "post_format": "single",
            "design_id": "astra-001",
            "editorial_title": "A quiet table in afternoon light",
            "caption_en": (
                "Morisot paints reading as stillness—soft whites, warm wood, and a book that holds the room together.\n\n"
                "Berthe Morisot — Reading (1873). Cleveland Museum of Art."
            ),
            "artworks": [_art("berthe")],
            "slide_count": 1,
            "published_at_label": "Slot 04",
        },
        {
            "id": "draft-07",
            "post_format": "reel",
            "design_id": "astra-083",
            "editorial_title": "Where does the blue stop?",
            "caption_en": (
                "Whistler reduces the Thames at dusk to three bands of mist, bridge, and reflection.\n\n"
                "James McNeill Whistler — Nocturne: Blue and Silver — Battersea Reach (1870–75). "
                "Freer Gallery of Art, Smithsonian Institution."
            ),
            "artworks": [_art("whistler", focus=(0.47, 0.48))],
            "slide_count": 1,
            "reel_status": "cover_ready",
            "published_at_label": "Slot 03 · Reels kapağı",
        },
        {
            "id": "draft-08",
            "post_format": "single",
            "design_id": "astra-061",
            "editorial_title": "Violet in the dark vase",
            "caption_en": (
                "Odilon Redon turns a modest bouquet into a floating palette of ochre, rose, and chalky violet.\n\n"
                "Odilon Redon — Vase of Flowers (1916). Cleveland Museum of Art."
            ),
            "artworks": [_art("redon")],
            "slide_count": 1,
            "published_at_label": "Slot 02",
        },
        {
            "id": "draft-09",
            "post_format": "carousel",
            "design_id": "astra-094",
            "editorial_title": "Three studies of gesture",
            "caption_en": (
                "A second trio for Three Rooms: stage rehearsal, portrait poise, and mountain geometry in dialogue.\n\n"
                "Edgar Degas · John Singer Sargent · Paul Cézanne."
            ),
            "artworks": [_art("degas"), _art("sargent"), _art("cezanne")],
            "slide_count": 4,
            "published_at_label": "Slot 01 · Üçlü #2",
        },
    ]

    # Real historical posts to illustrate prior profile cells below the new drafts.
    history = [
        {
            "id": "hist-01",
            "post_format": "single",
            "title": meta["renoir"][1],
            "artist": meta["renoir"][0],
            "date": meta["renoir"][2],
            "museum_url": meta["renoir"][3],
            "institution": meta["renoir"][5],
            "caption_en": "Earlier profile post — Pierre-Auguste Renoir, Romaine Lacaux (1864).",
            "source_image": str((assets_dir / "renoir.jpg").resolve()),
            "published_at_label": "Önceki profil · 1",
        },
        {
            "id": "hist-02",
            "post_format": "carousel",
            "title": meta["church"][1],
            "artist": meta["church"][0],
            "date": meta["church"][2],
            "museum_url": meta["church"][3],
            "institution": meta["church"][5],
            "caption_en": "Earlier profile post — Frederic Edwin Church, Twilight in the Wilderness (1860).",
            "source_image": str((assets_dir / "church.jpg").resolve()),
            "published_at_label": "Önceki profil · 2",
        },
        {
            "id": "hist-03",
            "post_format": "single",
            "title": meta["remb"][1],
            "artist": meta["remb"][0],
            "date": meta["remb"][2],
            "museum_url": meta["remb"][3],
            "institution": meta["remb"][5],
            "caption_en": "Earlier profile post — Rembrandt van Rijn, Portrait of a Woman (1635 or earlier).",
            "source_image": str((assets_dir / "remb.jpg").resolve()),
            "published_at_label": "Önceki profil · 3",
        },
    ]
    return drafts, history


def create_astra_pilot_workspace(
    output_dir: str | Path,
    draft_specs: list[dict],
    *,
    history_specs: list[dict] = (),
    pinned_ids: tuple[str, ...] = (),
    profile_handle: str = "@artfolio.db",
) -> AstraPilotWorkspaceState:
    """Create a self-contained local Astra pilot workspace with real 1080x1350 renders."""
    output = Path(output_dir).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Pilot workspace output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".astra-pilot-") as tmp:
        stage = Path(tmp)
        (stage / "sources").mkdir()
        (stage / "renders").mkdir()
        (stage / "history").mkdir()

        copied_sources: dict[str, str] = {}
        drafts: list[PilotDraftItem] = []

        for spec in draft_specs:
            art_records: list[PilotArtworkRecord] = []
            for raw_art in spec["artworks"]:
                src_path = Path(raw_art["image_path"]).resolve()
                if not src_path.is_file():
                    raise FileNotFoundError(f"Missing artwork image: {src_path}")
                key = raw_art["source_key"]
                if key not in copied_sources:
                    rel_src = f"sources/{key}{src_path.suffix.lower() or '.jpg'}"
                    shutil.copyfile(src_path, stage / rel_src)
                    copied_sources[key] = rel_src
                art_records.append(
                    PilotArtworkRecord(
                        source_key=key,
                        image_path=copied_sources[key],
                        artist=raw_art["artist"],
                        title=raw_art["title"],
                        date=raw_art.get("date", ""),
                        institution=raw_art.get("institution", ""),
                        museum_url=raw_art.get("museum_url", ""),
                        focus=raw_art.get("focus"),
                    )
                )
            draft = PilotDraftItem(
                id=spec["id"],
                post_format=spec["post_format"],
                design_id=spec["design_id"],
                editorial_title=spec["editorial_title"],
                caption_en=spec["caption_en"],
                artworks=tuple(art_records),
                detail_focuses=tuple(spec["detail_focuses"]) if spec.get("detail_focuses") else None,
                slide_count=spec.get("slide_count", 1),
                reel_status=spec.get("reel_status", "cover_ready" if spec["post_format"] == "reel" else "not_applicable"),
                published_at_label=spec.get("published_at_label", ""),
            )
            drafts.append(_render_draft_item(stage, draft))

        history_posts: list[PriorProfilePost] = []
        for hspec in history_specs:
            src_img = Path(hspec["source_image"]).resolve()
            if not src_img.is_file():
                raise FileNotFoundError(f"Missing history image: {src_img}")
            rel_hist = f"history/{hspec['id']}.jpg"
            with Image.open(src_img) as pil:
                pil.convert("RGB").save(stage / rel_hist, format="JPEG", quality=90, optimize=True)
            history_posts.append(
                PriorProfilePost(
                    id=hspec["id"],
                    post_format=hspec.get("post_format", "single"),
                    title=hspec["title"],
                    artist=hspec["artist"],
                    date=hspec.get("date", ""),
                    institution=hspec.get("institution", ""),
                    museum_url=hspec.get("museum_url", ""),
                    caption_en=hspec.get("caption_en", ""),
                    image_path=rel_hist,
                    published_at_label=hspec.get("published_at_label", ""),
                )
            )

        state = AstraPilotWorkspaceState(
            profile_handle=profile_handle,
            drafts=tuple(drafts),
            draft_order=tuple(item.id for item in drafts),
            pinned_ids=tuple(pinned_ids),
            history_posts=tuple(history_posts),
        )
        save_pilot_state(stage, state)
        _atomic_write_text(stage / "index.html", render_pilot_editor_html(state, editor_token=None))
        stage.rename(output)
    return state


def render_pilot_editor_html(
    state: AstraPilotWorkspaceState,
    *,
    editor_token: str | None,
) -> str:
    """Render the local 3x3 Instagram profile preview + card inspector + catalog browser."""
    catalog = load_design_catalog()
    usable = list_usable_designs()
    grid_cells = compute_profile_grid_cells(state)

    bootstrap = {
        "token": editor_token,
        "profile_handle": state.profile_handle,
        "draft_order": list(state.draft_order),
        "pinned_ids": list(state.pinned_ids),
        "cells": grid_cells,
        "usable_designs": [entry.model_dump(mode="json") for entry in usable],
        "catalog_summary": {
            "total": len(catalog.designs),
            "ready": len(usable),
            "catalog_only": len(catalog.designs) - len(usable),
            "families": [fam.model_dump(mode="json") for fam in catalog.families],
        },
    }
    payload = (
        json.dumps(bootstrap, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
    )

    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artfolio Astra · Yerel 3×3 Profil Önizleme</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f5f2ea;color:#1c1b18;font:15px/1.5 system-ui,-apple-system,sans-serif}}
header{{background:#19241e;color:#f3ebd6;padding:18px 28px;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px}}
header h1{{margin:0;font:24px/1.2 Georgia,serif;font-weight:normal}}
header p{{margin:4px 0 0;font-size:13px;color:#cfc5ab}}
.badge{{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;background:#2f4438;color:#f3ebd6;border:1px solid #4d6a59}}
.layout{{max-width:1320px;margin:24px auto;padding:0 20px 48px;display:grid;grid-template-columns:minmax(0,1.25fr) minmax(320px,.95fr);gap:28px;align-items:start}}
@media(max-width:960px){{.layout{{grid-template-columns:1fr}}}}
.phone-frame{{background:#fff;border:1px solid #d9d2c2;border-radius:16px;padding:18px;box-shadow:0 8px 30px rgba(25,24,20,.06)}}
.profile-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid #ece7da}}
.handle{{font:600 18px/1.2 system-ui}}
.sub{{font-size:12px;color:#666}}
.grid-3x3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}}
.cell{{position:relative;aspect-ratio:4/5;background:#ebe6d9;overflow:hidden;cursor:pointer;border:2px solid transparent;padding:0;text-align:left}}
.cell:hover{{border-color:#8b7549}}
.cell.selected{{border-color:#1c4d3a;box-shadow:0 0 0 2px #1c4d3a}}
.cell img{{display:block;width:100%;height:100%;object-fit:cover}}
.cell-tags{{position:absolute;top:6px;left:6px;right:6px;display:flex;justify-content:space-between;gap:4px;pointer-events:none}}
.tag{{background:rgba(20,20,18,.78);color:#fff9ed;font-size:10px;padding:2px 6px;border-radius:4px;letter-spacing:.03em;text-transform:uppercase}}
.tag.pin{{background:#b57c2a}}
.tag.hist{{background:rgba(70,70,66,.78)}}
.cell-foot{{position:absolute;left:0;right:0;bottom:0;padding:16px 7px 6px;background:linear-gradient(transparent,rgba(15,15,13,.82));color:#fff9ed;font-size:11px;line-height:1.25}}
.inspector{{background:#fff;border:1px solid #d9d2c2;border-radius:16px;padding:20px;position:sticky;top:20px}}
.inspector h2{{margin:0 0 4px;font:22px/1.2 Georgia,serif;font-weight:normal}}
.meta-line{{font-size:13px;color:#5a554a;margin:0 0 14px}}
.preview-box{{width:100%;max-height:360px;object-fit:contain;background:#f1ece0;border-radius:8px;display:block;margin-bottom:14px}}
label{{display:block;font-size:13px;font-weight:600;margin:12px 0 6px}}
select,textarea{{width:100%;font:14px/1.45 system-ui;padding:9px 10px;border:1px solid #c5bca8;border-radius:8px;background:#fff}}
textarea{{resize:vertical;min-height:110px;background:#faf8f3}}
.btn-row{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}
button.action{{padding:9px 14px;border-radius:8px;border:1px solid #2b2a26;background:#19241e;color:#fff9ed;font:13px system-ui;cursor:pointer}}
button.action.secondary{{background:#fff;color:#1c1b18;border-color:#bdb5a2}}
button.action:disabled{{opacity:.45;cursor:default}}
.status-bar{{margin-top:12px;font-size:13px;color:#284635;min-height:20px}}
.art-list{{margin:10px 0;padding:10px 12px;background:#faf8f3;border:1px solid #e6dfd0;border-radius:8px;font-size:13px}}
.art-list div+div{{margin-top:8px;padding-top:8px;border-top:1px solid #e6dfd0}}
.art-list a{{color:#1d4c39}}
.catalog-note{{margin-top:18px;padding-top:14px;border-top:1px solid #e8e2d3;font-size:12px;color:#5c574c}}
</style>
</head>
<body>
<header>
  <div>
    <h1>Artfolio Astra · Yerel 3×3 Profil Önizleme</h1>
    <p>Yalnız yerel pilot önizleme · Canlı yayın, kuyruk ve zamanlama servislerine veri göndermez.</p>
  </div>
  <div>
    <span class="badge">5/100 şablon kullanıma hazır</span>
    <span class="badge">100/100 katalog kayıtlı</span>
  </div>
</header>

<div class="layout">
  <section class="phone-frame">
    <div class="profile-top">
      <div>
        <div class="handle" id="profile-handle">{html.escape(state.profile_handle)}</div>
        <div class="sub">Yeni yayından eskiye · En fazla 3 sabit gönderi üstte, tekrarsız</div>
      </div>
      <div class="sub" id="grid-summary"></div>
    </div>
    <div class="grid-3x3" id="profile-grid"></div>
  </section>

  <aside class="inspector" id="inspector">
    <h2 id="ins-title">Bir kart seçin</h2>
    <p class="meta-line" id="ins-meta"></p>
    <img class="preview-box" id="ins-img" alt="Seçili gönderi önizlemesi">
    <div class="art-list" id="ins-artworks"></div>

    <div id="draft-controls">
      <label for="design-select">Astra Tasarımı (gerçek çıktı yeniden üretilir)</label>
      <select id="design-select"></select>
      <div class="btn-row">
        <button type="button" class="action" id="btn-apply-design">Tasarımı Uygula</button>
        <button type="button" class="action secondary" id="btn-pin">Sabitle / Kaldır</button>
        <button type="button" class="action secondary" id="btn-newer">Daha Yeniye Al</button>
        <button type="button" class="action secondary" id="btn-older">Daha Eskiye Al</button>
      </div>
    </div>

    <label for="ins-caption">İngilizce Açıklama Taslağı</label>
    <textarea id="ins-caption" readonly></textarea>
    <div class="status-bar" id="status-bar" role="status" aria-live="polite"></div>

    <div class="catalog-note" id="catalog-note"></div>
  </aside>
</div>

<script type="application/json" id="pilot-data">{payload}</script>
<script>
let state = JSON.parse(document.getElementById('pilot-data').textContent);
let selectedId = state.cells.length ? state.cells[0].id : null;

const gridEl = document.getElementById('profile-grid');
const summaryEl = document.getElementById('grid-summary');
const insTitle = document.getElementById('ins-title');
const insMeta = document.getElementById('ins-meta');
const insImg = document.getElementById('ins-img');
const insArtworks = document.getElementById('ins-artworks');
const insCaption = document.getElementById('ins-caption');
const designSelect = document.getElementById('design-select');
const draftControls = document.getElementById('draft-controls');
const statusBar = document.getElementById('status-bar');
const btnApply = document.getElementById('btn-apply-design');
const btnPin = document.getElementById('btn-pin');
const btnNewer = document.getElementById('btn-newer');
const btnOlder = document.getElementById('btn-older');
const catalogNote = document.getElementById('catalog-note');

function formatLabel(fmt, slides) {{
  if (fmt === 'carousel') return 'Carousel (' + slides + ' slayt)';
  if (fmt === 'reel') return 'Reels Kapağı';
  return 'Tekli';
}}

function renderGrid() {{
  gridEl.innerHTML = '';
  summaryEl.textContent = state.draft_order.length + ' taslak · ' + state.pinned_ids.length + '/3 sabit';
  for (const cell of state.cells) {{
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cell' + (cell.id === selectedId ? ' selected' : '');
    btn.dataset.id = cell.id;
    const fmtTag = cell.post_format === 'carousel' ? ('1/' + cell.slide_count) : (cell.post_format === 'reel' ? 'REEL' : 'SINGLE');
    const pinBadge = cell.pinned ? '<span class="tag pin">SABİT</span>' : (cell.kind === 'history' ? '<span class="tag hist">GEÇMİŞ</span>' : '<span></span>');
    btn.innerHTML =
      '<img src="' + cell.image_path + '" alt="' + cell.editorial_title.replace(/"/g, '&quot;') + '">' +
      '<div class="cell-tags">' + pinBadge + '<span class="tag">' + fmtTag + '</span></div>' +
      '<div class="cell-foot">' + (cell.design_id ? cell.design_id + ' · ' : '') + cell.editorial_title + '</div>';
    btn.addEventListener('click', () => {{
      selectedId = cell.id;
      renderGrid();
      renderInspector();
    }});
    gridEl.appendChild(btn);
  }}
}}

function renderInspector() {{
  const cell = state.cells.find(c => c.id === selectedId);
  if (!cell) return;
  insTitle.textContent = cell.editorial_title;
  insMeta.textContent =
    formatLabel(cell.post_format, cell.slide_count) +
    (cell.design_id ? ' · ' + cell.design_id : ' · Önceki profil') +
    (cell.pinned ? ' · Sabitlenmiş' : '') +
    (cell.published_at_label ? ' · ' + cell.published_at_label : '');
  insImg.src = cell.image_path;
  insCaption.value = cell.caption_en || '';

  insArtworks.innerHTML = cell.artworks.map(art => {{
    const link = art.museum_url
      ? '<a href="' + art.museum_url + '" target="_blank" rel="noopener noreferrer">Müze bağlantısı ↗</a>'
      : '<span>Yerel kaynak</span>';
    return '<div><strong>' + art.title + '</strong><br>' + art.artist + (art.date ? ' (' + art.date + ')' : '') +
      (art.institution ? ' · ' + art.institution : '') + '<br>' + link + '</div>';
  }}).join('');

  const isDraft = cell.kind === 'draft';
  const live = Boolean(state.token);
  draftControls.style.display = isDraft ? 'block' : 'none';
  if (isDraft) {{
    const reqSources = cell.artworks.length;
    designSelect.innerHTML = '';
    for (const d of state.usable_designs) {{
      if (d.required_sources !== reqSources) continue;
      const opt = document.createElement('option');
      opt.value = d.id;
      opt.textContent = d.id + ' — ' + d.name + ' (' + d.family_name + ')';
      if (d.id === cell.design_id) opt.selected = true;
      designSelect.appendChild(opt);
    }}
    const idx = state.draft_order.indexOf(cell.id);
    btnApply.disabled = !live;
    btnPin.disabled = !live;
    btnPin.textContent = cell.pinned ? 'Sabitlemeyi Kaldır' : 'Üste Sabitle';
    btnNewer.disabled = !live || idx <= 0;
    btnOlder.disabled = !live || idx < 0 || idx >= state.draft_order.length - 1;
    if (!live) {{
      statusBar.textContent = 'Statik önizleme modu. Canlı tasarım değişimi ve kayıt için yerel sunucuyla açın.';
    }}
  }}

  catalogNote.textContent =
    'Katalog durumu: ' + state.catalog_summary.total + ' tasarım kayıtlı (' +
    state.catalog_summary.ready + ' aktif pilot şablonu, ' +
    state.catalog_summary.catalog_only + ' katalog kaydı). Aktarımı bitmemiş tasarımlar otomatik seçimde kullanılmaz.';
}}

async function apiPost(url, body) {{
  const res = await fetch(url, {{
    method: 'POST',
    headers: {{
      'Content-Type': 'application/json',
      'X-Artfolio-Editor': state.token,
    }},
    body: JSON.stringify(body),
  }});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'İşlem başarısız');
  state.draft_order = data.draft_order;
  state.pinned_ids = data.pinned_ids;
  state.cells = data.cells;
  renderGrid();
  renderInspector();
  return data;
}}

btnApply.addEventListener('click', async () => {{
  const cell = state.cells.find(c => c.id === selectedId);
  if (!cell || cell.kind !== 'draft') return;
  statusBar.textContent = 'Gerçek 1080×1350 görsel yeniden üretiliyor…';
  btnApply.disabled = true;
  try {{
    await apiPost('/api/design', {{ draft_id: cell.id, design_id: designSelect.value }});
    statusBar.textContent = 'Tasarım güncellendi ve yerel diske kaydedildi (' + designSelect.value + ').';
  }} catch (err) {{
    statusBar.textContent = 'Hata: ' + err.message;
  }} finally {{
    btnApply.disabled = !state.token;
  }}
}});

async function moveDraft(delta) {{
  const idx = state.draft_order.indexOf(selectedId);
  const nextIdx = idx + delta;
  if (idx < 0 || nextIdx < 0 || nextIdx >= state.draft_order.length) return;
  const order = [...state.draft_order];
  const [moved] = order.splice(idx, 1);
  order.splice(nextIdx, 0, moved);
  statusBar.textContent = 'Sıra güncelleniyor…';
  try {{
    await apiPost('/api/order', {{ draft_order: order, pinned_ids: state.pinned_ids }});
    statusBar.textContent = 'Sıra güncellendi ve yerel diske kaydedildi.';
  }} catch (err) {{
    statusBar.textContent = 'Hata: ' + err.message;
  }}
}}

btnNewer.addEventListener('click', () => moveDraft(-1));
btnOlder.addEventListener('click', () => moveDraft(1));

btnPin.addEventListener('click', async () => {{
  let pins = [...state.pinned_ids];
  if (pins.includes(selectedId)) {{
    pins = pins.filter(id => id !== selectedId);
  }} else {{
    if (pins.length >= 3) {{
      statusBar.textContent = 'En fazla 3 gönderi sabitlenebilir. Önce bir sabit gönderiyi kaldırın.';
      return;
    }}
    pins.push(selectedId);
  }}
  statusBar.textContent = 'Sabit gönderiler güncelleniyor…';
  try {{
    await apiPost('/api/order', {{ draft_order: state.draft_order, pinned_ids: pins }});
    statusBar.textContent = 'Sabit gönderi seçimi diske kaydedildi.';
  }} catch (err) {{
    statusBar.textContent = 'Hata: ' + err.message;
  }}
}});

renderGrid();
renderInspector();
</script>
</body>
</html>
"""


def astra_pilot_server(workspace_dir: str | Path, *, port: int = 38145) -> HTTPServer:
    """Create a loopback-only local HTTP server for the Astra 3x3 profile editor."""
    root = Path(workspace_dir).resolve()
    load_pilot_state(root)
    session_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(
                status,
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _host_allowed(self) -> bool:
            actual_port = self.server.server_port
            return self.headers.get("Host") in {
                f"127.0.0.1:{actual_port}",
                f"localhost:{actual_port}",
            }

        def _validate_post(self) -> dict:
            actual_port = self.server.server_port
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            token = self.headers.get("X-Artfolio-Editor")
            if host not in {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"} or origin != f"http://{host}":
                raise PermissionError("Open the editor from its local address")
            if not token or not token.isascii() or not hmac.compare_digest(token, session_token):
                raise PermissionError("Editor session expired; reload the page")
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise ValueError("Expected JSON body")
            length = self.headers.get("Content-Length")
            if length is None or not length.isdecimal() or not 1 <= int(length) <= MAX_API_BYTES:
                raise ValueError("Invalid content length")
            return json.loads(self.rfile.read(int(length)))

        def do_GET(self) -> None:
            if not self._host_allowed():
                self._json(403, {"error": "Invalid editor host"})
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                state = load_pilot_state(root)
                body = render_pilot_editor_html(state, editor_token=session_token).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if re.fullmatch(r"/(renders|history|sources)/[a-zA-Z0-9._-]+\.(jpg|jpeg|png)", path):
                asset = (root / path.lstrip("/")).resolve()
                if asset.is_relative_to(root) and asset.is_file() and asset.stat().st_size <= 12_000_000:
                    mime = "image/png" if asset.suffix.lower() == ".png" else "image/jpeg"
                    self._send(200, asset.read_bytes(), mime)
                    return
            self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in {"/api/design", "/api/order"}:
                self._json(404, {"error": "Not found"})
                return
            try:
                payload = self._validate_post()
                if path == "/api/design":
                    if not isinstance(payload, dict) or set(payload) != {"draft_id", "design_id"}:
                        raise ValueError("Expected draft_id and design_id")
                    state = update_draft_design(root, payload["draft_id"], payload["design_id"])
                else:
                    if not isinstance(payload, dict) or set(payload) != {"draft_order", "pinned_ids"}:
                        raise ValueError("Expected draft_order and pinned_ids")
                    state = update_workspace_order(
                        root,
                        draft_order=payload["draft_order"],
                        pinned_ids=payload["pinned_ids"],
                    )
                self._json(
                    200,
                    {
                        "draft_order": list(state.draft_order),
                        "pinned_ids": list(state.pinned_ids),
                        "cells": compute_profile_grid_cells(state),
                    },
                )
            except PermissionError as exc:
                self._json(403, {"error": str(exc)})
            except (ValueError, KeyError, ValidationError, FileNotFoundError) as exc:
                self._json(422, {"error": str(exc)})

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.editor_token = session_token  # type: ignore[attr-defined]
    return server
