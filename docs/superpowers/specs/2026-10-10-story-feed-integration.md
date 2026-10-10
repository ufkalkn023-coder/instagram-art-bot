# Reviewed stories in prepared Feed

User approved all current visual/editorial examples and completing the proposed
queue and Insights integration. This task implements and verifies that integration
locally. No live Gemini is needed; production activation is a distinct rollout.

Keep the existing Feed `carousel`/`single` alternation and exact-SHA authorization,
reservation CAS, ambiguity stop, receipts, fresh rights checks and queue ownership.
A story is an explicit `artfolio-story-delivery-v1` treatment of one carousel slot.
Its 1–8 unique canonical source artworks are reserved once; its 3–10 ordered JPEG
pages may reference a source repeatedly. Existing carousel cardinality remains
6–9 distinct artworks/pages. Local drafts may have 12 pages; export rejects more
than 10 without truncation (Meta's official publishing sample documents 10).
Source: https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md#carousel-posts

Review approval binds the exact project revision, plan/source metadata, original
source bytes and ordered rendered page bytes. Editing or changing any bytes makes
approval stale. Quality/rights/image gates remain mandatory. Approval is editorial
acceptance, never a production publication permit. Export includes a complete
source/page mapping and reviewed digest; queue manifests hash all delivered media.

Use a separate `PreparedStoryContent` type, explicit queue content kind and typed
story delivery metadata in reservation, publication and receipt. Receipt artwork
positions continue to describe unique source membership; story pages describe the
separate ordered presentation. Reconciliation validates source roles and exact
child-container count independently. A one-source story remains a carousel.

CLI `preview_story approve/export` supports reviewed local artifacts. Queue CLI
`--story-project` substitutes reviewed stories into carousel preparation slots;
single slots keep the existing preparation path. Cross-package deduplication and
fresh museum rights validation apply unchanged.

Insights collects one snapshot per parent publication/media ID. Add narrative,
actual cover style, headline provenance and page count to contextual features and
age-matched descriptive story cohorts. Do not claim a winner or grow observation
counts from repeated pages. Old records and the legacy collector stay compatible.

Acceptance: reviewed export → sealed local/private-R2 queue → mocked guarded
publication → one unique-source history publication/receipt → one analytics
observation, including one-source stories, stale approval, changed assets, invalid
page references, duplicate collisions, uncertainty and reconciliation. Full offline
suite and targeted lint/format checks must pass. Preserve unrelated `.mimosa`.
