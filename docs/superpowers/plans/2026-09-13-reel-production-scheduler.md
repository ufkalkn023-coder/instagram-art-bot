# Reel Production Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved scheduler design
(`docs/superpowers/specs/2026-09-13-reel-production-scheduler-design.md`): one workflow
run selects one artwork, produces one deeply verified `artfolio-reels` release, and
publishes it once through the existing `publish_verified_reel()` production path.

**Spec:** `docs/superpowers/specs/2026-09-13-reel-production-scheduler-design.md`

## Global Constraints

- Existing carousel behavior is unchanged: no edits to `main.py`, feed modules,
  `.github/workflows/instagram_bot.yml`, counters, grid tone, or feed lifecycle.
- `artfolio-reels` is consumed as an external pinned checkout; do not modify it unless
  a concrete integration blocker is proven.
- One run = one artwork = one verified release = at most one `media_publish` call.
- No new Instagram publisher: `src/reel_publication.publish_verified_reel()` (wrapped
  by `scripts/publish_reel.py`) is the only publication path.
- Publication lifecycle (`PENDING`, `PUBLISHING`, `PUBLISHED`, `AMBIGUOUS`, `EXPIRED`)
  and all Task 1-4 history/reconciliation semantics remain authoritative.
- No blind retry after the publish boundary; `PUBLISHING`/`AMBIGUOUS` never
  automatically republish; only pre-publish proven failures expire and clean staging.
- No 7-day R2 retention cleanup worker in this plan.
- No remediation of unrelated Mimosa/security findings in this plan.
- Do not move, retag, or recreate stable tags in either repository.
- Every test is networkless: no real Instagram, no real R2 mutation, no real render,
  no real subprocess network calls; `npm`/`curl`/`subprocess` boundaries are mocked.
- Secrets appear only as names; never print, log, or commit credential values.
- Keep each task small enough for one independent implementation prompt; avoid giant
  test matrices and unrelated refactors.

## Planned File Map

- Create `src/reel_production.py`: single-Reel orchestration entry point
  (acquire → select over handoffs → produce → package → deep verify → publish).
- Create `scripts/produce_reel.py`: thin CLI wrapper for the orchestration entry point.
- Create `scripts/reconcile_reels.py`: thin CLI wrapper around the existing
  `reconcile_reel_publications()` for the workflow pre-flight stage.
- Create `.github/workflows/instagram_reels.yml`: the new workflow.
- Create `tests/test_reel_production.py`: orchestration tests.
- Create `tests/test_reel_production_cli.py`: CLI + workflow structure tests.
- Re-run unchanged: `tests/test_r2_reel_media.py`, `tests/test_reel_publication.py`,
  `tests/test_reel_publication_lifecycle.py`, `tests/test_reel_reconciliation.py`,
  `tests/test_reel_release.py`, `tests/test_instagram_poster.py`.

Files intentionally outside the change set: `main.py`, `src/r2_media.py`,
`src/instagram_poster.py`, `src/history_tracker.py`, `src/reel_publication.py`,
`src/reel_reconciliation.py`, `src/reel_release.py`, all `src/reel_*` acquisition,
selection, and handoff modules, `.github/workflows/instagram_bot.yml`, and both
repositories' tags.

## External commands consumed (verified against `artfolio-reels` @ working tree)

- Single Reel production: `npm run reel -- <handoff-json> [--render]`
  (planner integration, AFM music, render, QC; writes `data/reel-production-history.json`
  inside the reels workspace).
- Package creation: `npm run package -- <reel-id> [--overwrite]`
  (produces `output/releases/<reel-id>/` with the exact five-file contract).
- Deep verification: `npm run reels:verify-release -- <reel-id> --deep --json`
  (exit 0, `valid=true`, `errors=[]`, H.264 1080x1920 @30fps, valid duration, non-empty
  audio codec, usable `maxVolumeDb`).

---

### Task 1: Single-Reel production orchestration entry point (no publish, no render)

**Files:**

- Create: `src/reel_production.py`
- Create: `tests/test_reel_production.py`

**Interfaces:**

- Consumes (all existing, unchanged):
  - `reel_candidate_acquisition.acquire_reel_candidate_pool(...)` (rights validation,
    download validation, handoff export, museum credential env contract)
  - `reel_batch_candidates.build_batch_candidate_queue(...)` (deterministic selection
    over the exported handoffs, production-history exclusion, Remotion queue contract)
- Produces:
  - `ReelProductionSelectionError(RuntimeError)` when no eligible candidate survives.
  - `produce_reel_handoff(*, pool_size=None, attempt_limit=None, handoff_directory=...,
    manifest_path=None, work_directory=None, selection_target=None, environment=None)
    -> ReelProductionSelection` returning a frozen result with exactly one accepted
    artwork's canonical ID and handoff JSON path plus the acquisition/queue summaries.

**Steps:**

- [ ] **Step 1 (test first):** In `tests/test_reel_production.py`, write failing tests
  proving: exactly one handoff is selected from a seeded handoff pool; zero eligible
  candidates raises `ReelProductionSelectionError`; the function composes the existing
  acquisition and batch-candidate queue functions (monkeypatched recorders observe
  exactly one call each, in that order, with the configured limits) and duplicates
  none of their logic; no network, R2, or Instagram boundary is touched.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_reel_production.py -q` → RED.
- [ ] **Step 3:** Implement `produce_reel_handoff()` as pure composition of the
  existing functions: acquire a bounded handoff pool, run the batch-candidate queue
  over the exported handoffs, pick the single first queue entry, and fail closed when
  none exists. No publish, no render, no subprocess in this task.
- [ ] **Step 4:** Run `python3 -m pytest tests/test_reel_production.py -q` → GREEN.
- [ ] **Step 5:** Verify: `python3 -m pytest -q`, `python3 -m ruff check src/reel_production.py tests/test_reel_production.py`, `python3 -m compileall -q src tests`, `git diff --check`.
- [ ] **Step 6 (commit boundary):** `git add src/reel_production.py tests/test_reel_production.py && git commit -m "feat: add single-Reel production orchestration"`.

### Task 2: Wire the entry point to the pinned `artfolio-reels` checkout

**Files:**

- Modify: `src/reel_production.py`
- Modify: `tests/test_reel_production.py`

**Steps:**

- [ ] **Step 1 (test first):** Add failing tests for `produce_verified_reel_release(...)`
  which, given the Task 1 result plus `reels_repository` and an injected command runner:
  copies the handoff JSON into the reels workspace at a deterministic path; runs exactly
  `npm run reel -- <handoff> --render`, then `npm run package -- <reel-id>`, then
  `npm run reels:verify-release -- <reel-id> --deep --json` in `reels_repository`;
  parses strict JSON (one object, npm banner tolerated) requiring `valid=true`,
  `errors=[]`, and the expected `reelId`; returns the release directory path; fails
  closed with a clear `RuntimeError` on any non-zero exit, invalid JSON, or
  `valid=false` without calling any later stage. The runner seam is injected; tests
  never execute real npm.
- [ ] **Step 2:** Run the focused tests → RED.
- [ ] **Step 3:** Implement the three-stage handoff→produce→package→verify pipeline
  behind the injected runner seam (production defaults to `subprocess.run`), mirroring
  the strict subprocess handling style of `src/reel_release.py` (bounded output, no
  shell, secrets never in errors).
- [ ] **Step 4:** Run the focused tests → GREEN.
- [ ] **Step 5:** Verify: `python3 -m pytest tests/test_reel_production.py tests/test_reel_release.py -q`, `python3 -m pytest -q`, `python3 -m ruff check src/reel_production.py tests/test_reel_production.py`, `python3 -m compileall -q src scripts tests`, `git diff --check`.
- [ ] **Step 6 (commit boundary):** `git add src/reel_production.py tests/test_reel_production.py && git commit -m "feat: stage and verify Reel production handoffs"`.

### Task 3: Publish phase reusing the existing publication path

**Files:**

- Modify: `src/reel_production.py`
- Modify: `tests/test_reel_production.py`

**Steps:**

- [ ] **Step 1 (test first):** Add failing tests proving the entry point's publish
  phase calls `reel_publication.publish_verified_reel(...)` **exactly once** with the
  packaged release path, `reels_repository`, `account_id`, and `access_token`; that a
  publication failure propagates unchanged with **no second call** (no retry at any
  level); that pre-publish production failures (Task 2) result in zero publish calls;
  and that the full sequence respects one-run/one-release/one-publish-attempt.
- [ ] **Step 2:** Run the focused tests → RED.
- [ ] **Step 3:** Implement the publish phase as a single direct call to the existing
  `publish_verified_reel(...)` and return its `ReelPublicationRecord`. No wrapper
  retries, no exception mapping, no new publisher.
- [ ] **Step 4:** Run the focused tests → GREEN.
- [ ] **Step 5:** Verify: `python3 -m pytest tests/test_reel_production.py tests/test_reel_publication.py -q`, `python3 -m pytest -q`, `python3 -m ruff check src/reel_production.py tests/test_reel_production.py`, `git diff --check`.
- [ ] **Step 6 (commit boundary):** `git add src/reel_production.py tests/test_reel_production.py && git commit -m "feat: publish verified Reel from production entry point"`.

### Task 4: Workflow file and thin CLIs

**Files:**

- Create: `.github/workflows/instagram_reels.yml`
- Create: `scripts/produce_reel.py`
- Create: `scripts/reconcile_reels.py`
- Create: `tests/test_reel_production_cli.py`

**Steps:**

- [ ] **Step 1 (test first):** In `tests/test_reel_production_cli.py`, write failing
  tests that (a) validate the workflow YAML as text: triggers (`workflow_dispatch`
  with required `confirm_publish` equal to `PUBLISH_REEL_TO_INSTAGRAM`; schedule cron
  `0 7,12,17,22 * * *`), job gated by `vars.ARTFOLIO_REEL_SCHEDULE_ENABLED == 'true'`,
  two checkouts with `ARTFOLIO_REELS_PRODUCTION_REF` and a SHA/`stable-` shape check
  step, Python 3.10 setup plus `actions/setup-node` with `node-version-file` pointing
  at the checked-out `artfolio-reels` `.node-version` (the pinned repository's
  `.node-version` is authoritative; the currently tested production line uses Node 24,
  but the workflow must not hardcode the version), `GOOGLE_GEMINI_API_KEY` mapped to
  both `GOOGLE_GEMINI_API_KEY` and `GEMINI_API_KEY`, R2 + Instagram + museum secret
  names present, `concurrency.group` equal to the carousel workflow's group
  (`instagram-bot`), preflight steps (compileall, `pytest -q`, production-config
  validation), a reconciliation step using `scripts/reconcile_reels.py`, a production
  step using `scripts/produce_reel.py`, and **no** `actions/upload-artifact` step for
  `reel.mp4`; and (b) validate the two CLIs with monkeypatched orchestration and
  reconciliation functions (no real network): argument contract, environment
  credential pass-through, sanitized failures, exit codes.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_reel_production_cli.py -q` → RED.
- [ ] **Step 3:** Implement the workflow file and the two thin CLIs (argparse wrappers
  only; all logic stays in `src/reel_production.py` and the existing
  `reconcile_reel_publications()`).
- [ ] **Step 4:** Run `python3 -m pytest tests/test_reel_production_cli.py -q` → GREEN.
- [ ] **Step 5:** Verify: `python3 -m pytest -q`, `python3 -m ruff check scripts/produce_reel.py scripts/reconcile_reels.py tests/test_reel_production_cli.py`, `python3 -m compileall -q src scripts tests`, `git diff --check`, and confirm `git status --short` shows no change to `.github/workflows/instagram_bot.yml`.
- [ ] **Step 6 (commit boundary):** `git add .github/workflows/instagram_reels.yml scripts/produce_reel.py scripts/reconcile_reels.py tests/test_reel_production_cli.py && git commit -m "feat: add scheduled Reel production workflow"`.

### Task 5: Focused invariant and safety tests

**Files:**

- Modify: `tests/test_reel_production.py`
- Modify: `tests/test_reel_production_cli.py`

**Steps:**

- [ ] **Step 1 (test first):** Add the consolidated invariant tests: end-to-end
  orchestration with every boundary mocked proves one run produces exactly one release
  and calls `publish_verified_reel` exactly once; a failed deep verification yields
  zero publish calls; a failed publish is never retried inside the entry point; no
  test path reaches `instagram_poster` or `r2_media` network functions (assert via
  mocks with `side_effect=AssertionError`); log and error capture contains no secret
  or token value; the workflow text contains no `reel.mp4` artifact upload and no
  credential literal.
- [ ] **Step 2:** Run both test files → RED where an invariant is not yet held.
- [ ] **Step 3:** Make the smallest fixes if any invariant test exposes a gap.
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_reel_production.py tests/test_reel_production_cli.py tests/test_reel_publication.py tests/test_r2_reel_media.py -q`, `python3 -m pytest -q`, `python3 -m compileall -q src scripts tests`, `python3 -m ruff check src scripts tests/test_reel_production.py tests/test_reel_production_cli.py`, `git diff --check`.
- [ ] **Step 5 (commit boundary):** `git add tests/test_reel_production.py tests/test_reel_production_cli.py && git commit -m "test: pin Reel scheduler safety invariants"`.

### Task 6: Manual GitHub-hosted runner production proof (operational)

No code changes. Execute after Tasks 1-5 are merged to `main`.

- [ ] Set repository variables: `ARTFOLIO_REELS_PRODUCTION_REF` (tested full SHA or
  `stable-` tag of `artfolio-reels`) and leave `ARTFOLIO_REEL_SCHEDULE_ENABLED` unset.
- [ ] Dispatch `instagram_reels.yml` with `confirm_publish=PUBLISH_REEL_TO_INSTAGRAM`.
- [ ] Accept: job green; deep verification passed before publish (job log); exactly
  one `reel_publications` row and one media ID; `reel_publication_count` +1; feed
  rows, `grid_publication_count`, and grid tone byte-identical; published Instagram
  media reports `media_product_type=REELS`; staged object healthy under
  `reels/publications/<publication_id>/`.
- [ ] Repeat until at least one fully green run and one exercised failure boundary
  (for example, a temporarily invalid pin) behave per the spec's failure table.
- [ ] Commit boundary: none (operational task).

### Task 7: Schedule enablement (operational, gated)

- [ ] **Step 1:** After Task 6 passes: set `ARTFOLIO_REEL_SCHEDULE_ENABLED=true` and
  temporarily reduce the workflow cron to a single daily slot (`0 17 * * *`);
  commit boundary: `git commit -m "chore: enable single-slot Reel schedule"` (workflow
  file only).
- [ ] **Step 2:** Observe several consecutive scheduled runs, including one forced
  failure path.
- [ ] **Step 3:** Widen the cron to `0 7,12,17,22 * * *` (4 Reels/day); commit
  boundary: `git commit -m "chore: enable full Reel schedule"` (workflow file only).
- [ ] Verification after each step: workflow run history green; R2 history counts
  consistent; no `AMBIGUOUS`/`PUBLISHING` artwork ever re-published.

## Plan Self-Review

- **Scope:** Tasks 1-5 are code (entry point, wiring, publish reuse, workflow, tests);
  Tasks 6-7 are operational. No feed, carousel, `artfolio-reels`, tag, secret-value,
  or 7-day-cleanup work is included; Mimosa remediation is explicitly excluded.
- **Reuse:** selection, acquisition, rights, dedup, handoff, reconciliation,
  publication, and verification are all existing production code; the only new logic
  is composition, command sequencing, and the workflow definition.
- **Safety invariants:** enforced by existing primitives (`reserve_reel` global
  dedup, durable boundary, single `media_publish`, monotonic lifecycle) and pinned by
  Task 5 tests.
- **Known operational risk (not a design blocker):** the repository's Mimosa pre-commit
  gate currently force-blocks commits on 12 pre-existing, unrelated high findings;
  implementation tasks must not remediate them and may require the operator to create
  commit-boundary commits from an environment where the gate passes.
