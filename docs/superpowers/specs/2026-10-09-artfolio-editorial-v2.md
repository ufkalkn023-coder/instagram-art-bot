# Artfolio 2.0 — first local delivery

User brief: the supplied Artfolio 2.0 text, compared with the repository and
approved with “gerekli olanları yapalım”. First delivery is Phase 1–2: a
non-publishing editorial engine and 30 rights-cleared works × three cover styles.
The existing uncommitted editorial style remains part of this work.

## Contract

- Preserve production theme IDs, publishing, receipts, reservations, rights,
  queue bytes, cadence, live approval and existing CLI behavior.
- Preview-only Gemini `gemini-3.8-flash`, explicit Medium, image bytes plus museum
  metadata, strict JSON, bounded timeout/output/call count. Existing production
  Gemini configuration is not silently switched.
- Separate `public_title`, `editorial_angle`, `headline_evidence`, theme identity,
  source metadata, model/protocol version and chosen cover style.
- Request 4–6 candidates with image observations or exact museum-source quotes.
  Reject unknown identities, unsupported quotations, invalid crop coordinates,
  generic/duplicate titles and explicitly failed visual review. AI agreement is
  review evidence, not independent art-historical truth certification.
- Last-100-title lexical similarity, plus in-batch repetition checks. Sparse
  legacy history is disclosed; no historical titles are invented.
- Unusable AI returns the original museum title, with a reason and no invented
  angle/crop. Such outputs are labeled fallback, not successful AI curation.
- Museum Journal, Artwork First, Detail Study share bundled fonts. Full-artwork
  style retains the entire image on a flat field. Detail crops require bounded
  focus coordinates; missing focus renders a labeled full-artwork alternative.
- Cache only exact model/protocol/image/metadata identities in local files.
  Paid calls are opt-in and bounded by an explicit maximum call count.
- A local CLI ingests source manifests or acquires bounded Cleveland candidates,
  applies strict rights and existing secure download validation, renders three
  styles per work, writes evidence/quality manifest and an escaped HTML gallery.
  It imports no publishing/R2/history-mutation client and loads no IG credential.
- No live AI success is claimed without an actual response. No deployment,
  production main change, credential update or Instagram action is authorized.

## Acceptance

Focused regression tests, full existing suite, Ruff/compile/diff checks, portable
font loading, visual inspection, and a genuine 30-work/90-cover local gallery.
Record actual model usage/fallbacks and known limitations. Advanced narrative
slides and attribution-safe learning are subsequent deliveries after visual review.

## Sources

- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/generate-content/thinking
- https://ai.google.dev/gemini-api/docs/structured-output
- Current repository museum adapters, rights policy, secure downloads and cover renderer.

## Visual-review correction — 2026-10-09

User correctly noted that the initial Detail Study column repeated full artworks.
The local gallery now accepts explicit reviewed crop coordinates through
`--detail-focus` with an existing `--manifest`. These are validated before output
writes, labeled `preview_focus`, recorded with the cover, and kept separate from
AI-selected plans/evidence. Missing focus still uses the honest full-artwork
fallback. The revised gallery has 30 actual detail crops selected after inspecting
the 30 source images; no live Gemini call or historical interpretation is added.
