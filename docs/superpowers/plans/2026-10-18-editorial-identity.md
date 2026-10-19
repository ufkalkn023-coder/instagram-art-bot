# Artfolio editorial identity implementation

**Goal:** Replace generic local presentation with source-backed editorial briefs,
specific writing, meaningful details/comparisons and three restrained art directions.

**Architecture:** Add an isolated local editorial draft workflow beside the existing
story publisher. Reuse strict source rights, image validation, fonts and crop checks.
Existing reviewed projects, sealed packages and production contracts stay untouched.
All new outputs remain human-review drafts, with no approval/export/queue shortcut.

**Execution:** Inline, sequentially under the user's approved five-part scope.

1. Typed brief: one angle, source membership, explicit source quotes/catalogue facts
   or clearly labeled visual notes. Reject unsupported quotes, duplicate/unknown
   identities and rights failures. A visual note is a human review task, not an
   automated image-understanding certificate.
2. Writing gate: reject stock hooks/general praise, repeated/filler titles and
   invented context. Prefer one short lead and factual credits; missing lead stays
   absent. Source quotations remain attached to their evidence. No model calls.
3. Details: render only explicitly annotated focus regions with a specific note;
   apply existing resolution, emptiness and overlap checks. Never infer semantics
   from geometry suggestions; do not pad the page count with redundant crops.
4. Comparisons: require a declared aspect, different observations on both sources
   and a substantive rationale; artist/museum membership alone is insufficient.
5. Art direction: artwork-led, journal and detail-study local variants. Preserve
   full artwork and color, quiet credits, no obligatory teaser/CTA or closing page.
   Draft sequence follows the brief. Render three genuine-source examples for QA.

Behavior tests first for each part. Then verify complete locked Python3.10 suite,
Ruff, compile, file/diff preservation, browser/pixel output and independent review.
Document actual commands and limits. No live publishing, paid AI, production-state
write, commit/push/merge or deployment. Record the verified milestone in UfukOS.
