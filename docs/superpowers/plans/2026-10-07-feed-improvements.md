# Feed Improvements Implementation Plan

> **For agentic workers:** Use the established test-first workflow and implement these stages sequentially. The user approved this roadmap on 2026-10-07. Preserve all existing publication authorization and state-integrity boundaries.

**Goal:** Improve publishing continuity, measure both Feed formats, prepare reliable content ahead of publication, and evaluate editorial choices using mature results.

**Architecture:** Extend the existing scheduler, Insights storage, selection and editorial modules. Each stage must pass focused regressions and the full suite before the next stage is integrated. Keep analytical/preview work separate from irreversible publication.

**Tech Stack:** Python 3.10+, existing Pydantic, Pillow, Requests and R2 conditional writes; GitHub Actions. No new dependency is planned.

**Spec:** User-approved six recommendations in this chat, implemented in the sequence below. The first stage's concrete contract is specified here.

## Global Constraints

- Preserve minimum 48 hours after the latest proven successful Feed completion, carousel/single alternation, strict rights, durable reservations, exact-SHA approval, original run identity and no ambiguous publish retry.
- Keep existing daily approvals and legacy single-slot permits readable and unchanged. New cadence requires an explicitly matching continuous approval.
- Do not publish a test post, rerun a past publication workflow, alter credentials, or overwrite `.mimosa/`.
- Do not silently add paid infrastructure or a new external notification channel.
- Preserve Reel collection/publishing behavior and keep Feed metrics tied to authoritative publication/media identities.
- Future rollout must use protected-main PR/CI and fresh matching approval; local code alone is not evidence of live rollout.

## Review Focus

- UTC midnight and the minute-17 boundary must yield nonoverlapping, finite windows.
- A clean definitive failure must retain its 24-hour attempt backoff across changes to SHA or approval.
- Cooldown skips must never hide unsafe ownership or unreadable state, or mutate durable state.
- Readiness is advisory: actual admission, reservation and prepublication must revalidate state and authorization.
- Content prepared earlier must be checked again for rights, duplicate history and compatibility immediately before reservation.

## Stage 1 — Publishing cadence and actionable status

**Files:** `src/models.py`, `src/feed_schedule.py`, `scripts/manage_feed_schedule.py`, a read-only readiness script, Feed workflow, schedule/model/workflow regressions and operational documentation.

**Interfaces:**

- Approval and permit `cadence`: `daily_utc1717` (backward-compatible default) or `hourly_utc17`.
- `enable_continuous(..., cadence=...)`: existing approval CAS; idempotent only for matching SHA and cadence.
- `FeedScheduleManager.status(*, expected_sha: str | None = None, now: datetime | None = None) -> dict`: sanitized, read-only operational snapshot with `ready`, `status`, completion/next eligibility/next attempt, next format and reason.

**Contract:** Hourly eligibility at UTC minute 17; continuous hourly windows last exactly 60 minutes. Daily/legacy behavior remains unchanged. Derive next attempt from the proven success +48h and, after a safely closed definitive failure, its recorded outcome +24h. Preserve that failure clock when an approval is replaced. Success may allow a future window whose start equals the prior expiry; overlap and slot reuse remain invalid. Unreadable/invalid state fails with a nonzero exit. Ordinary waiting, paused or mismatched-SHA status emits `ready=false` without writes. A lightweight scheduled check gates expensive tests/preflight/publishing; actual publishing still reloads all guards.

- [x] Write and run failing cadence/model/status regressions (48h equality, midnight, delayed run, immutable cadence, old-state compatibility, failure backoff, consumed run, CAS conflict, unsafe prior attempt).
- [x] Implement the minimal model/scheduler extensions and read-only status.
- [x] Integrate hourly cron and read-only readiness into the existing workflow; publication executes only after all due-run validations succeed.
- [x] Document the cadence and status commands; correct stale daily-only README text.
- [x] Run focused tests, full `pytest -q`, compileall, workflow validation and diff review.

## Stage 2 — Feed analytics and single-post learning

- [ ] Reuse existing exact-ID Feed targets in the Insights collector; verify actual gaps before adding a second collector.
- [ ] Collect supported Feed metrics at existing mature windows without manufacturing missing data or Reel associations.
- [ ] Include single publications in learning with a publication-format feature and retain conservative confidence weighting.
- [ ] Verify identity mismatches, partial/unsupported metrics, duplicate collection and Reel compatibility.

## Stage 3 — Prepared content queue

Local implementation checkpoint, 2026-10-07: format-scoped Feed learning, missing-window reporting and equal-age comparison are implemented and tested in `codex/feed-learning-reports-queue`; see [implementation ledger](2026-10-07-feed-learning-reports-queue.md). A local filesystem queue pilot is also implemented. This stage remains incomplete until shared R2 persistence/distributed ownership and Actions integration are completed. These changes are not deployed; live main remains the Stage 1 rollout.

- [ ] Prepare a bounded queue of 3–5 verified Feed packages using existing acquisition/rendering paths without publication mutation.
- [ ] Persist package identity, content digest, creation/expiry, format and source rights in the existing storage pattern.
- [ ] Consume only compatible, fresh packages; recheck current history/rights/format before reservation. Never reuse a package after uncertain publication.
- [ ] Verify exhaustion/fallback, stale packages, concurrent consumers and preservation of publication locks.

## Stage 4 — Connected editorial planning

- [ ] Plan carousel/single thematic pairs using the existing theme registry and variety controls.
- [ ] Persist the editorial relationship and exclude repeated artworks; preserve museum metadata and uncertainty.
- [ ] Verify missing-theme fallback, theme repetition limits and format rotation.

## Stage 5 — Controlled editorial experiments

- [ ] Extend existing hook/cover taxonomy instead of creating a parallel system.
- [ ] Assign one experimental variable per comparison and persist the assignment with the publication.
- [ ] Evaluate mature, comparable cohorts with minimum evidence and uncertainty; do not auto-promote a winner from sparse results.
- [ ] Verify deterministic assignment, comparable age/format groups and insufficient-data behavior.

## Stage 6 — Consolidated status and notifications

- [ ] Extend Stage 1 status with ready queue count and analytics freshness.
- [ ] Show last success, next opportunity and specific actionable failure; distinguish normal waiting from a blocked bot.
- [ ] Add meaningful-change-only monitoring through the chosen channel. Codex is the proposed default; external recipients require explicit selection.
- [ ] Verify repeated unchanged states stay quiet and failures/recovery are reported once.

## Integration

- [ ] Review final diffs and complete applicable validation for every stage.
- [ ] Record durable decisions and verified deployment state in UfukOS; preserve historical evidence.
- [ ] Report implemented, deployed and pending behavior separately.

## Stage 1 Rollout — Authorized by User

The user's direct “yayınla” reply authorizes this concrete commit/PR/CI/merge/hourly-approval rollout. It does not request an extra Instagram post.

1. Commit only the Stage 1 files on `codex/feed-improvements`; exclude `.mimosa/`.
2. Push the branch and create/attach a focused pull request. Wait for required Python 3.10 CI; do not bypass protected main.
3. Merge the green PR normally, verify the deployed main tree and post-merge CI.
4. Read current durable generation, receipt coverage, last proven completion, cleanup/owned-media guards and GitHub main identity. Do not reuse a recorded generation or baseline.
5. Call the existing `enable-continuous` CAS once for that exact main SHA with `--cadence hourly_utc17`; read back matching active approval and unchanged publication/receipt/duplicate protections.
6. Read-only status must reflect the original +48-hour minimum and the next auto format. Do not dispatch a manual/test post, rerun an old run, shorten cooldown or restart the obsolete follow-up.

Stages 2–6 remain implementation work after this first individually reviewable stage. The Insights collector already collects exact-ID Feed targets; the confirmed Stage 2 gap is that learning observations currently filter out singles. Reuse the existing collector and mature snapshot rules.

## Local Verification — Stage 1

- Full local suite: `python3 -m pytest -q` — **1442 passed, 10 skipped in 43.29s**.
- Focused model/schedule/operator/single suites — **133 passed**; workflow/status tests also passed within the full suite.
- `python3 -m compileall -q main.py src scripts tests` and `git diff --check` passed.
- Safety review found a false READY result for legacy permit dates/short expiry; fixed with regressions. R2 media ownership reads are intentionally retained before ordinary waiting.
- At the local verification checkpoint, the existing daily live SHA/approval was unchanged. No Git commit, push, PR, merge, scheduler/approval write or Instagram request had been made. Deployment evidence will be recorded in UfukOS after rollout.
