# Artfolio story production and review

User approved the proposed coherent development package with “geliştirelim o halde”.
Preserve existing local edits/.mimosa and production publication contracts. No commits,
pushes, R2/Instagram operations or live Gemini. Use current Pillow/Pydantic/runtime.

## Contract

A versioned StoryPlan owns theme/public title/angle/headline evidence, narrative,
cover style, stable slides and their source references. Unique artworks live in a
separate source registry. Repeated detail/full views reference the same artwork ID;
slides have separate unique IDs. The local StoryPackage exposes unique artwork IDs
and ordered slide assets and never masquerades as legacy PreparedFeedContent.

Narratives: single_study (1 source), comparison (2), thematic_selection (3–8).
Slides: cover, artwork, detail, comparison, context, closing. First cover/last closing,
3–12 pages, stable IDs, known source references, bounded copy, typed provenance and
normalized crops. Context is an exact source-description excerpt with quote evidence.
Absent verified context is omitted. Detail input can be explicit reviewed preview
focus or local geometry/edge proposals, recorded honestly; none is certified visual
or historical truth. AI remains disabled and all projects require manual review.

Quality separates fatal gates from warnings: source rights/file validity, missing
identity, invalid/duplicate crops, pixel/upscaling bounds, source quote integrity,
copy capacity and title repetition. Valid sources receive bounded local detail
proposals; no useful/large-enough region produces a labeled no-detail narrative.
Style history selects among supported cover variants without bypassing gates.

Persistent local project: atomic versioned project.json, immutable source registry,
plan and revision. Stable slide content hashes include actual source bytes, metadata,
fonts and renderer version. Only changed pages render again. Metadata records cover,
narrative and headline provenance for future existing Insights integration; no new
learning/production activation in this delivery.

Browser review: title/slide copy/order and detail rectangle controls; save/render
uses an optional localhost-only editor with fixed project paths, capped JSON,
same-origin + secret header protection, revision CAS and sequential writes. Invalid
changes cannot overwrite the last valid project. Static gallery also works offline.

## Files / ownership and verification

1. Primary: src/story_plan.py, src/story_quality.py; tests/test_story_plan.py,
   tests/test_story_quality.py. Red/green tests for identity separation, narratives,
   quoted context, no-detail fallbacks, duplicate/low-resolution crops and rights.
2. Independent renderer worker: src/story_design.py, tests/test_story_design.py.
   Interface render_story_slide(slide, sources, public_title, cover_style, output_path,
   *, position, total) -> dict. sources maps artwork IDs to SourceArtwork; slide has
   id/role/artwork_ids/title/body/focus/focus_basis/evidence. Return path, role,
   actual_style/crop_basis where relevant. Whole work, genuine detail, two-work
   comparison, context and closing layouts; 1080x1350 JPEG; complete readable copy.
3. Primary: src/story_project.py, scripts/preview_story.py; tests/test_story_project.py,
   tests/test_story_editor.py. Atomic save/revision, render hashing, failure preservation,
   escaped gallery, safe update routes, browser UI and invalid-edit/CSRF regression.
4. README, three actual museum-source story artifacts; desktop/mobile inspection,
   focused + full tests, Ruff/format/compile/diff; independent review and UfukOS update.

No request for extra design/commit approvals: explicit user authorization and safe
local execution override redundant skill approval rounds. Renderer may be delegated
under dispatching-parallel-agents after this stable interface is implemented.
