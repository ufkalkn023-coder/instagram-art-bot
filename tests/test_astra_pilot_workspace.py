"""Tests for the 9-draft local 3x3 profile preview workspace, persistence, and live editor API."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

from src.astra_pilot_workspace import (
    astra_pilot_server,
    compute_profile_grid_cells,
    create_astra_pilot_workspace,
    load_pilot_state,
    update_draft_design,
    update_workspace_order,
)


def _create_sample_artworks(tmp_path: Path) -> list[dict]:
    records = []
    palettes = [
        ("#8b261d", "#e3a64b"),
        ("#19335c", "#d9d2b8"),
        ("#2d4a3e", "#f0d9a8"),
        ("#663344", "#e8c4b8"),
        ("#334455", "#c4d4e0"),
        ("#554422", "#e4d0a0"),
        ("#223344", "#99bbaa"),
        ("#773322", "#ddccaa"),
        ("#2a4d3c", "#d8b46c"),
        ("#4c2a3e", "#e2c9a6"),
        ("#1f3a52", "#c9d6df"),
        ("#5a3b2c", "#e8d5b7"),
    ]
    for idx, (bg, fg) in enumerate(palettes, 1):
        img_path = tmp_path / f"art_{idx:02d}.jpg"
        im = Image.new("RGB", (900, 750), bg)
        ImageDraw.Draw(im).ellipse((150, 120, 750, 630), fill=fg)
        im.save(img_path, format="JPEG", quality=90)
        records.append(
            {
                "source_key": f"art_{idx:02d}",
                "image_path": str(img_path),
                "artist": f"Museum Artist {idx}",
                "title": f"Museum Artwork {idx}",
                "date": f"18{70 + idx}",
                "institution": "Cleveland Museum of Art",
                "museum_url": f"https://clevelandart.org/art/1900.{idx}",
            }
        )
    return records


def _build_nine_draft_workspace(tmp_path: Path) -> Path:
    arts = _create_sample_artworks(tmp_path / "src_imgs" if False else tmp_path)
    draft_specs = [
        {
            "id": "draft-01",
            "post_format": "single",
            "design_id": "astra-001",
            "editorial_title": "Quiet room",
            "caption_en": "English caption 1.",
            "artworks": [arts[0]],
            "slide_count": 1,
        },
        {
            "id": "draft-02",
            "post_format": "carousel",
            "design_id": "astra-013",
            "editorial_title": "Three vertical edges",
            "caption_en": "English caption 2.",
            "artworks": [arts[1]],
            "slide_count": 4,
        },
        {
            "id": "draft-03",
            "post_format": "reel",
            "design_id": "astra-083",
            "editorial_title": "Who gets the light?",
            "caption_en": "English caption 3.",
            "artworks": [arts[2]],
            "slide_count": 1,
        },
        {
            "id": "draft-04",
            "post_format": "carousel",
            "design_id": "astra-061",
            "editorial_title": "Pigment study",
            "caption_en": "English caption 4.",
            "artworks": [arts[3]],
            "slide_count": 3,
        },
        {
            "id": "draft-05",
            "post_format": "carousel",
            "design_id": "astra-094",
            "editorial_title": "Three rooms one",
            "caption_en": "English caption 5.",
            "artworks": [arts[4], arts[5], arts[6]],
            "slide_count": 4,
        },
        {
            "id": "draft-06",
            "post_format": "single",
            "design_id": "astra-001",
            "editorial_title": "Afternoon stillness",
            "caption_en": "English caption 6.",
            "artworks": [arts[7]],
            "slide_count": 1,
        },
        {
            "id": "draft-07",
            "post_format": "reel",
            "design_id": "astra-083",
            "editorial_title": "Nocturne light",
            "caption_en": "English caption 7.",
            "artworks": [arts[8]],
            "slide_count": 1,
        },
        {
            "id": "draft-08",
            "post_format": "single",
            "design_id": "astra-061",
            "editorial_title": "Five pigments",
            "caption_en": "English caption 8.",
            "artworks": [arts[9]],
            "slide_count": 1,
        },
        {
            "id": "draft-09",
            "post_format": "carousel",
            "design_id": "astra-094",
            "editorial_title": "Three rooms two",
            "caption_en": "English caption 9.",
            "artworks": [arts[10], arts[11], arts[0]],
            "slide_count": 4,
        },
    ]
    history_specs = [
        {
            "id": "hist-01",
            "post_format": "single",
            "title": "Previous Profile Work",
            "artist": "Earlier Artist",
            "date": "1860",
            "institution": "Cleveland Museum of Art",
            "museum_url": "https://clevelandart.org/art/1860.1",
            "caption_en": "Prior profile post.",
            "source_image": arts[11]["image_path"],
        }
    ]
    workspace = tmp_path / "pilot_workspace"
    create_astra_pilot_workspace(
        workspace,
        draft_specs,
        history_specs=history_specs,
        pinned_ids=("draft-05", "draft-03", "draft-09"),
    )
    return workspace


def test_nine_draft_workspace_pins_top_three_without_duplication_and_persists(tmp_path):
    workspace = _build_nine_draft_workspace(tmp_path)
    state = load_pilot_state(workspace)
    assert state.publication_ready is False
    assert len(state.drafts) == 9

    cells = compute_profile_grid_cells(state)
    # 9 drafts + 1 history post = 10 unique cells (pinned items are NOT duplicated).
    assert len(cells) == 10
    assert len({cell["id"] for cell in cells}) == 10
    assert [cell["id"] for cell in cells[:3]] == ["draft-05", "draft-03", "draft-09"]
    assert all(cell["pinned"] is True for cell in cells[:3])
    assert all(cell["pinned"] is False for cell in cells[3:])
    assert cells[-1]["kind"] == "history"

    # Change design -> real image re-renders on disk and persists across reload.
    before_path = next(d.rendered_path for d in state.drafts if d.id == "draft-01")
    updated = update_draft_design(workspace, "draft-01", "astra-061")
    after_path = next(d.rendered_path for d in updated.drafts if d.id == "draft-01")
    assert after_path != before_path
    assert (workspace / after_path).is_file()

    # Change order and pins -> persists when reloaded from disk.
    new_order = list(reversed(state.draft_order))
    update_workspace_order(workspace, draft_order=new_order, pinned_ids=["draft-02"])
    reloaded = load_pilot_state(workspace)
    assert list(reloaded.draft_order) == new_order
    assert list(reloaded.pinned_ids) == ["draft-02"]
    assert next(d.design_id for d in reloaded.drafts if d.id == "draft-01") == "astra-061"


def test_loopback_editor_api_rerenders_and_persists_order(tmp_path):
    workspace = _build_nine_draft_workspace(tmp_path)
    server = astra_pilot_server(workspace, port=0)
    port = server.server_port
    token = server.editor_token  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            html_body = resp.read().decode("utf-8")
            assert resp.status == 200
            assert "Artfolio Astra" in html_body
            assert "grid-3x3" in html_body

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/design",
            data=json.dumps({"draft_id": "draft-06", "design_id": "astra-013"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "X-Artfolio-Editor": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            changed = next(c for c in data["cells"] if c["id"] == "draft-06")
            assert changed["design_id"] == "astra-013"
            assert (workspace / changed["image_path"]).is_file()

        reloaded = load_pilot_state(workspace)
        assert next(d.design_id for d in reloaded.drafts if d.id == "draft-06") == "astra-013"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
