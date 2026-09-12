# Artfolio Automated Reel Production Scheduler Design

**Status:** Approved architecture, implementation not started
**Date:** 2026-09-13
**Builds on:** `docs/superpowers/specs/2026-09-11-reel-publication-integration-design.md`

## Goal

Run the proven manual one-Reel publication path on a schedule: each run selects one
eligible artwork, produces one deeply verified `artfolio-reels` release, and publishes
it as one Instagram Reel through the existing `publish_verified_reel()` orchestration,
with the same lifecycle, dedup, and safety invariants as the manual path.

## Scope and non-negotiable invariants

- The existing carousel workflow (`.github/workflows/instagram_bot.yml`) remains
  separate and unchanged. Reel scheduling is a new, isolated workflow.
- One workflow run equals one artwork equals one verified Reel and results in **at
  most one** `media_publish` call. This is enforced by the existing
  `publish_verified_reel()` contract, not by the scheduler.
- `instagram-art-bot` owns: candidate acquisition, rights validation, the
  authoritative R2 history, global feed+Reel deduplication, handoff creation, and
  Instagram publishing.
- `artfolio-reels` owns: the Gemini planner, AFM music selection, the Remotion render,
  QC, release packaging, and deep release verification.
- The existing publication lifecycle remains authoritative and monotonic:
  `PENDING`, `PUBLISHING`, `PUBLISHED`, `AMBIGUOUS`, `EXPIRED`.
- No blind retry after the Instagram publish boundary. `PUBLISHING` and `AMBIGUOUS`
  reservations are never automatically republished by the scheduler, a later run, or
  reconciliation. Only pre-publish proven failures may expire and clean staging safely.
- Existing feed publishing is not behaviorally changed by the first Reel scheduler
  implementation: no feed module, lifecycle rule, counter, or carousel workflow edit.
- `artfolio-reels` production and render code remains unchanged unless a concrete
  integration blocker is proven.
- Existing stable tags are not moved.

## Architecture and data flow

One workflow run performs these stages in order. Every stage is existing production
code except the workflow definition itself and the pinning plumbing.

1. **Checkouts.** The workflow checks out two repositories into separate paths:
   - `instagram-art-bot` (this repository) at the workflow's own commit;
   - `artfolio-reels` into `./artfolio-reels`, pinned as described below.
2. **Environment.** Python 3.10 with the hash-pinned Art Bot lock files, mirroring the
   carousel workflow. Node.js LTS 20 with `npm ci` inside `./artfolio-reels` for the
   planner/QC/render/packaging toolchain. No new dependency sources are introduced.
3. **Preflight.** `python -m compileall -q main.py src scripts tests`, the networkless
   `pytest -q` suite, and the existing production-configuration validation
   (`python main.py --validate-production-config`). A red preflight never reaches
   selection or publication.
4. **Bounded reconciliation.** Run the existing
   `reconcile_reel_publications()` once, bounded and conservative: expire safely stale
   pre-Meta `PENDING` reservations, keep or record `AMBIGUOUS` evidence, and process
   the Reel-only staging cleanup queue. This is existing Task 4 production code; it
   never selects artwork, never creates containers, and never calls `media_publish`.
5. **Selection and acquisition (instagram-art-bot).** The existing Reel selection and
   acquisition path runs: `reel_selector.select_reel_candidates()` builds the bounded
   shortlist, `reel_candidate_acquisition.acquire_reel_candidate_pool()` fills a small
   handoff pool using museum adapters with rights validation, and
   `reel_handoff.export_reel_handoff()` writes the deterministic handoff package.
   Global feed+Reel protection is inherited automatically because acquisition reserves
   nothing and the publish path re-checks global protection inside `reserve_reel()`.
6. **Handoff to `artfolio-reels`.** The chosen handoff package is copied into the
   pinned `./artfolio-reels` working tree. No handoff schema changes.
7. **Production (artfolio-reels).** The pinned repository's existing scripts plan the
   Reel with Gemini (`GEMINI_API_KEY`), select AFM music, render with Remotion, run
   QC, and package `output/releases/<reel_id>` with `reel.mp4`, `caption.txt`,
   `metadata.json`, `manifest.json`, and `qc/contact-sheet.png`. No render or planner
   behavior changes.
8. **Deep verification.** `npm run reels:verify-release -- <release> --deep --json`
   must exit 0 with `valid=true`, `errors=[]`, H.264 1080x1920 at 30 fps, valid
   duration, non-empty audio, usable loudness, and accepted AFM identity. A failed
   verification stops the run before any reservation, staging, or Meta call.
9. **Publication (instagram-art-bot).** The existing manual command publishes the
   verified release:
   `python scripts/publish_reel.py <release> --artfolio-reels-root ./artfolio-reels`.
   This reuses `publish_verified_reel()` end to end: verified intake with private
   snapshot, global `reserve_reel()`, explicit sequential multipart R2 staging with the
   presigned-URL/curl transport, durable `record_reel_staging()`, the durable
   `before_publish` `PUBLISHING` boundary, at most one `media_publish`, durable
   receipt, best-effort permalink, and idempotent finalization.
10. **Run summary.** The job writes a small run manifest (release ID, publication ID,
    media ID, lifecycle outcome) to the workflow log and uploads only small
    diagnostic artifacts.

`scripts/publish_reel.py` is the only publication entry point. No second Instagram
publisher is created, and the workflow does not import or duplicate orchestrator logic.

## Workflow design

- New file: `.github/workflows/instagram_reels.yml`.
- Triggers: `workflow_dispatch` (with a required `confirm_publish` string input that
  must equal the fixed phrase `PUBLISH_REEL_TO_INSTAGRAM`, mirroring the carousel
  workflow's guard) and `schedule`.
- The schedule steps run only when the repository variable
  `ARTFOLIO_REEL_SCHEDULE_ENABLED` is exactly `"true"`; a manual dispatch always runs
  when confirmed. This mirrors the existing `ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED`
  gate and keeps scheduled publication switchable without code edits.
- `permissions: contents: read` only.
- `runs-on: ubuntu-latest` (GitHub-hosted runner) for rollout.
- `timeout-minutes: 60` (Remotion render plus staging and publication, with headroom
  over the proven manual runtime).
- Concurrency: the job joins the **same production concurrency group the carousel
  workflow already uses** (`group: instagram-bot`, `cancel-in-progress: false`).
  Sharing the existing group name serializes carousel and Reel production runs
  against the R2 history document without editing `instagram_bot.yml`, so existing
  feed publishing is untouched while history contention is reduced.

### Repository pinning

`artfolio-reels` is checked out with `actions/checkout` from
`ARTFOLIO_REELS_PRODUCTION_REF`, a repository variable that must contain either a
full 40-character commit SHA or a `stable-` prefixed immutable tag of the tested
production revision. The workflow fails fast if the variable is unset, blank, or does
not match one of those two shapes; a branch name such as `main` is not a valid value.
Promotion to a new pin is a deliberate variable edit after the referenced commit has
passed the manual dispatch proof.

### Schedule (Turkey time, UTC+3, no DST)

| Local (TRT) | UTC | Cron |
| --- | --- | --- |
| 10:00 | 07:00 | `0 7 * * *` |
| 15:00 | 12:00 | `0 12 * * *` |
| 20:00 | 17:00 | `0 17 * * *` |
| 01:00 | 22:00 | `0 22 * * *` |

Combined cron: `0 7,12,17,22 * * *`. Turkey has no daylight saving time, so the
mapping is stable year-round.

## Failure boundary table

| Failure point | Durable result | External action |
| --- | --- | --- |
| Preflight (compile, tests, config validation) fails | Run stops; nothing changes | No selection, no R2, no Meta. |
| Selection/acquisition/handoff failure | Run ends non-zero; no history change | No render, no R2, no Meta. |
| Render, QC, or packaging failure in `artfolio-reels` | No release; no history change | No reservation, no R2 staging, no Meta. |
| Deep verification failure | No publication; release kept for inspection in the workspace only | No reservation, no staging, no Meta. |
| Release intake failure inside `publish_verified_reel()` | No reservation | No R2 or Meta call (existing contract). |
| Reservation collision or CAS exhaustion | Existing owner unchanged | No staging, no Meta. |
| R2 staging (multipart/curl) failure | Reservation `EXPIRED` with `reel_staging_failed`; cleanup queued | Exact/prefix Reel cleanup only; no Meta. |
| `before_publish` cannot be confirmed durable | Publisher raises `InstagramPrePublishBoundaryError`; state re-read; expired only when provably pre-Meta | `media_publish` is never called. |
| Any post-boundary uncertainty (4xx, timeout, reset, malformed, missing ID, 5xx, ambiguous, unexpected) | Reservation conservatively `AMBIGUOUS`; original error propagates | Never expired, never cleaned, never republished. |
| Receipt/finalization persistence failure after Meta success | `ReelPublicationPersistenceError`; receipt stays durable | Never republished; resolved later by reconciliation only. |
| Permalink lookup or enrichment failure | Reel remains `PUBLISHED` | Reconciliation may enrich the permalink later. |
| Workflow/infrastructure failure mid-run | All reached lifecycle states remain durable and protected | The next run selects different artwork; `AMBIGUOUS`/`PUBLISHING` are never auto-retried. |

Across runs: global feed+Reel deduplication guarantees a protected or published
artwork is never selected for the other format; `AMBIGUOUS` never expires by age and
never returns to the candidate pool automatically.

## Rollout stages

1. **Manual proof.** `workflow_dispatch` on a GitHub-hosted runner with the
   confirmation phrase. Acceptance: one verified release, one `PUBLISHED` reservation
   with exactly one media ID in history, staged MP4 healthy at its public URL, small
   diagnostic artifacts present. Repeat at least once successfully.
2. **Scheduled gate.** Set `ARTFOLIO_REEL_SCHEDULE_ENABLED=true` with the workflow's
   schedule temporarily reduced to a single daily cron slot (for example
   `0 17 * * *`); verify several consecutive scheduled runs, including at least one
   forced failure path (for example, a temporarily invalid pin), before widening the
   cron.
3. **Full cadence.** Widen the cron to the full four-slot schedule (4 Reels/day).
   Any change to the pin, the schedule, or the secrets repeats stage 1 first.

## Security and secrets

- Secrets are referenced by name only and are never printed, echoed, or written to
  artifacts or logs: `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`,
  `CLOUDFLARE_R2_ACCOUNT_ID`, `CLOUDFLARE_R2_ACCESS_KEY_ID`,
  `CLOUDFLARE_R2_SECRET_ACCESS_KEY`, `CLOUDFLARE_R2_BUCKET_NAME`,
  `CLOUDFLARE_R2_PUBLIC_URL`, `GOOGLE_GEMINI_API_KEY`,
  `SMITHSONIAN_API_KEY`, `EUROPEANA_API_KEY`.
- One existing Gemini secret is reused: `GOOGLE_GEMINI_API_KEY` is mapped to
  `GEMINI_API_KEY` for the `artfolio-reels` steps inside the workflow
  (`GEMINI_API_KEY: ${{ secrets.GOOGLE_GEMINI_API_KEY }}`). No second Gemini key is
  created or required.
- Credentials exist only as step environment variables; nothing is written to disk
  outside runner-provided tool configuration.
- Presigned upload-part URLs are single-purpose and short-lived; they are passed only
  as arguments to curl, never printed, and never included in artifacts.
- Artifacts contain only small diagnostic outputs: the deep-verification JSON, the
  run manifest (release ID, publication ID, media ID, lifecycle outcome), and job
  logs. `reel.mp4`, staged MP4s, handoff images, and caption payloads are never
  uploaded as artifacts.
- Runner concurrency plus the `instagram-bot` group prevent concurrent writers to the
  R2 history document; the quoted-ETag CAS contract remains the authoritative guard.

## Staged media retention

Successful Reel publications intentionally keep their staged R2 MP4s after
publication; only `EXPIRED` staging is cleaned today. Design target: retain
successful staged media for 7 days, then remove it with a bounded, Reel-only cleanup
worker that reuses the existing Reel prefix-cleanup API and never touches the feed
image namespace. That retention worker is a later isolated task and is explicitly out
of scope for the first scheduler implementation.

## Testing and acceptance criteria

- **Continuous:** every workflow run executes compileall, the networkless pytest
  suite, and production-configuration validation before doing anything; PR CI
  (`ci.yml`) remains the gate for code changes.
- **Scheduler unit/contract tests (networkless):** the workflow file is validated for
  structure in tests (pin variable shape, confirmation input, shared concurrency
  group, schedule cron, enabled-variable gate) and no secrets appear as literals.
- **Manual dispatch acceptance (stage 1):** exactly one `reel_publications` row and
  one media ID per run; `reel_publication_count` incremented by exactly one; feed
  rows, `grid_publication_count`, and grid tone byte-identical; staged object present
  under `reels/publications/<publication_id>/` and healthy; artifacts present and
  free of `reel.mp4` or secrets.
- **Safety acceptance:** forcing each failure boundary (bad pin, verification failure,
  staging failure) yields the durable states in the failure boundary table with no
  `media_publish` call where the table says none; a deliberately ambiguous publish
  outcome leaves the artwork protected and unpublished on all subsequent runs.
- **Feed regression:** the carousel workflow file and feed production code produce no
  diff in the scheduler implementation commit.

## Explicit non-goals (first scheduler implementation)

- No R2 7-day retention cleanup worker yet.
- No automatic recovery from `AMBIGUOUS` (operator recovery via the existing
  reconciliation module remains a manual, evidence-gated action).
- No new Instagram publisher; `scripts/publish_reel.py` / `publish_verified_reel()`
  is the only path.
- No changes to Reel visuals, templates, or QC rules in `artfolio-reels`.
- No changes to AFM music selection behavior.
- No changes to carousel generation behavior or the carousel workflow.
- No moving, retagging, or recreating existing stable tags.
- No dry-run mode that bypasses verified release intake.
- No multi-Reel batching inside one run, no second publication per run, and no
  retry loop around publication at the workflow level.
- No self-hosted runners in the initial rollout.
