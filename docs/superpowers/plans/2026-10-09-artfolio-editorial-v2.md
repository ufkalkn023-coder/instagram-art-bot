# Artfolio Editorial V2 Implementation Plan

> **For agentic workers:** Execute the approved first delivery with focused tests
> and one final independent review; preserve concurrent style edits. No commits/pushes.

**Goal:** Produce evidence-bearing public titles and a local 30×3 cover review gallery.
**Architecture:** Preview-only typed editorial plan, bounded Gemini provider, three
Pillow layouts and a local acquisition/rendering CLI. Production publication stays separate.
**Tech Stack:** Existing Python 3.10, Pydantic, Pillow, google-genai and museum adapters.
**Spec:** ../specs/2026-10-09-artfolio-editorial-v2.md

## Global Constraints

Preserve `.mimosa/`, existing uncommitted style work and production publication
state. No new dependencies, deployment, IG requests or R2 writes. Secure image
validation and strict source rights precede any provider/rendering use.

## Review Focus

Untrusted model output, false source quotations, repeated titles, invalid focus
boxes, cache invalidation, quota failure, path traversal and gallery HTML escaping.

### 1. Editorial plan and provider

Files: src/editorial_v2.py; tests/test_editorial_v2.py.
Interfaces: typed source input, validated response, selected public title/angle/
evidence, rejection reasons, exact-identity cache and bounded Gemini call.

- [x] Red tests for unknown identities, unverifiable quotes, duplicate candidates,
  fail-closed visual review, factual fallback, image payload and cache invalidation.
- [x] Implement selection, input/output bounds, explicit Medium and opt-in calls.
- [x] Verify focused tests.

### 2. Three cover styles

Files: src/editorial_design.py; tests/test_editorial_design.py.
Interface: `render_editorial_cover(image_path, public_title, subtitle, style,
output_path, focus=None)` → layout report with output path/style/crop basis.
Focus is normalized left/top/right/bottom, with valid area and bounds.

- [x] Red tests for whole-image retention, focus bounds, missing-focus fallback,
  long text fitting, exact size and distinct styles.
- [x] Implement gallery-field Artwork First and bounded Detail Study alongside
  the existing Museum Journal primitives, sharing valid bundled fonts.
- [x] Verify focused tests and inspect examples.

### 3. Local batch and gallery

Files: scripts/preview_editorial_v2.py; tests/test_editorial_v2_preview.py; README.md.
Interface: local source manifest or bounded Cleveland acquisition, opt-in Gemini,
maximum calls, isolated output/cache, optional last-100-headline JSON.

- [x] Red tests for strict rights, safe paths, HTML escaping and three outputs/work.
- [x] Implement evidence manifest, resumed source input and report generation.
- [x] Produce 30 real works/90 covers; disclose AI availability and fallback counts.

### 4. Integration evidence

- [x] Full suite, Ruff, compile and diff validation; one fresh-context review.
- [x] Inspect the gallery, record local-only state and remaining rollout/Phase 3–4 work.

## Delivery evidence

User explicitly requested no live Gemini calls. Gallery contains 30 unique CC0
Cleveland works, 90 1080×1350 JPEGs, zero AI calls, 30 factual-title fallbacks,
and zero supplied historical headlines. All Detail Study slots are visibly
labeled full-artwork alternatives because no model focus was obtained. This is
a layout review artifact, not evidence of successful live AI curation.

Artifacts: `/Users/ufuk/.codex/visualizations/2026/10/09/01a11e7b-50d5-7921-9b2f-7a1562751434/editorial-v2-gallery/`;
reusable `sources.json` is in sibling `editorial-v2-sources-complete/`.
Desktop and 390px mobile inspection passed with no horizontal overflow. Four
different composition examples were inspected as a contact sheet.

Independent review found generic phrase variants and cache-write errors;
both were corrected with regression tests and the reviewer confirmed resolution.
SDK transport timeout behavior was verified against the locked SDK source and a
mock timeout regression. Advanced narratives, complete editorial truth review,
performance learning, live AI validation and production rollout remain separate.

Final verification: focused **34 passed**; full locked Python 3.10 suite
**1583 passed, 13 skipped in 72.09s**, log
`/private/tmp/artfolio-editorial-v2-final-tests.log`. Changed-file Ruff, new-file
format check, compileall and git diff --check passed. UfukOS project CURRENT,
DEC-036 and a new Codex work record were updated; no secrets persisted.

Subsequent user visual feedback: the original third column's full-artwork
fallback did not satisfy detail comparison. Added explicit reviewed preview-focus
input, provenance/rectangle reporting and early input validation. New gallery
`editorial-v2-detail-gallery/` contains 30 actual detail crops, 90 total covers,
zero AI calls; crop input is sibling `editorial-detail-focus.json`. All source
images and all detail outputs were visually inspected; two crops were adjusted.
Final focused **42 passed**. Full **1590 passed, 13 skipped in 72.88s** before the
final CLI-only validation-order guard; that guard is included in the 42-test
focused run. Ruff/compile/diff passed; independent review findings resolved.

Ruling: user explicitly delegated safe local implementation and asks for internal,
concise planning followed by execution. The supplied brief and accepted first
delivery are the spec; proceed without a redundant approval round. Renderer work
may be delegated independently under dispatching-parallel-agents; shared files
remain primary-owned. The local 30×3 artifact is the concrete user review surface.
